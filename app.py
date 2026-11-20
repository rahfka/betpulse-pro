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


bt = _charger_module_sport("compétences_sportives.paris", "sports_skills.betting")
fb = _charger_module_sport("compétences_sportives.football", "sports_skills.football")
tn = _charger_module_sport("compétences_sportives.tennis", "sports_skills.tennis")

TZ_PARIS = ZoneInfo("Europe/Paris")

STATUTS_TERMINES = {
    "closed", "final", "post", "completed", "finished",
    "ft", "aet", "pen", "status_final", "status_post",
}
STATUTS_TERMINES_OU_EN_COURS = STATUTS_TERMINES | {"in_progress", "live"}

MOTS_ALERTE_FORT = (
    "forfait", "blessure", "blessé", "blessée", "opéré", "opérée",
    "rupture", "absent", "absente", "suspendu", "out", "injured",
)
MOTS_ALERTE_FAIBLE = ("incertain", "incertaine", "doute", "gêne", "repris", "doubtful", "knock")


@dataclass(frozen=True)
class Config:
    saison_foot: str = field(default_factory=lambda: (os.getenv("SAISON_FOOT") or "2026").strip())
    n_derniers_matchs: int = field(default_factory=lambda: max(1, int(os.getenv("N_DERNIERS_MATCHS", "8"))))
    fenetre_heures: int = field(default_factory=lambda: max(1, int(os.getenv("FENETRE_HEURES", "48"))))
    timeout_http: float = field(default_factory=lambda: max(1.0, float(os.getenv("TIMEOUT_HTTP", "4"))))
    cache_ttl_sec: int = field(default_factory=lambda: max(60, int(os.getenv("CACHE_TTL_SEC", "900"))))
    mise_unite_defaut: float = field(default_factory=lambda: max(0.0, float(os.getenv("MISE_UNITE_DEFAUT", "1.0"))))
    auto_refresh_sec: int = field(default_factory=lambda: max(0, int(os.getenv("AUTO_REFRESH_SEC", "0"))))
    activer_debug_routes: bool = field(
        default_factory=lambda: os.getenv("ACTIVER_DEBUG_ROUTES", "false").strip().lower() in ("1", "true", "yes")
    )


CFG = Config()


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
    if statut in STATUTS_TERMINES_OU_EN_COURS:
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
    for h in range(max_buts):
        pr_h = poisson_prob(h, lam_h)
        for a in range(max_buts):
            pr = pr_h * poisson_prob(a, lam_a)
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
        return 1 / 3, 1 / 3, 1 / 3, 0.5, 0.5
    p_h, p_n, p_a = p_h / tot, p_n / tot, p_a / tot
    p_ov25_norm = min(max(p_over_25, 0.0), 1.0)
    return p_h, p_n, p_a, p_ov25_norm, 1.0 - p_ov25_norm


def moyenne_ponderee_shrinkage(valeurs, prior_moyenne, prior_poids=3.0):
    if not valeurs:
        return prior_moyenne
    n = len(valeurs)
    poids = [1.0 + (i * 0.25) for i in range(n)]
    somme_poids = sum(poids)
    moy_obs = sum(v * p for v, p in zip(valeurs, poids)) / somme_poids
    return ((moy_obs * somme_poids) + (prior_moyenne * prior_poids)) / (somme_poids + prior_poids)


def _calculer_indice_confiance(proba_pct, fiable, gravite_alerte=0.0):
    score = proba_pct
    if not fiable:
        score -= 15.0
    if gravite_alerte > 0.0:
        score -= min(gravite_alerte * 10.0, 22.0)
    score = round(max(10.0, min(95.0, score)), 1)
    if fiable and gravite_alerte == 0.0 and score >= 60.0:
        niveau = "HAUTE"
    elif score >= 49.0:
        niveau = "MOYENNE"
    else:
        niveau = "FAIBLE"
    return score, niveau


def _extraire_scores_evenement(ev):
    comps = ev.get("competitors")
    if isinstance(comps, dict):
        comps_liste = [
            {**comps.get("home", {}), "qualifier": "home"},
            {**comps.get("away", {}), "qualifier": "away"},
        ]
    elif isinstance(comps, list):
        comps_liste = comps
    else:
        comps_liste = []

    res = []
    for idx_c, c in enumerate(comps_liste):
        if not isinstance(c, dict):
            continue
        t_obj = c.get("team") if isinstance(c.get("team"), dict) else c
        tid = str(t_obj.get("id") or c.get("id") or "")
        tnm = str(t_obj.get("name") or t_obj.get("short_name") or c.get("name") or "").strip()
        qual = str(c.get("qualifier") or c.get("homeAway") or "").strip().lower()
        if not qual:
            qual = "home" if idx_c == 0 else "away"
        sc = c.get("score")
        sc_int = None
        if sc is not None and str(sc).strip() != "":
            try:
                sc_int = int(float(sc))
            except Exception:
                sc_int = None
        winner = c.get("winner")
        res.append({
            "id": tid,
            "name": tnm,
            "qualifier": qual,
            "score": sc_int,
            "winner": winner,
        })
    return res


def stats_foot_detail(team_id, team_name="", events_override=None, n_derniers=8):
    events = list(events_override or [])
    joues = [ev for ev in events if str(ev.get("status", "")).lower() in STATUTS_TERMINES]
    joues.sort(key=lambda ev: str(ev.get("start_time") or ev.get("date") or ""))

    bp_all, bc_all = [], []
    bp_home, bc_home = [], []
    bp_away, bc_away = [], []
    v = n = d = 0
    nom_l = (team_name or "").strip().lower()

    for ev in reversed(joues):
        scores_par_role = []
        moi = None
        mon_role = ""
        for item in _extraire_scores_evenement(ev):
            sc_int = item["score"]
            if sc_int is None:
                continue
            tid = item["id"]
            tnm_l = item["name"].lower()
            qual = item["qualifier"]
            scores_par_role.append((tid, tnm_l, qual, sc_int))
            est_moi = (team_id and tid and tid == str(team_id)) or (
                nom_l and tnm_l and (tnm_l == nom_l or nom_l in tnm_l or tnm_l in nom_l)
            )
            if est_moi and moi is None:
                moi = sc_int
                mon_role = qual

        adv = None
        if moi is not None and len(scores_par_role) >= 2:
            for tid, tnm_l, _, sc_int in scores_par_role:
                if not ((team_id and tid and tid == str(team_id)) or (nom_l and tnm_l and tnm_l == nom_l)):
                    adv = sc_int
                    break
            if adv is None:
                for _, _, _, sc_int in scores_par_role:
                    if sc_int != moi:
                        adv = sc_int
                        break
                if adv is None:
                    adv = moi

        if moi is not None and adv is not None:
            bp_all.append(moi)
            bc_all.append(adv)
            if mon_role == "home":
                bp_home.append(moi)
                bc_home.append(adv)
            elif mon_role == "away":
                bp_away.append(moi)
                bc_away.append(adv)

            if moi > adv:
                v += 1
            elif moi == adv:
                n += 1
            else:
                d += 1
        if len(bp_all) >= n_derniers:
            break

    bp_all.reverse()
    bc_all.reverse()
    bp_home.reverse()
    bc_home.reverse()
    bp_away.reverse()
    bc_away.reverse()
    nb_matchs = v + n + d
    a_historique = nb_matchs >= 3

    return {
        "forme": f"{v}V-{n}N-{d}D" if nb_matchs > 0 else "Forme récente",
        "a_historique": a_historique,
        "nb_matchs": nb_matchs,
        "bp": moyenne_ponderee_shrinkage(bp_all, 1.30, prior_poids=3.0),
        "bc": moyenne_ponderee_shrinkage(bc_all, 1.25, prior_poids=3.0),
        "bp_home": moyenne_ponderee_shrinkage(bp_home, 1.40, prior_poids=2.5),
        "bc_home": moyenne_ponderee_shrinkage(bc_home, 1.15, prior_poids=2.5),
        "bp_away": moyenne_ponderee_shrinkage(bp_away, 1.15, prior_poids=2.5),
        "bc_away": moyenne_ponderee_shrinkage(bc_away, 1.35, prior_poids=2.5),
    }


def _evaluer_gravite_titres(titres):
    gravite = 0.0
    for t in titres or []:
        tl = str(t).lower()
        if any(m in tl for m in MOTS_ALERTE_FORT):
            gravite += 1.0
        elif any(m in tl for m in MOTS_ALERTE_FAIBLE):
            gravite += 0.5
    return min(gravite, 2.5)


def _requete_rss(requete_brute, max_titres=2):
    q = urllib.parse.quote(requete_brute)
    url = f"https://news.google.com/rss/search?q={q}&hl=fr&gl=FR&ceid=FR:fr"
    titres = []
    try:
        r = _HTTP_SESSION.get(url, timeout=CFG.timeout_http)
        if r.status_code == 200 and r.text:
            root = ET.fromstring(r.text)
            for item in root.findall(".//item")[:4]:
                t = (item.findtext("title") or "").strip()
                if t:
                    titres.append(t)
    except Exception as e:
        logger.debug("Erreur RSS (%s): %s", requete_brute, e)
    return titres[:max_titres]


def scanner_sante_cible(cible, max_titres=2):
    cle = (str(cible).strip().lower(), max_titres)
    cached = _CACHE_SANTE.get(cle, CFG.cache_ttl_sec)
    if cached is not None:
        return list(cached)

    requete = f'"{cible}" (blessure OR forfait OR incertain OR absent) (match OR équipe OR foot OR tennis OR ATP OR WTA) when:3d'
    res = _requete_rss(requete, max_titres)
    _CACHE_SANTE.set(cle, res)
    return list(res)


def _construire_entree_foot(ev, label_comp, events_comp):
    ev_id = str(ev.get("id") or "")
    start_str = str(ev.get("start_time") or ev.get("date") or "")
    h_info, a_info = _normaliser_home_away(ev)
    h_nom = h_info.get("short_name") or h_info.get("name") or "Home"
    a_nom = a_info.get("short_name") or a_info.get("name") or "Away"

    st_h = stats_foot_detail(h_info.get("id", ""), h_nom, events_comp, CFG.n_derniers_matchs)
    st_a = stats_foot_detail(a_info.get("id", ""), a_nom, events_comp, CFG.n_derniers_matchs)
    fiable = st_h["a_historique"] and st_a["a_historique"]

    h_att = (0.60 * st_h["bp"]) + (0.40 * st_h["bp_home"])
    h_def = (0.60 * st_h["bc"]) + (0.40 * st_h["bc_home"])
    a_att = (0.60 * st_a["bp"]) + (0.40 * st_a["bp_away"])
    a_def = (0.60 * st_a["bc"]) + (0.40 * st_a["bc_away"])

    lam_h = max(((h_att * 1.08) + a_def) / 2.0, 0.35)
    lam_a = max(((a_att * 0.92) + h_def) / 2.0, 0.35)
    p_h, p_n, p_a, p_ov25, p_un25 = simuler_poisson_foot(lam_h, lam_a)

    choix, prob = max(
        [(f"Victoire {h_nom}", p_h), ("Match nul", p_n), (f"Victoire {a_nom}", p_a)],
        key=lambda x: x[1],
    )

    plafond = 0.80 if fiable else 0.64
    prob_cal = min(max(prob, 0.05), plafond)
    proba_pct = round(prob_cal * 100, 1)

    buts_label = "+2.5 buts" if p_ov25 >= p_un25 else "-2.5 buts"
    buts_proba = round(min(max(p_ov25, p_un25), 0.85) * 100, 1)

    prudence = "ELEVE" if (proba_pct >= 72 and fiable) else ("MODERE" if proba_pct >= 56 else "PRUDENT")
    conf_score, conf_niveau = _calculer_indice_confiance(proba_pct, fiable, 0.0)

    return {
        "sport": "FOOT",
        "competition": label_comp,
        "fiable": fiable,
        "event_id": ev_id,
        "match": f"{h_nom} vs {a_nom}",
        "date_match": _formater_date_locale(start_str),
        "detail": (
            f"[{label_comp}] {h_nom} [{st_h['forme']}] vs {a_nom} [{st_a['forme']}] "
            f"| Buts: {buts_label} ({buts_proba:.0f}%)"
        ),
        "choix": choix,
        "proba": proba_pct,
        "prudence": prudence,
        "confiance": conf_niveau,
        "confiance_score": conf_score,
        "mise_u": CFG.mise_unite_defaut,
        "equipes": [h_nom, a_nom],
    }


def analyser_foot_competitions(competitions, heures_max=48):
    resultats_a_venir = []
    alertes_globales = {}
    stats_par_comp = {}
    apercu_lignes = []
    evenements_termines = {}

    aujourdhui = datetime.now(timezone.utc).date()

    taches_sb = [
        (comp["slug"], (aujourdhui + timedelta(days=delta)).strftime("%Y%m%d"))
        for comp in competitions
        for delta in range(2)
    ]

    def charger_sb(args):
        slug, jour = args
        try:
            return _extraire_events(fb.get_fixtures_by_date(slug, jour))
        except Exception as e:
            logger.debug("get_fixtures_by_date(%s, %s) erreur: %s", slug, jour, e)
            return []

    with ThreadPoolExecutor(max_workers=8) as ex:
        res_sb = list(ex.map(charger_sb, taches_sb))

    events_upcoming_par_slug = {c["slug"]: [] for c in competitions}
    for (slug, _), evs in zip(taches_sb, res_sb):
        events_upcoming_par_slug[slug].extend(evs)

    taches_hist_set = set()
    for comp in competitions:
        slug = comp["slug"]
        eqs = set(comp.get("equipes") or [])
        for ev in events_upcoming_par_slug[slug]:
            h, a = _normaliser_home_away(ev)
            if h.get("id"):
                eqs.add(str(h["id"]))
            if a.get("id"):
                eqs.add(str(a["id"]))
        for tid in eqs:
            taches_hist_set.add((str(tid), slug))

    taches_hist = list(taches_hist_set)

    def charger_hist(args):
        tid, slug = args
        try:
            sched = get_foot_schedule_cached(tid, slug, CFG.saison_foot)
            return _extraire_events(sched)
        except Exception as e:
            logger.debug("charger_hist(%s, %s) erreur: %s", tid, slug, e)
            return []

    with ThreadPoolExecutor(max_workers=10) as ex:
        res_hist = list(ex.map(charger_hist, taches_hist))

    events_hist_par_slug = {c["slug"]: [] for c in competitions}
    for (_, slug), evs in zip(taches_hist, res_hist):
        events_hist_par_slug[slug].extend(evs)

    def analyser_comp(comp):
        slug = comp["slug"]
        label_comp = comp["label"]

        raw_comp_events = events_hist_par_slug[slug] + events_upcoming_par_slug[slug]
        dedup_comp = {}
        for idx, ev in enumerate(raw_comp_events):
            cle = _cle_dedup_evenement(ev, idx)
            if cle not in dedup_comp:
                dedup_comp[cle] = ev

        events_comp = list(dedup_comp.values())
        a_venir_48h = [ev for ev in events_comp if _est_match_a_venir_dans_fenetre(ev, heures_max)]
        a_venir_48h.sort(key=lambda e: str(e.get("start_time") or e.get("date") or ""))

        clos_comp = {}
        for ev in events_comp:
            if str(ev.get("status") or "").strip().lower() in STATUTS_TERMINES:
                eid = str(ev.get("id") or "").strip()
                if eid:
                    clos_comp[eid] = ev

        res_comp = []
        apercu_comp = []

        for ev in a_venir_48h:
            entree = _construire_entree_foot(ev, label_comp, events_comp)
            res_comp.append(entree)

            if len(apercu_comp) < 2:
                h, a = _normaliser_home_away(ev)
                apercu_comp.append(
                    f"{slug} | {_formater_date_locale(ev.get('start_time') or ev.get('date'))} | "
                    f"{h.get('short_name') or h.get('name') or '?'} vs {a.get('short_name') or a.get('name') or '?'}"
                )

        return label_comp, res_comp, {}, len(clos_comp), len(events_comp), apercu_comp, clos_comp

    with ThreadPoolExecutor(max_workers=6) as ex:
        res_comps = list(ex.map(analyser_comp, competitions))

    for label_comp, res_comp, alertes_comp, nb_clos, total_events, apercu_comp, clos_comp in res_comps:
        resultats_a_venir.extend(res_comp)
        alertes_globales.update(alertes_comp)
        apercu_lignes.extend(apercu_comp)
        evenements_termines.update(clos_comp)
        stats_par_comp[label_comp] = f"À venir: {len(res_comp)} | Terminés: {nb_clos} (bruts: {total_events})"

    logger.info("Couverture Foot : %s", " • ".join(f"{k}: {v}" for k, v in stats_par_comp.items()))
    return resultats_a_venir, alertes_globales, stats_par_comp, "\n".join(apercu_lignes[:12]), evenements_termines


def trouver_rang(nom, rk_map):
    if not nom or not rk_map:
        return None
    nom_l = " ".join(nom.lower().strip().split())
    if nom_l in rk_map:
        return rk_map[nom_l]
    for k, v in rk_map.items():
        if len(nom_l) >= 5 and (nom_l in k or k in nom_l):
            return v
    return None


def analyser_tennis(heures_max=48):
    try:
        atp_rk = {
            " ".join(p["name"].lower().split()): p["rank"]
            for p in tn.get_rankings(tour="atp").get("data", {}).get("rankings", [])
        }
        wta_rk = {
            " ".join(p["name"].lower().split()): p["rank"]
            for p in tn.get_rankings(tour="wta").get("data", {}).get("rankings", [])
        }
        sb = tn.get_scoreboard()
    except Exception as e:
        logger.warning("Erreur récupération données tennis: %s", e)
        return [], {}, {}

    simples = []
    alertes_tennis = {}
    termines_tennis = {}

    for t in sb.get("data", {}).get("tournaments", []):
        t_name = t.get("name")
        for d in t.get("draws", []):
            if "Doubles" in d.get("name", ""):
                continue
            rk_map = atp_rk if "Men" in d.get("name", "") else wta_rk
            for m in d.get("matches", []):
                mid = str(m.get("id") or "").strip()
                statut = str(m.get("status") or "").strip().lower()
                if statut in STATUTS_TERMINES and mid:
                    termines_tennis[mid] = m
                if not _est_match_a_venir_dans_fenetre(m, heures_max):
                    continue
                comps = m.get("competitors", [])
                if len(comps) != 2:
                    continue
                j1, j2 = comps[0].get("name", ""), comps[1].get("name", "")
                if "TBD" in (j1, j2):
                    continue
                r1, r2 = trouver_rang(j1, rk_map), trouver_rang(j2, rk_map)
                complet = r1 is not None and r2 is not None
                diff = (math.log((r2 or 180) + 1) - math.log((r1 or 180) + 1)) * (0.68 if complet else 0.25)
                p1 = 1 / (1 + math.exp(-diff))
                favori, prob = (j1, p1) if p1 >= 0.5 else (j2, 1 - p1)
                plafond = 0.78 if complet else 0.60
                prob = min(max(prob, 0.50), plafond)

                proba_pct = round(max(0.05, prob) * 100, 1)
                conf_score, conf_niveau = _calculer_indice_confiance(proba_pct, complet, 0.0)
                simples.append({
                    "sport": "TENNIS",
                    "competition": t_name or "Tennis",
                    "fiable": complet,
                    "event_id": mid,
                    "match": f"{j1} (#{r1 or 'NC'}) vs {j2} (#{r2 or 'NC'})",
                    "date_match": _formater_date_locale(str(m.get("date", ""))),
                    "detail": f"{t_name}",
                    "choix": f"Victoire {favori}",
                    "proba": proba_pct,
                    "prudence": "MODERE" if (proba_pct >= 58 and complet) else "PRUDENT",
                    "confiance": conf_niveau,
                    "confiance_score": conf_score,
                    "mise_u": CFG.mise_unite_defaut,
                    "equipes": [favori],
                })
    return simples, alertes_tennis, termines_tennis


def _enrichir_sante_items(items_cibles, tous_les_items=None):
    if not items_cibles:
        return {}

    equipes_a_scanner = set()
    for it in items_cibles[:10]:
        for nom in it.get("equipes", []):
            if nom:
                equipes_a_scanner.add(nom)

    alertes_globales = {}
    gravites = {}

    def scanner(nom):
        try:
            return nom, scanner_sante_cible(nom, 1)
        except Exception:
            return nom, []

    with ThreadPoolExecutor(max_workers=6) as ex:
        for nom, titres in ex.map(scanner, equipes_a_scanner):
            if titres:
                alertes_globales[nom] = titres
                gravites[nom] = _evaluer_gravite_titres(titres)

    liste_a_maj = list(items_cibles)
    if tous_les_items:
        ids_vus = {id(x) for x in liste_a_maj}
        for x in tous_les_items:
            if id(x) not in ids_vus:
                liste_a_maj.append(x)
                ids_vus.add(id(x))

    for it in liste_a_maj:
        grav = sum(gravites.get(n, 0.0) for n in it.get("equipes", []))
        if grav > 0.0:
            penalite_proba = min(grav * 0.025, 0.09)
            it["proba"] = round(max(0.05, it["proba"] / 100 - penalite_proba) * 100, 1)
            it["prudence"] = "MODERE" if (it["proba"] >= 62 and grav < 1.0) else "PRUDENT (alerte)"
        conf_score, conf_niveau = _calculer_indice_confiance(
            it.get("proba", 50.0),
            bool(it.get("fiable", True)),
            grav,
        )
        it["confiance"] = conf_niveau
        it["confiance_score"] = conf_score

    return alertes_globales


def _calculer_bilan(lignes):
    verifies = [r for r in lignes if r.get("statut") in ("GAGNE", "PERDU")]
    gagnes = [r for r in verifies if r.get("statut") == "GAGNE"]
    mises = 0.0
    pnl = 0.0
    for r in verifies:
        try:
            m = float(r.get("mise_u") or 0.0)
        except Exception:
            m = 0.0
        mises += m
        if r.get("statut") == "GAGNE":
            pnl += m
        elif r.get("statut") == "PERDU":
            pnl -= m
    taux = round((len(gagnes) / len(verifies)) * 100, 1) if verifies else 0.0
    roi = round((pnl / mises) * 100, 1) if mises > 0 else 0.0
    return {
        "total": len(lignes),
        "en_attente": sum(1 for r in lignes if r.get("statut") == "EN_ATTENTE"),
        "verifies": len(verifies),
        "gagnes": len(gagnes),
        "taux": taux,
        "mises_engagees": round(mises, 2),
        "pnl_total": round(pnl, 2),
        "roi_pct": roi,
    }


def _resoudre_statut_depuis_evenement(row, ev):
    if not ev:
        return None
    sport = str(row.get("sport") or "").upper()
    choix_l = str(row.get("choix") or "").strip().lower()
    scores = _extraire_scores_evenement(ev)

    if sport == "FOOT":
        h_sc = a_sc = None
        h_nm = a_nm = ""
        for item in scores:
            if item["qualifier"] == "home":
                h_sc = item["score"]
                h_nm = item["name"].lower()
            elif item["qualifier"] == "away":
                a_sc = item["score"]
                a_nm = item["name"].lower()
        if len(scores) >= 2 and (h_sc is None or a_sc is None):
            h_sc = scores[0]["score"]
            h_nm = scores[0]["name"].lower()
            a_sc = scores[1]["score"]
            a_nm = scores[1]["name"].lower()
        if h_sc is None or a_sc is None:
            return None
        if choix_l == "match nul":
            return "GAGNE" if h_sc == a_sc else "PERDU"
        if choix_l.startswith("victoire "):
            cible = choix_l.replace("victoire ", "", 1).strip()
            if cible and h_nm and (cible == h_nm or cible in h_nm or h_nm in cible):
                return "GAGNE" if h_sc > a_sc else "PERDU"
            if cible and a_nm and (cible == a_nm or cible in a_nm or a_nm in cible):
                return "GAGNE" if a_sc > h_sc else "PERDU"
        return None

    if sport == "TENNIS" and choix_l.startswith("victoire "):
        cible = choix_l.replace("victoire ", "", 1).strip()
        if not cible:
            return None
        for item in scores:
            nm = item["name"].lower()
            if nm and (cible == nm or cible in nm or nm in cible):
                if item["winner"] is True:
                    return "GAGNE"
                if item["winner"] is False:
                    return "PERDU"
        if len(scores) == 2 and scores[0]["score"] is not None and scores[1]["score"] is not None:
            s0, s1 = scores[0]["score"], scores[1]["score"]
            if s0 != s1:
                gagnant = scores[0]["name"].lower() if s0 > s1 else scores[1]["name"].lower()
                return "GAGNE" if (cible == gagnant or cible in gagnant or gagnant in cible) else "PERDU"
    return None


def mettre_a_jour_historique(items, evenements_termines=None):
    champs = [
        "date_analyse", "sport", "event_id", "match", "date_match",
        "choix", "proba", "prudence", "mise_u", "statut",
    ]
    clos_map = evenements_termines or {}

    with _CSV_LOCK:
        lignes = []
        cles = set()
        if os.path.exists(FICHIER_HISTORIQUE):
            try:
                with open(FICHIER_HISTORIQUE, "r", newline="", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        if any((v or "").strip() for v in row.values()):
                            clean_row = {k: (row.get(k) or "").strip() for k in champs}
                            if not clean_row["statut"]:
                                clean_row["statut"] = "EN_ATTENTE"
                            lignes.append(clean_row)
                            cles.add(f"{clean_row['sport']}_{clean_row['event_id']}_{clean_row['choix']}")
            except Exception as e:
                logger.warning("Erreur lecture historique CSV: %s", e)

        for r in lignes:
            if r.get("statut") == "EN_ATTENTE":
                ev_clos = clos_map.get(str(r.get("event_id") or "").strip())
                nouveau_statut = _resoudre_statut_depuis_evenement(r, ev_clos)
                if nouveau_statut in ("GAGNE", "PERDU"):
                    r["statut"] = nouveau_statut

        now_str = datetime.now(TZ_PARIS).strftime("%Y-%m-%d %H:%M")
        for p in items:
            cle = f"{p['sport']}_{p['event_id']}_{p['choix']}"
            if cle not in cles:
                lignes.append({
                    "date_analyse": now_str,
                    "sport": str(p["sport"]),
                    "event_id": str(p["event_id"]),
                    "match": str(p["match"]),
                    "date_match": str(p["date_match"]),
                    "choix": str(p["choix"]),
                    "proba": str(p["proba"]),
                    "prudence": str(p["prudence"]),
                    "mise_u": str(p.get("mise_u", CFG.mise_unite_defaut)),
                    "statut": "EN_ATTENTE",
                })
                cles.add(cle)

        try:
            os.makedirs(os.path.dirname(FICHIER_HISTORIQUE) or ".", exist_ok=True)
            tmp_csv = f"{FICHIER_HISTORIQUE}.tmp"
            with open(tmp_csv, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=champs)
                writer.writeheader()
                for r in lignes:
                    writer.writerow({k: r.get(k, "") for k in champs})
            os.replace(tmp_csv, FICHIER_HISTORIQUE)
        except Exception as e:
            logger.warning("Erreur écriture historique CSV: %s", e)

        return _calculer_bilan(lignes)


def definir_statut_historique(sport, event_id, choix, statut):
    statut_norm = str(statut or "").strip().upper()
    if statut_norm not in ("GAGNE", "PERDU", "EN_ATTENTE"):
        return None
    champs = [
        "date_analyse", "sport", "event_id", "match", "date_match",
        "choix", "proba", "prudence", "mise_u", "statut",
    ]
    cible_cle = f"{str(sport or '').strip().upper()}_{str(event_id or '').strip()}_{str(choix or '').strip()}"

    with _CSV_LOCK:
        lignes = []
        if os.path.exists(FICHIER_HISTORIQUE):
            try:
                with open(FICHIER_HISTORIQUE, "r", newline="", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        if any((v or "").strip() for v in row.values()):
                            lignes.append({k: (row.get(k) or "").strip() for k in champs})
            except Exception as e:
                logger.warning("Erreur lecture historique pour mise à jour statut: %s", e)
                return None

        modifie = False
        for r in lignes:
            cle_r = f"{r.get('sport', '').upper()}_{r.get('event_id', '')}_{r.get('choix', '')}"
            if cle_r == cible_cle:
                r["statut"] = statut_norm
                modifie = True

        if not modifie:
            return None

        try:
            tmp_csv = f"{FICHIER_HISTORIQUE}.tmp"
            with open(tmp_csv, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=champs)
                writer.writeheader()
                for r in lignes:
                    writer.writerow({k: r.get(k, "") for k in champs})
            os.replace(tmp_csv, FICHIER_HISTORIQUE)
        except Exception as e:
            logger.warning("Erreur sauvegarde statut historique: %s", e)
            return None

        return _calculer_bilan(lignes)


def executer_analyse():
    global _DERNIER_RAPPORT, _ANALYSE_EN_COURS
    with _STATE_LOCK:
        if _ANALYSE_EN_COURS:
            return _DERNIER_RAPPORT
        _ANALYSE_EN_COURS = True

    try:
        logger.info("Analyse en cours (fenetre: %sh)...", CFG.fenetre_heures)
        clos_globaux = {}

        try:
            foot_res, alertes_f, stats_comp, apercu_bruts, clos_foot = analyser_foot_competitions(
                COMPETITIONS_CIBLES, CFG.fenetre_heures
            )
            clos_globaux.update(clos_foot)
        except Exception as e:
            logger.error("Erreur Foot: %s", e)
            foot_res, alertes_f, stats_comp, apercu_bruts = [], {}, {}, f"Erreur Foot: {e}"

        try:
            tennis_res, alertes_t, clos_tennis = analyser_tennis(CFG.fenetre_heures)
            clos_globaux.update(clos_tennis)
        except Exception as e:
            logger.error("Erreur Tennis: %s", e)
            tennis_res, alertes_t = [], {}

        foot_sorted = sorted(foot_res, key=lambda x: (x.get("confiance_score", x["proba"]), x["proba"]), reverse=True)
        tennis_sorted = sorted(tennis_res, key=lambda x: (x.get("confiance_score", x["proba"]), x["proba"]), reverse=True)
        tous_items = foot_sorted + tennis_sorted

        alertes_combines = {**alertes_f, **alertes_t}
        try:
            candidats_sante = sorted(
                tous_items,
                key=lambda x: (x.get("confiance_score", x["proba"]), x["proba"]),
                reverse=True,
            )[:12]
            alertes_sante = _enrichir_sante_items(candidats_sante, tous_les_items=tous_items)
            alertes_combines.update(alertes_sante)
        except Exception as e:
            logger.error("Erreur Santé: %s", e)

        foot_sorted = sorted(foot_res, key=lambda x: (x.get("confiance_score", x["proba"]), x["proba"]), reverse=True)
        tennis_sorted = sorted(tennis_res, key=lambda x: (x.get("confiance_score", x["proba"]), x["proba"]), reverse=True)
        top_sur = sorted(
            foot_sorted + tennis_sorted,
            key=lambda x: (x.get("confiance_score", x["proba"]), x["proba"]),
            reverse=True,
        )[:10]

        try:
            bilan = mettre_a_jour_historique(
                [x for x in top_sur if x.get("fiable", True)],
                evenements_termines=clos_globaux,
            )
        except Exception as e:
            logger.error("Erreur Historique: %s", e)
            with _STATE_LOCK:
                bilan = dict(_DERNIER_RAPPORT.get("bilan") or {})

        synthese = "\n".join(
            f"• [{x['sport']} | Confiance {x.get('confiance', 'MOYENNE')}] {x['match']} ({x['date_match']}) -> {x['choix']} ({x['proba']}%)"
            for x in top_sur[:8]
        ) or f"Aucun match à venir dans les prochaines {CFG.fenetre_heures}h."

        nouveau_rapport = {
            "mis_a_jour": datetime.now(TZ_PARIS).strftime("%Y-%m-%d %H:%M %Z"),
            "stats_comp": stats_comp,
            "apercu_bruts": apercu_bruts,
            "top_sur": top_sur,
            "foot": foot_sorted,
            "tennis": tennis_sorted,
            "alertes": alertes_combines,
            "bilan": bilan,
            "synthese": synthese,
        }

        with _STATE_LOCK:
            _DERNIER_RAPPORT = nouveau_rapport

        _sauvegarder_rapport_disque(nouveau_rapport)
        logger.info("Analyse terminée et rapport mis à jour.")
        return nouveau_rapport
    finally:
        with _STATE_LOCK:
            _ANALYSE_EN_COURS = False


app = Flask(__name__, static_folder="statique", static_url_path="/statique")

_HTML_HOME = """<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BetPulse Pro</title>
  <style>
    :root { --bg: #0f172a; --card: #1e293b; --text: #e2e8f0; --muted: #94a3b8; --accent: #38bdf8; }
    * { box-sizing: border-box; }
    body {
      font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
      margin: 0; min-height: 100vh;
      background: radial-gradient(circle at top, #1e293b 0%, var(--bg) 68%);
      color: var(--text); display: flex; align-items: center; justify-content: center; padding: 24px;
    }
    .center-wrap { width: 100%; max-width: 520px; text-align: center; }
    .logo-btn {
      display: inline-flex; flex-direction: column; align-items: center; justify-content: center; gap: 14px;
      background: var(--card); border: 2px solid rgba(56, 189, 248, 0.45); border-radius: 24px;
      padding: 36px 44px; color: var(--text); text-decoration: none;
      box-shadow: 0 18px 40px rgba(0, 0, 0, 0.35); transition: transform 0.18s ease, border-color 0.18s ease;
    }
    .logo-btn:hover { transform: translateY(-3px) scale(1.01); border-color: var(--accent); }
    .logo-mark {
      width: 84px; height: 84px; border-radius: 22px;
      background: linear-gradient(135deg, #38bdf8 0%, #0284c7 100%);
      color: #0f172a; display: flex; align-items: center; justify-content: center;
      font-size: 2rem; font-weight: 900;
    }
    .logo-title { font-size: 2rem; font-weight: 800; }
    .logo-title span { color: var(--accent); }
    .logo-cta { font-size: 0.92rem; color: var(--accent); font-weight: 600; }
    .status-note { margin-top: 18px; font-size: 0.85rem; color: var(--muted); }
  </style>
</head>
<body>
  <div class="center-wrap">
    <a href="/app" class="logo-btn" title="Ouvrir les matchs et pronostics">
      <div class="logo-mark">BP</div>
      <div class="logo-title">BetPulse <span>Pro</span></div>
      <div class="logo-cta">Appuyer pour voir les matchs &amp; pronostics →</div>
    </a>
    <div class="status-note">
      {% if en_cours %}
        Analyse en cours en arrière-plan…
      {% elif r.mis_a_jour %}
        Dernière analyse : {{ r.mis_a_jour }}
      {% else %}
        Initialisation des données…
      {% endif %}
    </div>
  </div>
</body>
</html>
"""

_HTML_LOADING = """<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BetPulse Pro — Chargement…</title>
  <style>
    body { background: #0f172a; color: #e2e8f0; font-family: sans-serif; text-align: center; padding: 80px 20px; }
    h1 { color: #38bdf8; }
    p { color: #94a3b8; }
    a { color: #38bdf8; text-decoration: none; display: inline-block; margin-top: 16px; }
    .loader { border: 4px solid #334155; border-top: 4px solid #38bdf8; border-radius: 50%; width: 42px; height: 42px; animation: spin 1s linear infinite; margin: 24px auto; }
    @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
  </style>
</head>
<body>
  <h1>BetPulse Pro</h1>
  <div class="loader"></div>
  <p id="status-msg">Récupération des matchs et pronostics en cours…</p>
  <a href="/">← Retour au logo</a>
  <script>
    async function verifierEtat() {
      try {
        const res = await fetch("/api/status", { cache: "no-store" });
        if (res.ok) {
          const data = await res.json();
          if (data.ready) {
            window.location.replace(window.location.href);
            return;
          }
        }
      } catch (e) {}
      setTimeout(verifierEtat, 2000);
    }
    verifierEtat();
  </script>
</body>
</html>
"""

_HTML_PAGE = """<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BetPulse Pro — Matchs &amp; Pronostics</title>
  <style>
    :root {
      --bg: #0b1220;
      --card: #131d31;
      --card-hover: #18253f;
      --text: #e2e8f0;
      --muted: #94a3b8;
      --accent: #38bdf8;
      --green: #10b981;
      --orange: #f59e0b;
      --red: #ef4444;
      --border: #24334d;
    }
    * { box-sizing: border-box; }
    body {
      font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
      margin: 0;
      padding: 20px;
      background: radial-gradient(circle at top right, #132238 0%, var(--bg) 55%);
      color: var(--text);
    }
    h1, h2 { color: var(--accent); margin: 0 0 14px 0; }
    h2 { font-size: 1.2rem; display: flex; align-items: center; gap: 8px; }
    .container { max-width: 1220px; margin: 0 auto; }
    .header {
      display: flex; justify-content: space-between; align-items: center;
      flex-wrap: wrap; gap: 14px; margin-bottom: 18px; padding-bottom: 16px;
      border-bottom: 1px solid var(--border);
    }
    .brand-link { display: inline-flex; align-items: center; gap: 12px; text-decoration: none; color: var(--text); }
    .brand-badge {
      width: 40px; height: 40px; border-radius: 12px;
      background: linear-gradient(135deg, #38bdf8 0%, #0284c7 100%);
      color: #0b1220; font-weight: 900; display: inline-flex; align-items: center; justify-content: center;
      font-size: 1.05rem;
    }
    .meta { color: var(--muted); font-size: 0.88em; }
    button, .btn, select {
      background: var(--accent); color: #0b1220; border: none; padding: 9px 15px;
      border-radius: 9px; cursor: pointer; font-weight: 700; text-decoration: none;
      display: inline-flex; align-items: center; gap: 6px; font-size: 0.88em;
      transition: transform 0.12s ease, opacity 0.15s ease, background 0.15s ease;
    }
    select { background: #0b1220; color: var(--text); border: 1px solid var(--border); font-weight: 600; }
    .btn-cat { background: var(--card); color: var(--text); border: 1px solid var(--border); font-weight: 600; }
    .btn-cat.active { background: var(--accent); color: #0b1220; border-color: var(--accent); font-weight: 700; }
    button:hover, .btn:hover { opacity: 0.92; transform: translateY(-1px); }
    button:disabled { opacity: 0.65; cursor: wait; transform: none; }
    .cat-bar { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }
    .toolbar {
      display: flex; flex-wrap: wrap; gap: 10px; align-items: center; justify-content: space-between;
      background: rgba(19, 29, 49, 0.75); border: 1px solid var(--border);
      padding: 10px 12px; border-radius: 12px; margin-bottom: 18px;
    }
    .search-input {
      background: #0b1220; color: var(--text); border: 1px solid var(--border);
      border-radius: 8px; padding: 8px 12px; font-size: 0.88em; min-width: 210px; flex: 1; max-width: 320px;
    }
    .search-input:focus { outline: none; border-color: var(--accent); }
    .filter-pills { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
    .pill-btn {
      background: #0b1220; color: var(--muted); border: 1px solid var(--border);
      padding: 6px 11px; border-radius: 20px; font-size: 0.8em; font-weight: 600;
    }
    .pill-btn.active { background: rgba(56, 189, 248, 0.18); color: var(--accent); border-color: var(--accent); }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 16px; margin-bottom: 26px; }
    .card {
      background: var(--card); padding: 16px; border-radius: 12px;
      border: 1px solid var(--border); border-left: 4px solid var(--accent);
      box-shadow: 0 6px 14px rgba(0,0,0,0.18);
      transition: transform 0.14s ease, background 0.14s ease, border-color 0.14s ease;
    }
    .card:hover { background: var(--card-hover); transform: translateY(-2px); }
    .card.HAUTE { border-left-color: var(--green); }
    .card.MOYENNE { border-left-color: var(--orange); }
    .card.FAIBLE { border-left-color: var(--red); }
    .card-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; margin-bottom: 8px; }
    .card-title { font-size: 1.06em; font-weight: 700; line-height: 1.3; }
    .badges-right { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
    .badge { font-size: 0.74em; padding: 3px 8px; border-radius: 999px; font-weight: 800; white-space: nowrap; }
    .badge.proba { background: rgba(56, 189, 248, 0.16); color: #7dd3fc; border: 1px solid rgba(56, 189, 248, 0.35); }
    .badge.HAUTE { background: rgba(16, 185, 129, 0.2); color: #6ee7b7; border: 1px solid rgba(16, 185, 129, 0.4); }
    .badge.MOYENNE { background: rgba(245, 158, 11, 0.2); color: #fcd34d; border: 1px solid rgba(245, 158, 11, 0.4); }
    .badge.FAIBLE { background: rgba(239, 68, 68, 0.2); color: #fca5a5; border: 1px solid rgba(239, 68, 68, 0.4); }
    .card-meta { font-size: 0.86em; color: var(--muted); margin-bottom: 10px; line-height: 1.45; }
    .card-meta strong { color: var(--text); }
    .conf-meter { margin: 8px 0 4px 0; }
    .conf-label { display: flex; justify-content: space-between; font-size: 0.76em; color: var(--muted); margin-bottom: 4px; }
    .meter-track { height: 6px; background: #0b1220; border-radius: 999px; overflow: hidden; }
    .meter-fill { height: 100%; border-radius: 999px; background: var(--accent); }
    .card.HAUTE .meter-fill { background: var(--green); }
    .card.MOYENNE .meter-fill { background: var(--orange); }
    .card.FAIBLE .meter-fill { background: var(--red); }
    .card-detail {
      font-size: 0.8em; color: #cbd5e1; background: #0b1220; border: 1px solid rgba(36, 51, 77, 0.8);
      padding: 8px 10px; border-radius: 8px; margin-top: 10px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      line-height: 1.4;
    }
    .card-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
    .mini-btn {
      background: #0b1220; color: var(--text); border: 1px solid var(--border);
      padding: 5px 9px; border-radius: 7px; font-size: 0.75em; font-weight: 600;
    }
    .mini-btn.win:hover { border-color: var(--green); color: #6ee7b7; }
    .mini-btn.loss:hover { border-color: var(--red); color: #fca5a5; }
    .panel { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; margin-bottom: 24px; }
    .stats-bar { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 16px; }
    .stat-item { background: var(--card); border: 1px solid var(--border); padding: 8px 13px; border-radius: 999px; font-size: 0.84em; }
    pre { white-space: pre-wrap; background: #0b1220; border: 1px solid var(--border); padding: 14px; border-radius: 8px; overflow-x: auto; font-size: 0.85em; margin: 0; line-height: 1.5; }
    .notice { background: rgba(56, 189, 248, 0.12); border: 1px solid rgba(56, 189, 248, 0.35); color: #bae6fd; padding: 10px 14px; border-radius: 10px; margin-bottom: 18px; font-size: 0.9em; }
    .toast {
      position: fixed; bottom: 18px; right: 18px; background: #1e293b; color: #f8fafc;
      border: 1px solid var(--accent); padding: 10px 14px; border-radius: 10px;
      font-size: 0.84em; font-weight: 600; box-shadow: 0 10px 24px rgba(0,0,0,0.4);
      opacity: 0; pointer-events: none; transition: opacity 0.2s ease; z-index: 99;
    }
    .toast.show { opacity: 1; }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div>
        <a href="/" class="brand-link" title="Retour au logo BetPulse Pro">
          <span class="brand-badge">BP</span>
          <h1 style="margin: 0;">BetPulse Pro</h1>
        </a><br>
        <span class="meta">Dernière mise à jour : {{ r.mis_a_jour or "En attente" }} • Fenêtre : {{ fenetre_heures }}h • <span id="compteur-cartes">Filtre actif</span></span>
      </div>
      <form method="post" action="/refresh" style="margin: 0;" onsubmit="document.getElementById('btn-refresh').disabled=true; document.getElementById('btn-refresh').innerText='Actualisation…';">
        <button id="btn-refresh" type="submit">↻ Actualiser</button>
      </form>
    </div>

    <div class="cat-bar">
      <a href="{{ url_for('app_view', cat='all') }}" class="btn btn-cat {% if cat == 'all' %}active{% endif %}">Tout</a>
      <a href="{{ url_for('app_view', cat='top') }}" class="btn btn-cat {% if cat == 'top' %}active{% endif %}">Top Pronostics ({{ r.top_sur|length }})</a>
      <a href="{{ url_for('app_view', cat='foot') }}" class="btn btn-cat {% if cat == 'foot' %}active{% endif %}">Football ({{ r.foot|length }})</a>
      <a href="{{ url_for('app_view', cat='tennis') }}" class="btn btn-cat {% if cat == 'tennis' %}active{% endif %}">Tennis ({{ r.tennis|length }})</a>
      <a href="{{ url_for('app_view', cat='alertes') }}" class="btn btn-cat {% if cat == 'alertes' %}active{% endif %}">Alertes ({{ r.get('alertes', {})|length }})</a>
      <a href="{{ url_for('app_view', cat='bilan') }}" class="btn btn-cat {% if cat == 'bilan' %}active{% endif %}">Bilan &amp; Synthèse</a>
    </div>

    <div class="toolbar">
      <input id="search-input" class="search-input" type="search" placeholder="Rechercher une équipe, un joueur, une compétition…" oninput="appliquerFiltres()">
      <div class="filter-pills">
        <button type="button" class="pill-btn active" data-conf="ALL" onclick="setConf('ALL', this)">Confiance : Tous</button>
        <button type="button" class="pill-btn" data-conf="HAUTE" onclick="setConf('HAUTE', this)">Haute</button>
        <button type="button" class="pill-btn" data-conf="MOYENNE" onclick="setConf('MOYENNE', this)">Moyenne</button>
        <button type="button" class="pill-btn" data-conf="FAIBLE" onclick="setConf('FAIBLE', this)">Faible</button>
        <select id="proba-min" onchange="appliquerFiltres()" title="Probabilité minimale">
          <option value="0">Proba min : Tous</option>
          <option value="55">≥ 55%</option>
          <option value="65">≥ 65%</option>
          <option value="75">≥ 75%</option>
        </select>
        <select id="sort-mode" onchange="trierCartes()" title="Tri des cartes">
          <option value="conf">Tri : Confiance</option>
          <option value="proba">Tri : Probabilité</option>
          <option value="date">Tri : Date match</option>
        </select>
      </div>
    </div>

    {% if en_cours %}
    <div class="notice">Mise à jour en arrière-plan en cours — les données affichées restent accessibles.</div>
    {% endif %}

    {% if cat in ('all', 'top') %}
    <h2>★ Top pronostics (Foot &amp; Tennis)</h2>
    <div class="grid">
      {% for it in r.top_sur %}
      {% set conf = it.get('confiance', 'MOYENNE') %}
      {% set conf_sc = it.get('confiance_score', it.proba) %}
      <div class="card {{ conf }}"
           data-conf="{{ conf }}"
           data-proba="{{ it.proba }}"
           data-conf-score="{{ conf_sc }}"
           data-date="{{ it.date_match }}"
           data-search="{{ (it.choix ~ ' ' ~ it.match ~ ' ' ~ it.competition ~ ' ' ~ (it.detail or ''))|lower }}">
        <div class="card-top">
          <div class="card-title">{{ it.choix }}</div>
          <div class="badges-right">
            <span class="badge proba">{{ it.proba }}%</span>
            <span class="badge {{ conf }}">{{ conf }}</span>
          </div>
        </div>
        <div class="card-meta">
          <strong>{{ it.match }}</strong><br>
          {{ it.date_match }} — {{ it.competition }}
        </div>
        <div class="conf-meter">
          <div class="conf-label"><span>Indice de confiance</span><span>{{ conf_sc }}/100</span></div>
          <div class="meter-track"><div class="meter-fill" style="width: {{ conf_sc }}%;"></div></div>
        </div>
        {% if it.detail %}
        <div class="card-detail">{{ it.detail }}</div>
        {% endif %}
        <div class="card-actions">
          <button type="button" class="mini-btn" onclick="copierProno(this)" data-copy="[{{ it.sport }}] {{ it.match }} ({{ it.date_match }}) -> {{ it.choix }} ({{ it.proba }}% | Confiance {{ conf }})">Copier</button>
          <button type="button" class="mini-btn win" onclick="majStatut('{{ it.sport }}', '{{ it.event_id }}', {{ it.choix|tojson }}, 'GAGNE')">✓ Gagné</button>
          <button type="button" class="mini-btn loss" onclick="majStatut('{{ it.sport }}', '{{ it.event_id }}', {{ it.choix|tojson }}, 'PERDU')">✕ Perdu</button>
        </div>
      </div>
      {% else %}
      <div class="card">Aucun pronostic disponible.</div>
      {% endfor %}
    </div>
    {% endif %}

    {% if cat in ('all', 'foot') %}
    <h2>⚽ Matchs de Foot à venir — {{ fenetre_heures }}h ({{ r.foot|length }})</h2>
    <div class="stats-bar">
      {% for k, v in r.get("stats_comp", {}).items() %}
        <span class="stat-item"><strong>{{ k }}</strong> : {{ v }}</span>
      {% endfor %}
    </div>
    <div class="grid">
      {% for it in r.foot %}
      {% set conf = it.get('confiance', 'MOYENNE') %}
      {% set conf_sc = it.get('confiance_score', it.proba) %}
      <div class="card {{ conf }}"
           data-conf="{{ conf }}"
           data-proba="{{ it.proba }}"
           data-conf-score="{{ conf_sc }}"
           data-date="{{ it.date_match }}"
           data-search="{{ (it.choix ~ ' ' ~ it.match ~ ' ' ~ it.competition ~ ' ' ~ (it.detail or ''))|lower }}">
        <div class="card-top">
          <div class="card-title">{{ it.choix }}</div>
          <div class="badges-right">
            <span class="badge proba">{{ it.proba }}%</span>
            <span class="badge {{ conf }}">{{ conf }}</span>
          </div>
        </div>
        <div class="card-meta">
          <strong>{{ it.match }}</strong><br>
          {{ it.date_match }}
        </div>
        <div class="conf-meter">
          <div class="conf-label"><span>Indice de confiance</span><span>{{ conf_sc }}/100</span></div>
          <div class="meter-track"><div class="meter-fill" style="width: {{ conf_sc }}%;"></div></div>
        </div>
        <div class="card-detail">{{ it.detail }}</div>
        <div class="card-actions">
          <button type="button" class="mini-btn" onclick="copierProno(this)" data-copy="[FOOT] {{ it.match }} ({{ it.date_match }}) -> {{ it.choix }} ({{ it.proba }}% | Confiance {{ conf }})">Copier</button>
        </div>
      </div>
      {% else %}
      <div class="card">Aucun match de foot à venir dans cette fenêtre.</div>
      {% endfor %}
    </div>
    {% endif %}

    {% if cat in ('all', 'tennis') %}
    <h2>🎾 Matchs de Tennis à venir — {{ fenetre_heures }}h ({{ r.tennis|length }})</h2>
    <div class="grid">
      {% for it in r.tennis %}
      {% set conf = it.get('confiance', 'MOYENNE') %}
      {% set conf_sc = it.get('confiance_score', it.proba) %}
      <div class="card {{ conf }}"
           data-conf="{{ conf }}"
           data-proba="{{ it.proba }}"
           data-conf-score="{{ conf_sc }}"
           data-date="{{ it.date_match }}"
           data-search="{{ (it.choix ~ ' ' ~ it.match ~ ' ' ~ it.competition ~ ' ' ~ (it.detail or ''))|lower }}">
        <div class="card-top">
          <div class="card-title">{{ it.choix }}</div>
          <div class="badges-right">
            <span class="badge proba">{{ it.proba }}%</span>
            <span class="badge {{ conf }}">{{ conf }}</span>
          </div>
        </div>
        <div class="card-meta">
          <strong>{{ it.match }}</strong><br>
          {{ it.date_match }} — {{ it.competition }}
        </div>
        <div class="conf-meter">
          <div class="conf-label"><span>Indice de confiance</span><span>{{ conf_sc }}/100</span></div>
          <div class="meter-track"><div class="meter-fill" style="width: {{ conf_sc }}%;"></div></div>
        </div>
        <div class="card-actions">
          <button type="button" class="mini-btn" onclick="copierProno(this)" data-copy="[TENNIS] {{ it.match }} ({{ it.date_match }}) -> {{ it.choix }} ({{ it.proba }}% | Confiance {{ conf }})">Copier</button>
        </div>
      </div>
      {% else %}
      <div class="card">Aucun match de tennis à venir dans cette fenêtre.</div>
      {% endfor %}
    </div>
    {% endif %}

    {% if cat in ('all', 'alertes') %}
    <h2>🩺 Alertes blessures / forfaits (3j)</h2>
    <div class="panel">
      {% for cible, titres in r.get("alertes", {}).items() %}
        <p style="margin-bottom: 10px;">
          <strong style="color: #fca5a5;">{{ cible }}</strong> :<br>
          <span style="font-size: 0.9em; color: #cbd5e1;">{{ titres | join(" | ") }}</span>
        </p>
      {% else %}
        <span class="meta">Aucune alerte récente.</span>
      {% endfor %}
    </div>
    {% endif %}

    {% if cat in ('all', 'bilan') %}
    <h2>📊 Bilan historique &amp; Synthèse</h2>
    <div class="stats-bar">
      <span class="stat-item">Total : <strong>{{ r.get("bilan", {}).get("total", 0) }}</strong></span>
      <span class="stat-item">En attente : <strong>{{ r.get("bilan", {}).get("en_attente", 0) }}</strong></span>
      <span class="stat-item">Vérifiés : <strong>{{ r.get("bilan", {}).get("verifies", 0) }}</strong></span>
      <span class="stat-item">Réussite : <strong>{{ r.get("bilan", {}).get("taux", 0.0) }}%</strong></span>
      <span class="stat-item">PnL : <strong>{{ r.get("bilan", {}).get("pnl_total", 0.0) }}u</strong></span>
    </div>
    <div class="panel">
      <pre>{{ r.synthese or "Aucune synthèse disponible pour le moment." }}</pre>
    </div>
    {% endif %}
  </div>

  <div id="toast" class="toast"></div>

  <script>
    let confActif = "ALL";
    function afficherToast(msg) {
      const el = document.getElementById("toast");
      if (!el) return;
      el.textContent = msg;
      el.classList.add("show");
      setTimeout(() => el.classList.remove("show"), 2200);
    }
    function setConf(val, btn) {
      confActif = val;
      document.querySelectorAll(".pill-btn").forEach(b => b.classList.remove("active"));
      if (btn) btn.classList.add("active");
      appliquerFiltres();
    }
    function appliquerFiltres() {
      const q = (document.getElementById("search-input")?.value || "").trim().toLowerCase();
      const probaMin = parseFloat(document.getElementById("proba-min")?.value || "0") || 0;
      let totalCartes = 0;
      let visibles = 0;
      document.querySelectorAll(".grid .card[data-conf]").forEach(card => {
        totalCartes += 1;
        const cConf = card.getAttribute("data-conf") || "";
        const cProba = parseFloat(card.getAttribute("data-proba") || "0") || 0;
        const cText = card.getAttribute("data-search") || "";
        const okConf = (confActif === "ALL") || (cConf === confActif);
        const okProba = cProba >= probaMin;
        const okText = !q || cText.includes(q);
        const show = okConf && okProba && okText;
        card.style.display = show ? "" : "none";
        if (show) visibles += 1;
      });
      const cpt = document.getElementById("compteur-cartes");
      if (cpt && totalCartes > 0) {
        cpt.textContent = `${visibles}/${totalCartes} affichés`;
      }
    }
    function trierCartes() {
      const mode = document.getElementById("sort-mode")?.value || "conf";
      document.querySelectorAll(".grid").forEach(grid => {
        const cards = Array.from(grid.querySelectorAll(".card[data-conf]"));
        if (cards.length < 2) return;
        cards.sort((a, b) => {
          if (mode === "proba") {
            return (parseFloat(b.dataset.proba) || 0) - (parseFloat(a.dataset.proba) || 0);
          }
          if (mode === "date") {
            return (a.dataset.date || "").localeCompare(b.dataset.date || "");
          }
          return (parseFloat(b.dataset.confScore) || 0) - (parseFloat(a.dataset.confScore) || 0);
        });
        cards.forEach(c => grid.appendChild(c));
      });
    }
    async function copierProno(btn) {
      const txt = btn?.getAttribute("data-copy") || "";
      if (!txt) return;
      try {
        await navigator.clipboard.writeText(txt);
        afficherToast("Pronostic copié !");
      } catch (e) {
        afficherToast("Copie impossible sur ce navigateur");
      }
    }
    async function majStatut(sport, eventId, choix, statut) {
      try {
        const res = await fetch("/api/historique/statut", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ sport, event_id: eventId, choix, statut })
        });
        if (res.ok) {
          afficherToast(`Statut mis à jour : ${statut}`);
        } else {
          afficherToast("Pronostic non trouvé dans l'historique");
        }
      } catch (e) {
        afficherToast("Erreur réseau");
      }
    }
    appliquerFiltres();
  </script>
</body>
</html>
"""


def _demarrer_analyse_si_besoin(delai_sec=0.0):
    def _runner():
        if delai_sec > 0:
            time.sleep(delai_sec)
        try:
            executer_analyse()
        except Exception as e:
            logger.critical("Erreur fatale analyse background: %s", e)

    threading.Thread(target=_runner, daemon=True).start()


def _demarrer_scheduler_si_actif():
    global _SCHEDULER_DEMARRE
    if CFG.auto_refresh_sec <= 0:
        return
    with _STATE_LOCK:
        if _SCHEDULER_DEMARRE:
            return
        _SCHEDULER_DEMARRE = True

    def _loop():
        while True:
            time.sleep(CFG.auto_refresh_sec)
            try:
                executer_analyse()
            except Exception as e:
                logger.warning("Erreur scheduler auto-refresh: %s", e)

    threading.Thread(target=_loop, daemon=True).start()


@app.route("/health")
def health():
    return "ok", 200


@app.route("/api/status")
def api_status():
    with _STATE_LOCK:
        maj = _DERNIER_RAPPORT.get("mis_a_jour")
        en_cours = _ANALYSE_EN_COURS
    if not maj and not en_cours:
        _demarrer_analyse_si_besoin(0.0)
    resp = make_response(jsonify({
        "ready": bool(maj),
        "mis_a_jour": maj,
        "en_cours": en_cours,
    }), 200)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/analyse")
def api_analyse():
    with _STATE_LOCK:
        rapport = dict(_DERNIER_RAPPORT)
    if not rapport.get("mis_a_jour"):
        rapport = executer_analyse()
    return jsonify(rapport)


@app.route("/api/historique/statut", methods=["POST"])
def api_historique_statut():
    payload = request.get_json(silent=True) or request.form or {}
    bilan = definir_statut_historique(
        payload.get("sport"),
        payload.get("event_id"),
        payload.get("choix"),
        payload.get("statut"),
    )
    if bilan is None:
        return jsonify({"ok": False, "error": "Entrée introuvable ou statut invalide"}), 404
    with _STATE_LOCK:
        _DERNIER_RAPPORT["bilan"] = bilan
        rapport_copie = dict(_DERNIER_RAPPORT)
    _sauvegarder_rapport_disque(rapport_copie)
    return jsonify({"ok": True, "bilan": bilan}), 200


if CFG.activer_debug_routes:
    @app.route("/api/debug-foot")
    def api_debug_foot():
        fonctions = [n for n in dir(fb) if not n.startswith("_") and callable(getattr(fb, n, None))]
        sched = get_foot_schedule_cached("160", "ligue-1", CFG.saison_foot)
        evs = _extraire_events(sched)
        echantillon = [
            {
                "id": ev.get("id"),
                "status": ev.get("status"),
                "date": ev.get("start_time") or ev.get("date"),
                "home": (_normaliser_home_away(ev)[0].get("name")),
                "away": (_normaliser_home_away(ev)[1].get("name")),
            }
            for ev in evs[:5]
        ]
        return jsonify({
            "module": getattr(fb, "__name__", str(fb)),
            "fonctions_disponibles": fonctions,
            "nb_events_psg": len(evs),
            "echantillon_psg": echantillon,
        })


@app.route("/refresh", methods=["POST"])
def refresh():
    _demarrer_analyse_si_besoin(0.0)
    return redirect(url_for("app_view"))


@app.route("/")
def index():
    with _STATE_LOCK:
        rapport = dict(_DERNIER_RAPPORT)
        en_cours = _ANALYSE_EN_COURS

    if not rapport.get("mis_a_jour") and not en_cours:
        _demarrer_analyse_si_besoin(0.2)

    resp = make_response(render_template_string(
        _HTML_HOME,
        r=rapport,
        en_cours=en_cours,
    ), 200)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/app")
def app_view():
    cat = (request.args.get("cat") or "all").strip().lower()
    if cat not in {"all", "top", "foot", "tennis", "alertes", "bilan"}:
        cat = "all"

    with _STATE_LOCK:
        rapport = dict(_DERNIER_RAPPORT)
        en_cours = _ANALYSE_EN_COURS

    if not rapport.get("mis_a_jour"):
        if not en_cours:
            _demarrer_analyse_si_besoin(0.0)
        resp = make_response(render_template_string(_HTML_LOADING), 200)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp

    resp = make_response(render_template_string(
        _HTML_PAGE,
        r=rapport,
        en_cours=en_cours,
        cat=cat,
        fenetre_heures=CFG.fenetre_heures,
    ), 200)
    resp.headers["Cache-Control"] = "no-store"
    return resp


_charger_rapport_disque()
_demarrer_scheduler_si_actif()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
