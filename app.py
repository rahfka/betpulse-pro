import csv
import importlib
import math
import os
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, render_template_string

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _charger_module_sport(nom_anglais, nom_francais):
    for mod_name in (nom_anglais, nom_francais):
        try:
            return importlib.import_module(mod_name)
        except Exception:
            continue
    raise ImportError(f"Impossible d'importer le module '{nom_anglais}' ni '{nom_francais}'.")


bt = _charger_module_sport("sports_skills.betting", "compétences_sportives.paris")
fb = _charger_module_sport("sports_skills.football", "compétences_sportives.football")
tn = _charger_module_sport("sports_skills.tennis", "compétences_sportives.tennis")


# =====================================================================
# CONFIGURATION GENERALE (GitHub + Render — 8 compétitions / 48h)
# =====================================================================
def _resoudre_chemin_historique():
    env_path = os.getenv("FICHIER_HISTORIQUE", "").strip()
    if env_path:
        return env_path

    dossier_script = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
    candidat_local = os.path.join(dossier_script, "historique_paris.csv")
    try:
        os.makedirs(dossier_script, exist_ok=True)
        test_file = os.path.join(dossier_script, ".write_test")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test_file)
        return candidat_local
    except Exception:
        return "/tmp/historique_paris.csv"


FICHIER_HISTORIQUE = _resoudre_chemin_historique()

EQUIPES_FOOT_IDS = ["160", "174", "176", "166"]  # PSG, Monaco, Marseille, Lille
SAISON_FOOT = os.getenv("SAISON_FOOT", "2026").strip() or "2026"
N_DERNIERS_MATCHS = int(os.getenv("N_DERNIERS_MATCHS", "10"))
FENETRE_HEURES = int(os.getenv("FENETRE_HEURES", "48"))
TIMEOUT_HTTP = int(os.getenv("TIMEOUT_HTTP", "8"))

COMPETITIONS_CIBLES = [
    {
        "slug": "ligue-1",
        "label": "Ligue 1",
        "mots": ("ligue 1", "french ligue 1", "ligue-1", "fra.1"),
        "exclus": ("women", "féminine", "feminine", "u19", "u21", "reserve", "national", "amical"),
    },
    {
        "slug": "la-liga",
        "label": "Liga Espagnole",
        "mots": ("la liga", "laliga", "spanish la liga", "la-liga", "esp.1"),
        "exclus": ("women", "femenina", "u19", "segunda", "b "),
    },
    {
        "slug": "bundesliga",
        "label": "Bundesliga",
        "mots": ("bundesliga", "1. bundesliga", "german bundesliga", "ger.1"),
        "exclus": ("2. bundesliga", "women", "frauen", "u19", "regionalliga"),
    },
    {
        "slug": "portuguese-liga",
        "label": "Liga Portugaise",
        "mots": ("liga portugal", "primeira liga", "portuguese liga", "portuguese-liga", "primeira-liga", "por.1"),
        "exclus": ("segunda", "women", "u23"),
    },
    {
        "slug": "premier-league",
        "label": "Premier League",
        "mots": ("premier league", "english premier league", "premier-league", "eng.1"),
        "exclus": ("russian", "russia", "rostov", "zenit", "cska", "akhmat", "orenburg", "baltika", "voronezh", "rubin", "women", "u21", "u18"),
    },
    {
        "slug": "serie-a",
        "label": "Serie A",
        "mots": ("serie a", "italian serie a", "serie-a", "ita.1"),
        "exclus": ("brazil", "brasileirao", "palmeiras", "flamengo", "vasco", "internacional", "vitória", "vitoria", "bragantino", "são paulo", "sao paulo", "fluminense", "serie b", "women", "femminile"),
    },
    {
        "slug": "uefa-champions-league",
        "label": "Ligue des Champions",
        "mots": ("champions league", "uefa champions league", "ligue des champions", "champions-league", "uefa.champions"),
        "exclus": ("women", "féminine", "feminine", "youth", "u19"),
    },
    {
        "slug": "uefa-europa-league",
        "label": "Europa League",
        "mots": ("europa league", "uefa europa league", "ligue europa", "europa-league", "uefa.europa"),
        "exclus": ("women", "féminine", "feminine", "conference"),
    },
]

FRACTION_KELLY = 0.25
MISE_MIN_UNITES = 0.25
MISE_MAX_UNITES = 2.0
SEUIL_EV_POSITIF_PCT = 3.0

_CACHE_FOOT_SCHED = {}
_CACHE_SEASON_ID = {}
_CACHE_COMP_EVENTS = {}
_CACHE_SANTE = {}
_CACHE_MAPPING_IDS = None
_DERNIER_RAPPORT = {
    "mis_a_jour": None,
    "stats_comp": {},
    "top_sur": [],
    "foot": [],
    "tennis": [],
    "alertes": {},
    "bilan": {},
    "synthese": "",
}

_HTTP_SESSION = requests.Session()
_HTTP_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
    )
})


# =====================================================================
# RECUPERATION ET NORMALISATION DES DONNEES FOOTBALL
# =====================================================================
def get_foot_schedule_cached(team_id, league_slug="ligue-1", season_year="2026"):
    cle = (str(team_id), str(league_slug), str(season_year))
    if cle not in _CACHE_FOOT_SCHED:
        try:
            _CACHE_FOOT_SCHED[cle] = fb.get_team_schedule(
                team_id=cle[0],
                league_slug=cle[1],
                season_year=cle[2],
                competition_id=cle[1],
            )
        except Exception as e:
            print(f"[FOOT] get_team_schedule({cle}) erreur: {e}", flush=True)
            _CACHE_FOOT_SCHED[cle] = {"data": {"events": []}}
    return _CACHE_FOOT_SCHED[cle]


def _extraire_liste_dict(payload, cles_possibles):
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for k in cles_possibles:
        val = data.get(k)
        if isinstance(val, list):
            return [x for x in val if isinstance(x, dict)]
    for val in data.values():
        if isinstance(val, list) and val and isinstance(val[0], dict):
            return val
    return []


def _resoudre_ids_competitions(forcer_refresh=False):
    global _CACHE_MAPPING_IDS
    if _CACHE_MAPPING_IDS is not None and not forcer_refresh:
        return _CACHE_MAPPING_IDS

    mapping = {}
    try:
        comps_resp = fb.get_competitions()
        comps_list = _extraire_liste_dict(comps_resp, ("competitions", "leagues", "items", "events"))
    except Exception as e:
        print(f"[FOOT] get_competitions() erreur: {e}", flush=True)
        comps_list = []

    for cible in COMPETITIONS_CIBLES:
        candidats = [cible["slug"]]
        exclus = cible.get("exclus", ())
        for item in comps_list:
            texte_item = " ".join(
                str(item.get(f, "")).lower()
                for f in ("id", "slug", "name", "short_name", "abbreviation", "league_slug")
            )
            cat_nom = str((item.get("category") or {}).get("name") or "").lower()
            if any(ex in texte_item or ex in cat_nom for ex in exclus):
                continue
            if any(m in texte_item for m in cible["mots"]):
                for champ_id in ("id", "slug", "league_slug", "competition_id"):
                    v = item.get(champ_id)
                    if v and str(v) not in candidats:
                        candidats.append(str(v))
        mapping[cible["slug"]] = candidats

    _CACHE_MAPPING_IDS = mapping
    return mapping


def _resoudre_season_id(comp_ids):
    cle = tuple(comp_ids)
    if cle in _CACHE_SEASON_ID:
        return _CACHE_SEASON_ID[cle]

    for cid in comp_ids:
        try:
            s_resp = fb.get_current_season(competition_id=cid)
            data_s = s_resp.get("data", s_resp) if isinstance(s_resp, dict) else {}
            if isinstance(data_s, dict):
                for k in ("id", "season_id", "slug"):
                    if data_s.get(k):
                        _CACHE_SEASON_ID[cle] = str(data_s[k])
                        return _CACHE_SEASON_ID[cle]
                seasons_arr = data_s.get("seasons")
                if isinstance(seasons_arr, list) and seasons_arr and isinstance(seasons_arr[0], dict):
                    sid = seasons_arr[0].get("id") or seasons_arr[0].get("season_id")
                    if sid:
                        _CACHE_SEASON_ID[cle] = str(sid)
                        return _CACHE_SEASON_ID[cle]
        except Exception:
            continue

    _CACHE_SEASON_ID[cle] = None
    return None


def _extraire_events_depuis_payload(payload):
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

    evenements = []
    for cle in ("events", "matches", "fixtures", "games", "schedules", "items"):
        val = data.get(cle)
        if isinstance(val, list):
            for item in val:
                if not isinstance(item, dict):
                    continue
                if any(k in item for k in ("competitors", "teams", "home_team", "away_team")):
                    evenements.append(item)
                else:
                    evenements.extend(_extraire_events_depuis_payload(item))

    for cle_bloc in ("leagues", "competitions", "seasons", "days", "tournaments"):
        bloc = data.get(cle_bloc)
        if isinstance(bloc, list):
            for sub in bloc:
                if isinstance(sub, dict):
                    evenements.extend(_extraire_events_depuis_payload(sub))

    return evenements


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
            if not h_info:
                h_info = t0
            if not a_info:
                a_info = t1
        return h_info or {}, a_info or {}
    return ev.get("home_team") or {}, ev.get("away_team") or {}


def _event_valide_pour_competition(ev, comp_cfg):
    exclus = comp_cfg.get("exclus", ())
    h_info, a_info = _normaliser_home_away(ev)
    h_nom = str(h_info.get("short_name") or h_info.get("name") or "").lower()
    a_nom = str(a_info.get("short_name") or a_info.get("name") or "").lower()
    txt_match = f"{h_nom} {a_nom} {str(ev.get('name') or '').lower()}"
    if any(ex in txt_match for ex in exclus):
        return False

    comp_obj = ev.get("competition") or ev.get("league") or ev.get("tournament")
    if isinstance(comp_obj, dict):
        txt_comp = " ".join(str(comp_obj.get(k) or "").lower() for k in ("id", "slug", "name", "short_name"))
        if any(ex in txt_comp for ex in exclus):
            return False
    return True


def recuperer_evenements_competition(comp_cfg, ids_candidats):
    slug = comp_cfg["slug"]
    if slug in _CACHE_COMP_EVENTS:
        return _CACHE_COMP_EVENTS[slug]

    evenements = []
    season_id = _resoudre_season_id(ids_candidats)
    if season_id:
        try:
            sched_s = fb.get_season_schedule(season_id=season_id)
            evenements.extend(_extraire_events_depuis_payload(sched_s))
        except Exception as e:
            print(f"[FOOT] get_season_schedule({season_id}) erreur: {e}", flush=True)

    maintenant = datetime.now(timezone.utc)
    jours_recherche = (-1, 0, 1, 2) if not evenements else (-7, -5, -3, -1, 0, 1, 2)
    for delta_j in jours_recherche:
        jour_str = (maintenant + timedelta(days=delta_j)).strftime("%Y-%m-%d")
        try:
            d_resp = fb.get_daily_schedule(date=jour_str)
            for ev in _extraire_events_depuis_payload(d_resp):
                comp_obj = ev.get("competition") or ev.get("league") or ev.get("tournament") or {}
                comp_txt = " ".join(
                    str(comp_obj.get(k) or "").lower()
                    for k in ("id", "slug", "name", "short_name")
                ) if isinstance(comp_obj, dict) else str(comp_obj).lower()
                txt_ev = f"{comp_txt} {str(ev.get('name') or '').lower()}"
                if any(m in txt_ev for m in comp_cfg["mots"]) or any(cid.lower() in comp_txt for cid in ids_candidats):
                    evenements.append(ev)
        except Exception as e:
            print(f"[FOOT] get_daily_schedule({jour_str}) erreur: {e}", flush=True)

    dedup = {}
    for idx, ev in enumerate(evenements):
        if not _event_valide_pour_competition(ev, comp_cfg):
            continue
        h_info, a_info = _normaliser_home_away(ev)
        dt_cle = str(ev.get("start_time") or ev.get("date") or "")[:16]
        eid_raw = str(ev.get("id") or ev.get("event_id") or "").strip()
        eq_cle = f"{h_info.get('id') or h_info.get('name')}_{a_info.get('id') or a_info.get('name')}_{dt_cle}".lower()
        cle = eid_raw if eid_raw else eq_cle
        if not cle:
            cle = f"{slug}_{idx}"
        if cle not in dedup:
            dedup[cle] = ev

    res = list(dedup.values())
    _CACHE_COMP_EVENTS[slug] = res
    return res


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
        s = str(date_str).strip()
        maintenant = datetime.now(timezone.utc)
        limite = maintenant + timedelta(hours=heures_max)
        if len(s) == 10 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            dt_jour = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            jour_debut = maintenant.replace(hour=0, minute=0, second=0, microsecond=0)
            return jour_debut <= dt_jour <= limite

        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (maintenant - timedelta(hours=2)) <= dt <= limite
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
    bruit_autre_sport_ou_faits_divers = (
        "nba", "basket", "wembanyama", "lakers", "celtics", "warriors",
        "rugby", "top 14", "handball", "cyclisme", "f1", "formule 1",
        "agression", "fusillade", "homicide", "polici", "tribunal", "justice", "prison"
    )
    if any(b in titre_bas for b in bruit_autre_sport_ou_faits_divers):
        return False

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
    cle_cache = (str(cible).strip().lower(), int(max_titres))
    if cle_cache in _CACHE_SANTE:
        return list(_CACHE_SANTE[cle_cache])

    mots_alerte = [
        "bless", "forfait", "incertain", "infirmerie", "absent", "pépin", "pepin",
        "ischio", "genou", "cheville", "cuisse", "mollet", "suspend", "abandon",
        "injury", "injured", "doubt", "withdraw"
    ]
    mots_exclus = [
        "roland-garros", "roland garros", "rétrospective", "retrospective",
        "histoire", "archive", "2022", "2023", "2024", "2025", "prolonge"
    ]
    q = urllib.parse.quote(f'"{cible}" (blessure OR forfait OR incertain) when:3d')
    url = f"https://news.google.com/rss/search?q={q}&hl=fr&gl=FR&ceid=FR:fr"
    titres = []

    try:
        r = _HTTP_SESSION.get(url, timeout=TIMEOUT_HTTP)
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

    res = list(dict.fromkeys(titres))[:max_titres]
    _CACHE_SANTE[cle_cache] = res
    return list(res)


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


def stats_foot_detail(team_id, team_name="", league_slug="ligue-1", season_year="2026", n_derniers=10, events_override=None):
    if events_override is not None:
        events = list(events_override)
    else:
        sched = get_foot_schedule_cached(team_id, league_slug=league_slug, season_year=season_year)
        events = _extraire_events_depuis_payload(sched)

    joues = [ev for ev in events if str(ev.get("status", "")).lower() in ("closed", "final", "post", "completed")]
    joues.sort(key=lambda ev: str(ev.get("start_time", "") or ev.get("date", "")))

    a_venir = [ev for ev in events if str(ev.get("status", "")).lower() in ("not_started", "scheduled", "pre")]
    a_venir.sort(key=lambda ev: str(ev.get("start_time", "") or ev.get("date", "")))

    bp_all, bc_all = [], []
    bp_home, bc_home = [], []
    bp_away, bc_away = [], []
    v = n = d = 0
    nom_l = (team_name or "").strip().lower()

    for ev in reversed(joues):
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

        moi = adv = None
        mon_qual = None
        for c in comps_liste:
            if not isinstance(c, dict):
                continue
            t_obj = c.get("team") if isinstance(c.get("team"), dict) else c
            tid = str(t_obj.get("id") or "")
            tnm = str(t_obj.get("name") or t_obj.get("short_name") or "").strip().lower()
            sc = c.get("score")
            if sc is None:
                continue
            try:
                sc_int = int(float(sc))
            except Exception:
                continue

            est_moi = (
                (team_id and tid and tid == str(team_id))
                or (nom_l and tnm and (tnm == nom_l or nom_l in tnm or tnm in nom_l))
            )
            if est_moi and moi is None:
                moi = sc_int
                mon_qual = c.get("qualifier") or c.get("homeAway")
            elif adv is None:
                adv = sc_int

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

        if len(bp_all) >= n_derniers:
            break

    bp_all.reverse()
    bc_all.reverse()
    bp_home.reverse()
    bc_home.reverse()
    bp_away.reverse()
    bc_away.reverse()

    a_historique = (v + n + d) > 0
    bp_base = moyenne_ponderee(bp_all, 1.25)
    bc_base = moyenne_ponderee(bc_all, 1.20)

    return {
        "forme": f"{v}V-{n}N-{d}D" if a_historique else "Forme récente",
        "a_historique": a_
