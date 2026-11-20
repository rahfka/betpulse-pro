import csv
import importlib
import json
import logging
import math
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, make_response, redirect, render_template_string, request, url_for

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("betpulse")

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _charger_module_sport(nom_francais, nom_anglais):
    for mod_name in (nom_francais, nom_anglais):
        try:
            return importlib.import_module(mod_name)
        except Exception:
            continue
    raise ImportError(f"Impossible d'importer '{nom_francais}' ni '{nom_anglais}'.")


fb = _charger_module_sport("compétences_sportives.football", "sports_skills.football")
tn = _charger_module_sport("compétences_sportives.tennis", "sports_skills.tennis")

TZ_PARIS = ZoneInfo("Europe/Paris")

STATUTS_TERMINES = {
    "closed", "final", "post", "completed", "finished",
    "ft", "aet", "pen", "status_final", "status_post",
}
STATUTS_EXCLUS_AVENIR = STATUTS_TERMINES | {
    "in_progress", "live", "postponed", "cancelled", "canceled", "suspended",
}

MOTS_ALERTE_FORT = (
    "forfait", "blessure", "blessé", "blessée", "opéré", "opérée",
    "rupture", "suspendu", "injured",
)
MOTS_ALERTE_FAIBLE = ("incertain", "incertaine", "doute", "gêne", "doubtful", "knock")
MOTS_BRUIT_RSS = (
    "rumeur", "mercato", "transfert", "interview", "podcast", "quiz",
    "pronostic", "pronostics", "cote", "cotes", "direct live", "résumé",
)


@dataclass(frozen=True)
class Config:
    saison_foot: str = field(default_factory=lambda: (os.getenv("SAISON_FOOT") or "2026").strip())
    n_derniers_matchs: int = field(default_factory=lambda: max(1, int(os.getenv("N_DERNIERS_MATCHS", "8"))))
    fenetre_heures: int = field(default_factory=lambda: max(1, int(os.getenv("FENETRE_HEURES", "48"))))
    timeout_http: float = field(default_factory=lambda: max(1.0, float(os.getenv("TIMEOUT_HTTP", "4"))))
    cache_ttl_sec: int = field(default_factory=lambda: max(60, int(os.getenv("CACHE_TTL_SEC", "900"))))
    mise_unite_defaut: float = field(default_factory=lambda: max(0.0, float(os.getenv("MISE_UNITE_DEFAUT", "1.0"))))
    mise_max_unites: float = field(default_factory=lambda: max(0.5, float(os.getenv("MISE_MAX_UNITES", "1.5"))))
    marge_value_pct: float = field(default_factory=lambda: max(0.0, float(os.getenv("MARGE_VALUE_PCT", "4.0"))))
    auto_refresh_sec: int = field(default_factory=lambda: max(0, int(os.getenv("AUTO_REFRESH_SEC", "0"))))
    activer_debug_routes: bool = field(
        default_factory=lambda: os.getenv("ACTIVER_DEBUG_ROUTES", "false").strip().lower() in ("1", "true", "yes")
    )
    odds_api_key: str = field(default_factory=lambda: os.getenv("ODDS_API_KEY", "").strip())
    odds_cache_ttl_sec: int = field(default_factory=lambda: max(300, int(os.getenv("ODDS_CACHE_TTL_SEC", "7200"))))


CFG = Config()

MAP_COMP_ODDS_API = {
    "ligue-1": "soccer_france_ligue_one",
    "la-liga": "soccer_spain_la_liga",
    "bundesliga": "soccer_germany_bundesliga",
    "portuguese-liga": "soccer_portugal_primeira_liga",
    "premier-league": "soccer_epl",
    "serie-a": "soccer_italy_serie_a",
    "uefa-champions-league": "soccer_uefa_champs_league",
    "uefa-europa-league": "soccer_uefa_europa_league",
}

BOOKMAKERS_FR_PRIORITAIRES = {
    "winamax_fr", "winamax_de", "betclic_fr", "parionssport_fr",
    "unibet_fr", "pmu_fr", "bwin_fr", "zebet_fr",
}


class _CacheBorne:
    def __init__(self, max_taille=512):
        self._max_taille = max(32, int(max_taille))
        self._data = OrderedDict()
        self._lock = threading.Lock()

    def get(self, cle, ttl_sec):
        now = time.monotonic()
        with self._lock:
            entree = self._data.get(cle)
            if not entree:
                return None
            ts, val = entree
            if (now - ts) > ttl_sec:
                self._data.pop(cle, None)
                return None
            self._data.move_to_end(cle)
            return val

    def set(self, cle, val):
        now = time.monotonic()
        with self._lock:
            if cle in self._data:
                self._data[cle] = (now, val)
                self._data.move_to_end(cle)
            else:
                self._data[cle] = (now, val)
                while len(self._data) > self._max_taille:
                    self._data.popitem(last=False)

    def clear(self):
        with self._lock:
            self._data.clear()


def _resoudre_chemin_historique():
    env_path = os.getenv("FICHIER_HISTORIQUE", "").strip()
    if env_path:
        return env_path
    dossier_script = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
    try:
        os.makedirs(dossier_script, exist_ok=True)
        test_file = os.path.join(dossier_script, ".write_test")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test_file)
        return os.path.join(dossier_script, "historique_paris.csv")
    except Exception:
        return "/tmp/historique_paris.csv"


FICHIER_HISTORIQUE = _resoudre_chemin_historique()
FICHIER_RAPPORT = os.getenv("FICHIER_RAPPORT", "/tmp/rapport.json").strip() or "/tmp/rapport.json"

COMPETITIONS_CIBLES = [
    {"slug": "ligue-1", "label": "Ligue 1", "equipes": ["160", "174", "176"]},
    {"slug": "la-liga", "label": "Liga Espagnole", "equipes": ["86", "83", "81"]},
    {"slug": "bundesliga", "label": "Bundesliga", "equipes": ["132", "124", "167"]},
    {"slug": "portuguese-liga", "label": "Liga Portugaise", "equipes": ["1929", "1927", "1930"]},
    {"slug": "premier-league", "label": "Premier League", "equipes": ["364", "359", "360"]},
    {"slug": "serie-a", "label": "Serie A", "equipes": ["111", "110", "103"]},
    {"slug": "uefa-champions-league", "label": "Ligue des Champions", "equipes": ["160", "86", "83", "364"]},
    {"slug": "uefa-europa-league", "label": "Europa League", "equipes": ["176", "174", "360", "111"]},
]

_HTTP_SESSION = requests.Session()
_HTTP_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
    )
})

_CACHE_FOOT_SCHED = _CacheBorne(max_taille=400)
_CACHE_SANTE = _CacheBorne(max_taille=400)
_CACHE_COTES_MARCHE = _CacheBorne(max_taille=64)
_STATE_LOCK = threading.Lock()
_CSV_LOCK = threading.Lock()
_ANALYSE_EN_COURS = False
_SCHEDULER_DEMARRE = False

_DERNIER_RAPPORT = {
    "mis_a_jour": None,
    "stats_comp": {},
    "apercu_bruts": "",
    "top_sur": [],
    "foot": [],
    "tennis": [],
    "alertes": {},
    "bilan": {},
    "synthese": "",
}


def _charger_rapport_disque():
    global _DERNIER_RAPPORT
    try:
        if os.path.exists(FICHIER_RAPPORT):
            with open(FICHIER_RAPPORT, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and data.get("mis_a_jour"):
                    with _STATE_LOCK:
                        _DERNIER_RAPPORT = data
    except Exception as e:
        logger.warning("Impossible de charger le rapport disque : %s", e)


def _sauvegarder_rapport_disque(rapport):
    tmp_path = f"{FICHIER_RAPPORT}.tmp"
    try:
        parent = os.path.dirname(FICHIER_RAPPORT)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(rapport, f, ensure_ascii=False)
        os.replace(tmp_path, FICHIER_RAPPORT)
    except Exception as e:
        logger.warning("Impossible de sauvegarder le rapport disque : %s", e)
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def _extraire_events(payload):
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    events = []
    for k in ("events", "matches", "fixtures", "games", "schedules", "items"):
        val = data.get(k)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    if any(x in item for x in ("competitors", "teams", "home_team", "away_team")):
                        events.append(item)
                    else:
                        events.extend(_extraire_events(item))
    return events


def _normaliser_home_away(ev):
    comps = ev.get("competitors")
    if isinstance(comps, dict):
        return comps.get("home") or {}, comps.get("away") or {}
    if isinstance(comps, list):
        h_info = a_info = None
        for c in comps:
            if not isinstance(c, dict):
                continue
            qual = str(c.get("qualifier") or c.get("homeAway") or "").strip().lower()
            t_obj = c.get("team") if isinstance(c.get("team"), dict) else c
            if qual == "home":
                h_info = t_obj
            elif qual == "away":
                a_info = t_obj
        if (not h_info or not a_info) and len(comps) >= 2:
            t0 = comps[0].get("team") if isinstance(comps[0].get("team"), dict) else comps[0]
            t1 = comps[1].get("team") if isinstance(comps[1].get("team"), dict) else comps[1]
            h_info = h_info or t0
            a_info = a_info or t1
        return h_info or {}, a_info or {}
    return ev.get("home_team") or {}, ev.get("away_team") or {}


def get_foot_schedule_cached(team_id, league_slug="ligue-1", season_year=None):
    saison = str(season_year or CFG.saison_foot)
    cle = (str(team_id), str(league_slug), saison)
    cached = _CACHE_FOOT_SCHED.get(cle, CFG.cache_ttl_sec)
    if cached is not None:
        return cached

    fn = getattr(fb, "get_team_schedule", None)
    res = {"data": {"events": []}}
    if callable(fn):
        try:
            res = fn(team_id=cle[0], league_slug=cle[1], season_year=cle[2], competition_id=cle[1])
        except TypeError:
            try:
                res = fn(cle[0], cle[1], cle[2])
            except Exception as e:
                logger.debug("get_team_schedule fallback erreur (%s): %s", cle, e)
        except Exception as e:
            logger.warning("get_team_schedule(%s) erreur: %s", cle, e)

    _CACHE_FOOT_SCHED.set(cle, res)
    return res


def _parse_date_utc(date_str):
    if not date_str:
        return None
    s = str(date_str).strip()
    try:
        if len(s) == 10 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _formater_date_locale(date_str):
    dt = _parse_date_utc(date_str)
    if dt is None:
        return str(date_str or "")[:16].replace("T", " ")
    return dt.astimezone(TZ_PARIS).strftime("%Y-%m-%d %H:%M")


def _cle_tri_date_match(item):
    dt = _parse_date_utc(item.get("date_match_iso") or item.get("date_match"))
    if dt is not None:
        return (0, dt.timestamp())
    raw = str(item.get("date_match") or "").strip()
    return (1, float("inf")) if not raw else (1, 0.0)


def _cle_dedup_evenement(ev, fallback_idx=0):
    eid = str(ev.get("id") or "").strip()
    if eid:
        return f"id:{eid}"
    h_info, a_info = _normaliser_home_away(ev)
    h_nom = str(h_info.get("short_name") or h_info.get("name") or "").strip().lower()
    a_nom = str(a_info.get("short_name") or a_info.get("name") or "").strip().lower()
    dt = _parse_date_utc(ev.get("start_time") or ev.get("date"))
    dt_bucket = dt.strftime("%Y-%m-%d-%H") if dt else str(ev.get("start_time") or ev.get("date") or "")[:13]
    equipes = tuple(sorted(x for x in (h_nom, a_nom) if x))
    if len(equipes) == 2:
        return f"match:{dt_bucket}:{equipes[0]}:{equipes[1]}"
    return f"fallback:{fallback_idx}"


def _est_match_a_venir_dans_fenetre(ev, heures_max):
    statut = str(ev.get("status") or "").strip().lower()
    if statut in STATUTS_EXCLUS_AVENIR:
        return False

    date_str = str(ev.get("start_time") or ev.get("date") or "").strip()
    dt = _parse_date_utc(date_str)
    if dt is None:
        return False

    maintenant = datetime.now(timezone.utc)
    return (maintenant - timedelta(minutes=15)) <= dt <= (maintenant + timedelta(hours=heures_max))


def poisson_prob(k, lam):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    if k < 0:
        return 0.0
    return math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1))


def simuler_poisson_foot(lam_h, lam_a):
    max_buts = 11
    p_h = p_n = p_a = p_over_25 = 0.0
    best_h = best_a = 0
    max_pr = -1.0
    matrice_scores = {}

    for h in range(max_buts):
        pr_h = poisson_prob(h, lam_h)
        for a in range(max_buts):
            pr = pr_h * poisson_prob(a, lam_a)
            matrice_scores[(h, a)] = pr
            if pr > max_pr:
                max_pr = pr
                best_h, best_a = h, a

            if h > a:
                p_h += pr
            elif h == a:
                p_n += pr
            else:
                p_a += pr
            if (h + a) > 2:
                p_over_25 += pr

    tot = p_h + p_n + p_a
    if tot <= 0:
        return 1 / 3, 1 / 3, 1 / 3, 0.5, 0.5, "1 - 1", 1, 1, {(1, 1): 0.12}
    p_h, p_n, p_a = p_h / tot, p_n / tot, p_a / tot
    p_ov25_norm = min(max(p_over_25, 0.0), 1.0)
    return p_h, p_n, p_a, p_ov25_norm, 1.0 - p_ov25_norm, f"{best_h} - {best_a}", best_h, best_a, matrice_scores


def _rendre_score_coherent(choix, h_nom, a_nom, best_h, best_a):
    h_sc, a_sc = int(best_h), int(best_a)
    c_l = str(choix or "").strip().lower()
    h_l = str(h_nom or "").strip().lower()
    a_l = str(a_nom or "").strip().lower()

    if c_l == "match nul":
        if h_sc != a_sc:
            h_sc = a_sc = max(1, min(h_sc, a_sc))
    elif c_l.startswith("victoire "):
        cible = c_l.replace("victoire ", "", 1).strip()
        if cible and (cible == h_l or cible in h_l or h_l in cible):
            if h_sc <= a_sc:
                h_sc = a_sc + 1
        elif cible and (cible == a_l or cible in a_l or a_l in cible):
            if a_sc <= h_sc:
                a_sc = h_sc + 1
    elif "+2.5" in c_l:
        if (h_sc + a_sc) < 3:
            if h_sc >= a_sc:
                h_sc = max(h_sc, 2)
                a_sc = max(a_sc, 1)
                if h_sc + a_sc < 3:
                    h_sc += 1
            else:
                a_sc = max(a_sc, 2)
                h_sc = max(h_sc, 1)
                if h_sc + a_sc < 3:
                    a_sc += 1
    elif "-2.5" in c_l:
        while (h_sc + a_sc) > 2:
            if h_sc >= a_sc and h_sc > 0:
                h_sc -= 1
            elif a_sc > 0:
                a_sc -= 1
            else:
                break

    return f"{h_sc} - {a_sc}", h_sc, a_sc


def _calculer_proba_score_exact(score_str, matrice_scores, lam_h, lam_a):
    if not score_str or "-" not in str(score_str):
        return None
    try:
        parts = [int(x.strip()) for x in str(score_str).split("-", 1)]
        h_sc, a_sc = parts[0], parts[1]
    except Exception:
        return None

    if matrice_scores and (h_sc, a_sc) in matrice_scores:
        pr = float(matrice_scores[(h_sc, a_sc)])
    else:
        pr = poisson_prob(h_sc, lam_h) * poisson_prob(a_sc, lam_a)

    return round(max(0.5, min(95.0, pr * 100.0)), 1)


def moyenne_ponderee_shrinkage(valeurs, prior_moyenne, prior_poids=1.5):
    if not valeurs:
        return prior_moyenne
    n = len(valeurs)
    poids = [1.0 + (i * 0.25) for i in range(n)]
    somme_poids = sum(poids)
    moy_obs = sum(v * p for v, p in zip(valeurs, poids)) / somme_poids
    return ((moy_obs * somme_poids) + (prior_moyenne * prior_poids)) / (somme_poids + prior_poids)


def _calculer_indice_confiance(proba_pct, fiable, gravite_alerte=0.0, est_nul=False):
    score = proba_pct
    if not fiable:
        score -= 10.0
    if gravite_alerte > 0.0:
        score -= min(gravite_alerte * 6.0, 14.0)
    if est_nul:
        score -= 8.0
    score = round(max(15.0, min(95.0, score)), 1)
    if fiable and gravite_alerte < 1.0 and not est_nul and score >= 65.0 and proba_pct >= 62.0:
        niveau = "HAUTE"
    elif score >= 48.0:
        niveau = "MOYENNE"
    else:
        niveau = "FAIBLE"
    return score, niveau


def _calculer_cote(proba_pct, marge=0.04):
    p = max(0.05, min(0.95, float(proba_pct or 50.0) / 100.0))
    fair_odds = 1.0 / p
    cote_broker = fair_odds * (1.0 - marge)
    return max(1.05, round(cote_broker, 2))


def _calculer_cote_min_et_mise(proba_pct, conf_score, fiable, est_nul=False):
    p = max(0.05, min(0.95, proba_pct / 100.0))
    marge_facteur = 1.0 + (CFG.marge_value_pct / 100.0)
    cote_min = round((1.0 / p) * marge_facteur, 2)

    if CFG.mise_unite_defaut <= 0:
        return cote_min, 0.0

    if not fiable or proba_pct < 50.0:
        mise = round(CFG.mise_unite_defaut * 0.25, 2)
    else:
        facteur_conf = max(0.35, min(1.35, conf_score / 60.0))
        facteur_nul = 0.65 if est_nul else 1.0
        mise = round(
            min(CFG.mise_max_unites, max(0.25, CFG.mise_unite_defaut * facteur_conf * facteur_nul)),
            2,
        )
    return cote_min, mise


def _normaliser_nom_equipe(nom):
    s = unicodedata.normalize("NFKD", str(nom or "")).encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"\b(fc|sc|ac|as|rc|rcd|cf|ud|cd|afc|ssc|sl|sd)\b", " ", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return " ".join(s.split())


def _memes_equipes(nom_a, nom_b):
    na = _normaliser_nom_equipe(nom_a)
    nb = _normaliser_nom_equipe(nom_b)
    if not na or not nb:
        return False
    return na == nb or (len(na) >= 4 and (na in nb or nb in na))


def charger_cotes_moyennes_foot(competitions):
    if not CFG.odds_api_key:
        return {}

    cotes_map = {}
    for comp in competitions:
        slug = comp.get("slug")
        label = comp.get("label")
        sport_key = MAP_COMP_ODDS_API.get(
