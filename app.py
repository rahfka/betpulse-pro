import csv
import math
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, render_template_string

import sports_skills.betting as bt
import sports_skills.football as fb
import sports_skills.tennis as tn

app = Flask(__name__, static_folder="static", static_url_path="/static")

# =====================================================================
# CONFIGURATION GENERALE (Render / Web App)
# =====================================================================
_DOSSIER_SCRIPT = os.path.dirname(os.path.abspath(__file__))
FICHIER_HISTORIQUE = os.path.join(_DOSSIER_SCRIPT, "historique_paris.csv")

EQUIPES_FOOT_IDS = ["160", "174", "176", "166"]  # PSG, Monaco, Marseille, Lille
LIGUE_SLUG = "ligue-1"
SAISON_FOOT = "2026"
N_DERNIERS_MATCHS = 10
FENETRE_HEURES = 72

FRACTION_KELLY = 0.25
MISE_MIN_UNITES = 0.25
MISE_MAX_UNITES = 2.0
SEUIL_EV_POSITIF_PCT = 3.0

_CACHE_FOOT_SCHED = {}
_DERNIER_SNAPSHOT = {
    "maj": None,
    "principaux": [],
    "filtres": [],
    "partiels": [],
    "doubles": [],
    "bilan": {
        "total": 0,
        "en_attente": 0,
        "verifies": 0,
        "gagnes": 0,
        "taux": 0.0,
        "mises_engagees": 0.0,
        "pnl_total": 0.0,
        "roi_pct": 0.0,
        "bilan_foot": "0/0 (0.0%)",
        "bilan_buts": "0/0 (0.0%)",
        "bilan_tennis": "0/0 (0.0%)",
    },
    "synthese": "Clique sur « Actualiser » pour lancer l'analyse et charger les pistes.",
}


def get_foot_schedule_cached(team_id, league_slug="ligue-1", season_year="2026"):
    cle = (str(team_id), str(league_slug), str(season_year))
    if cle not in _CACHE_FOOT_SCHED:
        _CACHE_FOOT_SCHED[cle] = fb.get_team_schedule(
            team_id=cle[0],
            league_slug=cle[1],
            season_year=cle[2],
            competition_id=cle[1],
        )
    return _CACHE_FOOT_SCHED[cle]


def poisson_prob(k, lam):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (math.exp(-lam) * (lam ** k)) / math.factorial(k)


def trouver_rang(nom, rk_map):
    if not nom or not rk_map:
        return None
    nom_l = " ".join(nom.lower().strip().split())
    if nom_l in rk_map:
        return rk_map[nom_l]

    parts = nom_l.split()
    nom_famille = parts[-1] if parts else nom_l
    candidats_famille = [
        v for k, v in rk_map.items()
        if len(nom_famille) >= 4 and re.search(rf"\b{re.escape(nom_famille)}\b", k)
    ]
    if len(candidats_famille) == 1:
        return candidats_famille[0]

    for k, v in rk_map.items():
        if len(nom_l) >= 5 and (nom_l in k or k in nom_l):
            return v
    return None


def dans_fenetre_heures(date_str, heures_max):
    if heures_max is None or not date_str:
        return True
    try:
        s = str(date_str).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        maintenant = datetime.now(timezone.utc)
        limite = maintenant + timedelta(hours=heures_max)
        return (maintenant - timedelta(hours=4)) <= dt <= limite
    except Exception:
        return True


def _extraire_noms_cles(entite):
    morceaux = re.findall(r"[A-Za-zÀ-ÿ0-9\-]{3,}", str(entite))
    if not morceaux:
        return []
    cles = {m.lower() for m in morceaux}
    cles.add(morceaux[-1].lower())
    return list(cles)


def _titre_concerne_vraiment_cible(titre_bas, cible, autres_cibles=None):
    cles_cible = _extraire_noms_cles(cible)
    if not cles_cible:
        return False

    pos_cible = None
    for cc in cles_cible:
        m = re.search(rf"\b{re.escape(cc)}\b", titre_bas)
        if m:
            pos_cible = m.start() if pos_cible is None else min(pos_cible, m.start())

    if pos_cible is None:
        return False

    if autres_cibles and any(
        w in titre_bas for w in ("bless", "forfait", "ischio", "genou", "cheville", "cuisse", "mollet", "injury")
    ):
        for autre in autres_cibles:
            if not autre or autre.lower() == cible.lower():
                continue
            for ca in _extraire_noms_cles(autre):
                if ca in cles_cible or len(ca) < 4:
                    continue
                m_autre = re.search(rf"\b{re.escape(ca)}\b", titre_bas)
                if m_autre and m_autre.start() < pos_cible:
                    return False

    return True


def _normaliser_titre_sante(titre):
    base = re.split(r"\s+[-–—|]\s+", titre.strip())[0].lower()
    base = re.sub(r"[^a-zà-ÿ0-9\s]", " ", base)
    mots = [
        m for m in base.split()
        if len(m) > 2 and m not in {"les", "des", "une", "pour", "avec", "sur", "dans", "après", "apres"}
    ]
    return set(mots)


def filtrer_alertes_uniques(alertes_dict):
    vus_exact = set()
    vus_ensembles = []
    nettoyees = {}

    for cible, titres in alertes_dict.items():
        retenus = []
        for t in titres:
            t_clean = " ".join(t.split()).strip()
            cle_exact = t_clean.lower()
            if cle_exact in vus_exact:
                continue

            mots_t = _normaliser_titre_sante(t_clean)
            doublon_similaire = False
            if mots_t:
                for prev_mots in vus_ensembles:
                    union = mots_t | prev_mots
                    if union and (len(mots_t & prev_mots) / len(union)) >= 0.70:
                        doublon_similaire = True
                        break

            if not doublon_similaire:
                vus_exact.add(cle_exact)
                vus_ensembles.append(mots_t)
                retenus.append(t_clean)

        if retenus:
            nettoyees[cible] = retenus

    return nettoyees


def scanner_sante_cible(cible, max_titres=3, autres_cibles=None):
    mots_alerte = [
        "bless", "forfait", "incertain", "infirmerie", "absent", "pépin", "pepin",
        "ischio", "genou", "cheville", "cuisse", "mollet", "suspend", "abandon",
        "injury", "injured", "doubt", "withdraw"
    ]
    mots_exclus = [
        "roland-garros", "roland garros", "rétrospective", "retrospective",
        "histoire", "archive", "2022", "2023", "2024", "2025"
    ]
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
        )
    }
    q = urllib.parse.quote(f'"{cible}" (blessure OR forfait OR incertain) when:3d')
    url = f"https://news.google.com/rss/search?q={q}&hl=fr&gl=FR&ceid=FR:fr"
    titres = []

    try:
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code == 200:
            root = ET.fromstring(r.text)
            for item in root.findall(".//item")[:8]:
                titre = (item.findtext("title") or "").strip()
                t_low = titre.lower()
                if any(ex in t_low for ex in mots_exclus):
                    continue
                if not _titre_concerne_vraiment_cible(t_low, cible, autres_cibles=autres_cibles):
                    continue
                if any(m in t_low for m in mots_alerte):
                    titres.append(titre)
    except Exception:
        pass
    return list(dict.fromkeys(titres))[:max_titres]


def alerte_grave_detectee(titres_sante):
    mots_graves = ("forfait", "déclarer forfait", "declare forfait", "ischio", "abandon", "withdraw")
    texte = " ".join(titres_sante).lower()
    return any(mg in texte for mg in mots_graves)


def moyenne_ponderee(valeurs, defaut):
    if not valeurs:
        return defaut
    n = len(valeurs)
    poids = [1.0 + (i * 0.25) for i in range(n)]
    return sum(v * p for v, p in zip(valeurs, poids)) / sum(poids)


def stats_foot_detail(team_id, league_slug="ligue-1", season_year="2026", n_derniers=10):
    sched = get_foot_schedule_cached(team_id, league_slug=league_slug, season_year=season_year)
    events = sched.get("data", {}).get("events", [])
    joues = [ev for ev in events if ev.get("status") == "closed"]
    joues.sort(key=lambda ev: str(ev.get("start_time", "")))

    a_venir = [ev for ev in events if ev.get("status") == "not_started"]
    a_venir.sort(key=lambda ev: str(ev.get("start_time", "")))

    bp_all, bc_all = [], []
    bp_home, bc_home = [], []
    bp_away, bc_away = [], []
    v = n = d = 0

    for ev in joues[-n_derniers:]:
        comps = ev.get("competitors", [])
        moi = adv = None
        mon_qual = None
        for c in comps:
            tid = str(c.get("team", {}).get("id"))
            sc = c.get("score")
            if sc is None:
                continue
            sc = int(sc)
            if tid == str(team_id):
                moi = sc
                mon_qual = c.get("qualifier")
            else:
                adv = sc

        if moi is not None and adv is not None:
            bp_all.append(moi)
            bc_all.append(adv)
            if mon_qual == "home":
                bp_home.append(moi)
                bc_home.append(adv)
            elif mon_qual == "away":
                bp_away.append(moi)
                bc_away.append(adv)

            if moi > adv:
                v += 1
            elif moi == adv:
                n += 1
            else:
                d += 1

    bp_base = moyenne_ponderee(bp_all, 1.2)
    bc_base = moyenne_ponderee(bc_all, 1.2)

    return {
        "forme": f"{v}V-{n}N-{d}D",
        "bp_home": moyenne_ponderee(bp_home, bp_base),
        "bc_home": moyenne_ponderee(bc_home, bc_base),
        "bp_away": moyenne_ponderee(bp_away, bp_base),
        "bc_away": moyenne_ponderee(bc_away, bc_base),
        "a_venir": a_venir,
    }


def simuler_poisson_foot(lam_h, lam_a):
    p_h = p_n = p_a = p_over_25 = 0.0
    for h in range(8):
        for a in range(8):
            pr = poisson_prob(h, lam_h) * poisson_prob(a, lam_a)
            if h > a:
                p_h += pr
            elif h == a:
                p_n += pr
            else:
                p_a += pr
            if (h + a) > 2:
                p_over_25 += pr
    tot = p_h + p_n + p_a
    p_h, p_n, p_a = p_h / tot, p_n / tot, p_a / tot
    return p_h, p_n, p_a, p_over_25, max(0.0, 1.0 - p_over_25)


def calculer_mise_kelly(prob, cote_dec, ev_pct, alerte_grave=False):
if cote_dec is None or cote_dec <= 1.01 or prob <= 0.0 or prob >= 1.0:
