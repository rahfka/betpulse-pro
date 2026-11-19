import csv
import math
import os
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests
import sports_skills.betting as bt
import sports_skills.football as fb
import sports_skills.tennis as tn

# Forcer UTF-8 proprement si disponible (utile sur Linux / Render)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# =====================================================================
# CONFIGURATION GENERALE (GitHub + Render / 8 compétitions / 48h)
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

_HTTP_SESSION = requests.Session()
_HTTP_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
    )
})


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
        except Exception:
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
    except Exception:
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
        except Exception:
            pass

    maintenant = datetime.now(timezone.utc)
    jours_recherche = (-1, 0, 1, 2) if not evenements else (-7, -5, -3, -1, 0, 1, 2)
    for delta_j in jours_recherche:
        jour_str = (maintenant + timedelta(days=delta_j)).strftime("%Y-%m-%d")
        try:
            d_resp = fb.get_daily_schedule(date=jour_str)
            for ev in _extraire_events_depuis_payload(d_resp):
                txt_ev = str(ev).lower()
                if any(m in txt_ev for m in comp_cfg["mots"]):
                    evenements.append(ev)
        except Exception:
            pass

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
        "a_historique": a_historique,
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
        return 0.0
    if ev_pct is None or ev_pct < SEUIL_EV_POSITIF_PCT:
        return 0.0
    b = cote_dec - 1.0
    q = 1.0 - prob
    kelly_plein = (b * prob - q) / b
    if kelly_plein <= 0:
        return 0.0
    mise = kelly_plein * FRACTION_KELLY * 10.0
    if alerte_grave:
        mise *= 0.5
    mise = min(mise, MISE_MAX_UNITES)
    if mise < MISE_MIN_UNITES:
        return 0.0
    return round(mise, 2)


def cote_dec_est_courte(dec_val):
    return dec_val is not None and 1.0 < dec_val <= 1.55


def analyser_cote_us(details_str, fair_prob, alerte_grave=False):
    m = re.search(r"([+-]\d+)", str(details_str))
    if not m:
        return "", None, "Neutre", None, 0.0
    cote_us = m.group(1)
    cote_num = int(cote_us)

    if abs(cote_num) < 100:
        return f" | Cote {cote_us} (anomalie flux)", None, "Neutre", None, 0.0

    conv = None
    try:
        conv = bt.convert_odds(odds=cote_us, from_format="american")
    except Exception:
        conv = None

    data_c = conv.get("data", {}) if isinstance(conv, dict) else {}
    imp = data_c.get("implied_probability") or data_c.get("probability") if isinstance(data_c, dict) else None
    dec = data_c.get("decimal") if isinstance(data_c, dict) else None

    if dec is None:
        if cote_num > 0:
            dec = 1.0 + (cote_num / 100.0)
        elif cote_num < 0:
            dec = 1.0 + (100.0 / abs(cote_num))

    if imp is None and dec and float(dec) > 1.0:
        imp = 1.0 / float(dec)

    try:
        dec_val = float(dec) if dec is not None else None
    except Exception:
        dec_val = None

    if dec_val and dec_val > 1.0:
        ev_pct = (fair_prob * dec_val - 1.0) * 100.0
        mise_u = calculer_mise_kelly(fair_prob, dec_val, ev_pct, alerte_grave=alerte_grave)
        if ev_pct >= SEUIL_EV_POSITIF_PCT:
            label_val = f"+Valeur ({ev_pct:+.1f}%)"
        elif cote_dec_est_courte(dec_val) or ev_pct <= -12.0:
            label_val = f"Cote courte ({ev_pct:+.1f}%)"
        else:
            label_val = f"Ajusté ({ev_pct:+.1f}%)"

        imp_txt = f", implicite {float(imp)*100:.1f}%" if imp is not None else ""
        mise_txt = f" | Mise: {mise_u:.2f}u" if mise_u > 0 else ""
        txt = f" | Cote {cote_us} (dec {dec_val:.2f}{imp_txt}) -> {label_val}{mise_txt}"
        return txt, ev_pct, label_val, dec_val, mise_u

    return f" | Cote {cote_us}", None, "Neutre", dec_val, 0.0


def niveau_prudence(proba_pct, nb_alertes_sante, label_val="Neutre", alerte_grave=False, donnees_completes=True):
    if not donnees_completes:
        return "PRUDENT (données partielles)"
    if alerte_grave:
        return "PRUDENT (alerte blessure)"
    if "Cote courte" in label_val:
        return "PRUDENT (cote courte)"
    if proba_pct >= 72 and nb_alertes_sante == 0:
        return "ELEVE"
    if proba_pct >= 56 and nb_alertes_sante <= 1:
        return "MODERE"
    return "PRUDENT"


def _construire_entree_foot(ev, label_comp, slug, events_comp, n_derniers):
    ev_id = str(ev.get("id") or "")
    start_str = str(ev.get("start_time") or ev.get("date") or "")
    h_info, a_info = _normaliser_home_away(ev)
    h_nom = h_info.get("short_name") or h_info.get("name") or "Home"
    a_nom = a_info.get("short_name") or a_info.get("name") or "Away"

    st_h = stats_foot_detail(
        h_info.get("id", ""), team_name=h_nom, league_slug=slug,
        season_year=SAISON_FOOT, n_derniers=n_derniers, events_override=events_comp,
    )
    st_a = stats_foot_detail(
        a_info.get("id", ""), team_name=a_nom, league_slug=slug,
        season_year=SAISON_FOOT, n_derniers=n_derniers, events_override=events_comp,
    )

    donnees_completes = st_h["a_historique"] or st_a["a_historique"]

    lam_h = max((st_h["bp_home"] + st_a["bc_away"]) / 2, 0.35)
    lam_a = max((st_a["bp_away"] + st_h["bc_home"]) / 2, 0.35)
    p_h, p_n, p_a, p_ov25, p_un25 = simuler_poisson_foot(lam_h, lam_a)

    options = [
        (f"Victoire {h_nom}", p_h, h_nom),
        ("Match nul", p_n, None),
        (f"Victoire {a_nom}", p_a, a_nom),
    ]
    choix, prob, _ = max(options, key=lambda x: x[1])

    titres_sante = []
    for cible in [h_nom, a_nom]:
        autres = [x for x in [h_nom, a_nom] if x != cible]
        titres_sante += scanner_sante_cible(cible, max_titres=2, autres_cibles=autres)
    titres_sante = list(dict.fromkeys(titres_sante))[:3]

    grave = alerte_grave_detectee(titres_sante)
    malus_sante = min(len(titres_sante) * 0.02 + (0.04 if grave else 0.0), 0.10)
    prob_ajustee = max(0.05, prob - malus_sante)

    cote_txt = ""
    ev_val = None
    label_val = "Neutre"
    cote_dec = None
    mise_u = 0.0
    odds_obj = ev.get("odds")
    if isinstance(odds_obj, dict) and odds_obj.get("details"):
        cote_txt, ev_val, label_val, cote_dec, mise_u = analyser_cote_us(
            odds_obj.get("details"), prob_ajustee, alerte_grave=grave
        )

    if not donnees_completes:
        mise_u = 0.0
        a_val = False
    else:
        a_val = ("+Valeur" in label_val)

    ov_pct = round(p_ov25 * 100, 1)
    un_pct = round(p_un25 * 100, 1)
    if ov_pct >= un_pct:
        buts_label = "+2.5 buts"
        buts_proba = ov_pct
    else:
        buts_label = "-2.5 buts"
        buts_proba = un_pct

    buts_opt = f"{buts_label} ({buts_proba:.0f}%)"
    focus_buts = " [Signal buts fort]" if (donnees_completes and max(ov_pct, un_pct) >= 75) else ""

    proba_pct = round(prob_ajustee * 100, 1)
    prudence = niveau_prudence(
        proba_pct, len(titres_sante), label_val,
        alerte_grave=grave, donnees_completes=donnees_completes
    )

    return {
        "sport": "FOOT",
        "competition": label_comp,
        "fiable": donnees_completes,
        "event_id": str(ev_id),
        "match": f"{h_nom} vs {a_nom}",
        "date_match": start_str[:16].replace("T", " ") if len(start_str) >= 16 else start_str[:10],
        "detail": (
            f"[{label_comp}] {h_nom} [{st_h['forme']}] vs {a_nom} [{st_a['forme']}] | "
            f"Buts: {buts_opt}{focus_buts} | Alertes 3j: {len(titres_sante)}{cote_txt}"
        ),
        "choix": choix,
        "proba": proba_pct,
        "prudence": prudence,
        "a_valeur": a_val,
        "ev_pct": ev_val,
        "cote_dec": cote_dec,
        "mise_u": mise_u,
        "buts_choix": buts_label,
        "buts_proba": buts_proba,
    }, titres_sante


def analyser_foot_competitions(competitions, equipes_ids=None, n_derniers=10, heures_max=48, mapping_ids=None):
    vus = set()
    resultats = []
    alertes_globales = {}
    stats_par_comp = {}
    if mapping_ids is None:
        mapping_ids = _resoudre_ids_competitions()

    for comp in competitions:
        slug = comp["slug"]
        label_comp = comp.get("label", slug)
        ids_cand = mapping_ids.get(slug, [slug])
        events_comp = recuperer_evenements_competition(comp, ids_cand)

        if equipes_ids:
            for cid in ids_cand:
                for tid in equipes_ids:
                    sched = get_foot_schedule_cached(tid, league_slug=cid, season_year=SAISON_FOOT)
                    for ev in _extraire_events_depuis_payload(sched):
                        if not _event_valide_pour_competition(ev, comp):
                            continue
                        eid_f = str(ev.get("id") or "")
                        if eid_f and not any(str(x.get("id")) == eid_f for x in events_comp):
                            events_comp.append(ev)

        nb_retenus = 0
        for ev in events_comp:
            statut = str(ev.get("status", "")).lower()
            if statut in ("closed", "final", "post", "completed", "in_progress", "live"):
                continue
            ev_id = str(ev.get("id") or "")
            cle_ev = f"{slug}_{ev_id}" if ev_id else f"{slug}_{ev.get('start_time')}_{len(vus)}"
            if cle_ev in vus:
                continue

            start_str = str(ev.get("start_time") or ev.get("date") or "")
            if not dans_fenetre_heures(start_str, heures_max):
                continue

            vus.add(cle_ev)
            nb_retenus += 1
            entree, titres_sante = _construire_entree_foot(ev, label_comp, slug, events_comp, n_derniers)
            resultats.append(entree)
            if titres_sante:
                alertes_globales[entree["match"]] = titres_sante

        stats_par_comp[label_comp] = f"{nb_retenus} (bruts: {len(events_comp)})"

    return resultats, alertes_globales, stats_par_comp


def analyser_tennis(heures_max=48):
    atp_rk = {
        " ".join(p["name"].lower().split()): p["rank"]
        for p in tn.get_rankings(tour="atp").get("data", {}).get("rankings", [])
    }
    wta_rk = {
        " ".join(p["name"].lower().split()): p["rank"]
        for p in tn.get_rankings(tour="wta").get("data", {}).get("rankings", [])
    }

    sb = tn.get_scoreboard()
    simples_fiables = []
    simples_nc = []
    alertes_tennis = {}

    tous_joueurs = []
    for t in sb.get("data", {}).get("tournaments", []):
        for d in t.get("draws", []):
            for m in d.get("matches", []):
                for c in m.get("competitors", []):
                    nm = c.get("name", "")
                    if nm and nm != "TBD":
                        tous_joueurs.append(nm)

    for t in sb.get("data", {}).get("tournaments", []):
        t_name = t.get("name")
        for d in t.get("draws", []):
            draw_name = d.get("name", "")
            if "Doubles" in draw_name:
                continue

            victoires_tournoi = {}
            for m in d.get("matches", []):
                if m.get("status") == "closed":
                    res = m.get("result", "")
                    if " bt " in res:
                        gagnant = res.split(" bt ")[0].strip()
                        victoires_tournoi[gagnant] = victoires_tournoi.get(gagnant, 0) + 1

            for m in d.get("matches", []):
                if m.get("status") != "not_started":
                    continue
                date_m = str(m.get("date", ""))
                if not dans_fenetre_heures(date_m, heures_max):
                    continue

                comps = m.get("competitors", [])
                if len(comps) != 2:
                    continue
                j1, j2 = comps[0].get("name", ""), comps[1].get("name", "")
                if j1 == "TBD" or j2 == "TBD":
                    continue

                rk_map = atp_rk if "Men" in draw_name else wta_rk
                r1 = trouver_rang(j1, rk_map)
                r2 = trouver_rang(j2, rk_map)

                v1 = sum(v for k, v in victoires_tournoi.items() if j1.lower() in k.lower())
                v2 = sum(v for k, v in victoires_tournoi.items() if j2.lower() in k.lower())

                donnees_completes = (r1 is not None and r2 is not None)
                eff_r1 = r1 if r1 is not None else 150
                eff_r2 = r2 if r2 is not None else 150
                poids_rk = 0.85 if donnees_completes else 0.35

                diff = (math.log(eff_r2 + 1) - math.log(eff_r1 + 1)) * poids_rk + (v1 - v2) * 0.20
                p1 = 1 / (1 + math.exp(-diff))
                p2 = 1 - p1

                favori, prob = (j1, p1) if p1 >= p2 else (j2, p2)
                if donnees_completes:
                    prob = min(max(prob, 0.50), 0.82)

                titres_sante = []
                if donnees_completes:
                    autres = [x for x in tous_joueurs if x.lower() != favori.lower()]
                    titres_sante = scanner_sante_cible(favori, max_titres=2, autres_cibles=autres)
                    if titres_sante:
                        alertes_tennis[favori] = titres_sante

                grave = alerte_grave_detectee(titres_sante)
                malus = min(len(titres_sante) * 0.03 + (0.06 if grave else 0.0), 0.12)
                prob_ajustee = max(0.05, prob - malus)
                proba_pct = round(prob_ajustee * 100, 1)

                lbl1 = f"#{r1}" if r1 is not None else "NC"
                lbl2 = f"#{r2}" if r2 is not None else "NC"
                prudence = (
                    niveau_prudence(proba_pct, len(titres_sante), alerte_grave=grave)
                    if donnees_completes
                    else "PRUDENT (NC)"
                )

                entree = {
                    "sport": "TENNIS",
                    "competition": t_name or "Tennis",
                    "fiable": donnees_completes,
                    "event_id": str(m.get("id", "")),
                    "match": f"{j1} ({lbl1}) vs {j2} ({lbl2})",
                    "date_match": date_m[:16].replace("T", " ") if len(date_m) >= 16 else date_m[:10],
                    "detail": (
                        f"{t_name} | {date_m[:16]} UTC | "
                        f"V_tournoi: {j1}={v1}, {j2}={v2} | Alertes 3j: {len(titres_sante)}"
                    ),
                    "choix": f"Victoire {favori}",
                    "proba": proba_pct,
                    "prudence": prudence,
                    "a_valeur": False,
                    "ev_pct": None,
                    "cote_dec": None,
                    "mise_u": 0.0,
                }
                if donnees_completes:
                    simples_fiables.append(entree)
                else:
                    simples_nc.append(entree)

    return simples_fiables, simples_nc, alertes_tennis


def _calculer_pnl_ligne(row):
    statut = (row.get("statut") or "").strip().upper()
    if statut not in ("GAGNE", "PERDU"):
        return None
    try:
        mise = float(row.get("mise_u") or 0.0)
    except Exception:
        mise = 0.0
    try:
        cote = float(row.get("cote_dec") or 0.0)
    except Exception:
        cote = 0.0
    if mise <= 0 or cote <= 1.0:
        return 0.0
    if statut == "GAGNE":
        return round(mise * (cote - 1.0), 2)
    return round(-mise, 2)


def _evaluer_issue_foot(res_f, choix_l):
    if "+2.5 buts" in choix_l:
        return "GAGNE" if res_f["total_buts"] > 2 else "PERDU"
    if "-2.5 buts" in choix_l:
        return "GAGNE" if res_f["total_buts"] < 3 else "PERDU"
    if res_f["issue"] == "DRAW":
        return "GAGNE" if "nul" in choix_l else "PERDU"
    gagne = any(nm and nm in choix_l for nm in res_f["noms"])
    return "GAGNE" if gagne else "PERDU"


def mettre_a_jour_et_sauver_historique(pistes, foot_res, competitions, mapping_ids=None):
    champs = [
        "date_analyse", "sport", "event_id", "match", "date_match",
        "choix", "proba", "prudence", "cote_dec", "mise_u", "pnl_u", "statut"
    ]
    lignes_existantes = []
    cles_deja_enregistrees = set()

    if os.path.exists(FICHIER_HISTORIQUE):
        try:
            with open(FICHIER_HISTORIQUE, "r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames:
                    for row in reader:
                        if not any((v or "").strip() for v in row.values()):
                            continue
                        row["statut"] = (row.get("statut") or "EN_ATTENTE").strip().upper()
                        lignes_existantes.append(row)
                        cle = f"{row.get('sport')}_{row.get('event_id')}_{row.get('choix')}"
                        cles_deja_enregistrees.add(cle)
        except Exception:
            lignes_existantes = []

    vainqueurs_tennis_par_id = {}
    matchs_tennis_clos = []
    try:
        sb = tn.get_scoreboard()
        for t in sb.get("data", {}).get("tournaments", []):
            for d in t.get("draws", []):
                for m in d.get("matches", []):
                    if m.get("status") == "closed":
                        res = m.get("result", "")
                        mid = str(m.get("id", ""))
                        joueurs = [
                            c.get("name", "").strip().lower()
                            for c in m.get("competitors", [])
                            if c.get("name")
                        ]
                        if " bt " in res:
                            gagnant = res.split(" bt ")[0].strip().lower()
                            if mid:
                                vainqueurs_tennis_par_id[mid] = gagnant
                            if len(joueurs) == 2:
                                matchs_tennis_clos.append({
                                    "vainqueur": gagnant,
                                    "joueurs": joueurs,
                                })
    except Exception:
        pass

    resultats_foot_clos = {}
    liste_foot_clos = []
    if mapping_ids is None:
        mapping_ids = _resoudre_ids_competitions()

    for comp in competitions:
        try:
            for ev in recuperer_evenements_competition(comp, mapping_ids.get(comp["slug"], [comp["slug"]])):
                if str(ev.get("status", "")).lower() in ("closed", "final", "post", "completed"):
                    eid = str(ev.get("id", ""))
                    h_info, a_info = _normaliser_home_away(ev)
                    s0, s1 = h_info.get("score"), a_info.get("score")
                    if s0 is not None and s1 is not None:
                        i0, i1 = int(float(s0)), int(float(s1))
                        total_buts = i0 + i1
                        h_noms = {
                            str(h_info.get(k, "")).strip().lower()
                            for k in ("name", "short_name", "abbreviation")
                            if h_info.get(k)
                        }
                        a_noms = {
                            str(a_info.get(k, "")).strip().lower()
                            for k in ("name", "short_name", "abbreviation")
                            if a_info.get(k)
                        }
                        if i0 == i1:
                            entry_res = {
                                "issue": "DRAW",
                                "noms": set(),
                                "h_noms": h_noms,
                                "a_noms": a_noms,
                                "total_buts": total_buts,
                            }
                        else:
                            gagnant_obj = h_info if i0 > i1 else a_info
                            noms = {
                                str(gagnant_obj.get(k, "")).strip().lower()
                                for k in ("name", "short_name", "abbreviation")
                                if gagnant_obj.get(k)
                            }
                            entry_res = {
                                "issue": "WIN",
                                "noms": noms,
                                "h_noms": h_noms,
                                "a_noms": a_noms,
                                "total_buts": total_buts,
                            }
                        if eid:
                            resultats_foot_clos[eid] = entry_res
                        liste_foot_clos.append(entry_res)
        except Exception:
            pass

    for row in lignes_existantes:
        if row.get("statut") == "EN_ATTENTE":
            sp = row.get("sport")
            eid = str(row.get("event_id", ""))
            choix_l = row.get("choix", "").lower()
            match_l = row.get("match", "").lower()

            if sp == "TENNIS":
                if eid and eid in vainqueurs_tennis_par_id:
                    vainq = vainqueurs_tennis_par_id[eid]
                    row["statut"] = "GAGNE" if (vainq and vainq in choix_l) else "PERDU"
                else:
                    for mc in matchs_tennis_clos:
                        j_a, j_b = mc["joueurs"]
                        if j_a in match_l and j_b in match_l:
                            row["statut"] = "GAGNE" if mc["vainqueur"] in choix_l else "PERDU"
                            break
            elif sp == "FOOT":
                res_f = resultats_foot_clos.get(eid)
                if not res_f and " vs " in match_l:
                    for rf in liste_foot_clos:
                        h_ok = any(n and n in match_l for n in rf["h_noms"])
                        a_ok = any(n and n in match_l for n in rf["a_noms"])
                        if h_ok and a_ok:
                            res_f = rf
                            break
                if res_f:
                    row["statut"] = _evaluer_issue_foot(res_f, choix_l)

        pnl = _calculer_pnl_ligne(row)
        row["pnl_u"] = "" if pnl is None else f"{pnl:+.2f}"

    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M")
    a_enregistrer = [p for p in pistes if p.get("fiable", True)]
    for f in foot_res:
        if f.get("fiable", True) and f.get("buts_choix") and f.get("buts_proba") is not None:
            a_enregistrer.append({
                "sport": "FOOT",
                "event_id": f["event_id"],
                "match": f["match"],
                "date_match": f["date_match"],
                "choix": f["buts_choix"],
                "proba": f["buts_proba"],
                "prudence": "MODERE" if f["buts_proba"] >= 75 else "PRUDENT",
                "cote_dec": "",
                "mise_u": 0.0,
            })

    for p in a_enregistrer:
        cle = f"{p['sport']}_{p['event_id']}_{p['choix']}"
        if cle not in cles_deja_enregistrees:
            lignes_existantes.append({
                "date_analyse": maintenant,
                "sport": p["sport"],
                "event_id": p["event_id"],
                "match": p["match"],
                "date_match": p["date_match"],
                "choix": p["choix"],
                "proba": str(p["proba"]),
                "prudence": p["prudence"],
                "cote_dec": "" if p.get("cote_dec") is None else str(p.get("cote_dec")),
                "mise_u": str(p.get("mise_u", 0.0)),
                "pnl_u": "",
                "statut": "EN_ATTENTE",
            })
            cles_deja_enregistrees.add(cle)

    try:
        os.makedirs(os.path.dirname(FICHIER_HISTORIQUE) or ".", exist_ok=True)
        with open(FICHIER_HISTORIQUE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=champs)
            writer.writeheader()
            for r in lignes_existantes:
                writer.writerow({k: r.get(k, "") for k in champs})
    except Exception:
        pass

    def resume_filtre(predicat):
        sous = [r for r in lignes_existantes if predicat(r) and r.get("statut") in ("GAGNE", "PERDU")]
        ok = [r for r in sous if r.get("statut") == "GAGNE"]
        pct = (len(ok) / len(sous) * 100) if sous else 0.0
        return f"{len(ok)}/{len(sous)} ({pct:.1f}%)"

    verifies = [r for r in lignes_existantes if r.get("statut") in ("GAGNE", "PERDU")]
    gagnes = [r for r in verifies if r.get("statut") == "GAGNE"]
    en_attente = [r for r in lignes_existantes if r.get("statut") == "EN_ATTENTE"]
    taux = (len(gagnes) / len(verifies) * 100) if verifies else 0.0

    mises_engagees = 0.0
    pnl_total = 0.0
    for r in verifies:
        try:
            m = float(r.get("mise_u") or 0.0)
        except Exception:
            m = 0.0
        try:
            p_val = float(r.get("pnl_u") or 0.0)
        except Exception:
            p_val = 0.0
        if m > 0:
            mises_engagees += m
            pnl_total += p_val

    roi_pct = (pnl_total / mises_engagees * 100.0) if mises_engagees > 0 else 0.0

    return {
        "total": len(lignes_existantes),
        "en_attente": len(en_attente),
        "verifies": len(verifies),
        "gagnes": len(gagnes),
        "taux": round(taux, 1),
        "mises_engagees": round(mises_engagees, 2),
        "pnl_total": round(pnl_total, 2),
        "roi_pct": round(roi_pct, 1),
        "bilan_foot": resume_filtre(lambda r: r.get("sport") == "FOOT" and "2.5 buts" not in r.get("choix", "")),
        "bilan_buts": resume_filtre(lambda r: r.get("sport") == "FOOT" and "2.5 buts" in r.get("choix", "")),
        "bilan_tennis": resume_filtre(lambda r: r.get("sport") == "TENNIS"),
    }


def appeler_ia_gratuite(prompt_texte):
    or_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if or_key:
        try:
            r = _HTTP_SESSION.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {or_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "openrouter/free",
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Tu es un analyste sportif concis en français. "
                                "Synthétise forme, blessures détectées et risques "
                                "sans jamais garantir de résultat."
                            ),
                        },
                        {"role": "user", "content": prompt_texte},
                    ],
                    "max_tokens": 500,
                },
                timeout=20,
            )
            if r.status_code == 200:
                txt = r.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                if txt.strip():
                    return txt.strip()
        except Exception:
            pass

    gm_key = os.getenv("GEMINI_API_KEY", "").strip()
    if gm_key:
        try:
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"gemini-2.5-flash:generateContent?key={gm_key}"
            )
            r = _HTTP_SESSION.post(
                url,
                headers={"Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": prompt_texte}]}],
                    "generationConfig": {"maxOutputTokens": 500},
                },
                timeout=20,
            )
            if r.status_code == 200:
                parts = (
                    r.json()
                    .get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [])
                )
                txt = "".join(p.get("text", "") for p in parts).strip()
                if txt:
                    return txt
        except Exception:
            pass

    return None


def generer_synthese(pistes, alertes_sante):
    lignes_pistes = [
        f"- [{p['sport']}] {p['match']} -> {p['choix']} ({p['proba']}%, niveau {p['prudence']}) | {p['detail']}"
        for p in pistes[:6]
    ]
    lignes_sante = [
        f"- {cible} : {' // '.join(titres)}"
        for cible, titres in alertes_sante.items()
    ]

    prompt = (
        "Analyse ces pronostics statistiques et les alertes blessures récentes (3 jours) :\n"
        "PISTES :\n" + "\n".join(lignes_pistes) + "\n\n"
        "ALERTES SANTE RECENTES :\n" + ("\n".join(lignes_sante) if lignes_sante else "Aucune alerte récente détectée.") + "\n\n"
        "Rédige un bilan court en français : points forts, impact des blessures et niveau de prudence."
    )

    rep_ia = appeler_ia_gratuite(prompt)
    if rep_ia:
        return rep_ia

    lignes = ["(Synthese locale — renseigne GEMINI_API_KEY ou OPENROUTER_API_KEY pour le texte IA)"]
    for p in pistes[:6]:
        mise_str = f" | Mise: {p['mise_u']:.2f}u" if p.get("mise_u", 0.0) > 0 else ""
        lignes.append(f"• [{p['prudence']}] {p['match']} -> {p['choix']} ({p['proba']}%{mise_str})")
    for cible, titres in alertes_sante.items():
        lignes.append(f"• Sante {cible} : {titres[0]}")
    return "\n".join(lignes)


def executer_analyse():
    print(f"Analyse en cours (fenetre: {FENETRE_HEURES}h)...", flush=True)
    mapping_ids = _resoudre_ids_competitions()
    foot_res, alertes_f, stats_comp = analyser_foot_competitions(
        COMPETITIONS_CIBLES,
        equipes_ids=EQUIPES_FOOT_IDS,
        n_derniers=N_DERNIERS_MATCHS,
        heures_max=FENETRE_HEURES,
        mapping_ids=mapping_ids,
    )
    t_simples_ok, t_simples_nc, alertes_t = analyser_tennis(heures_max=FENETRE_HEURES)

    foot_complets = sorted([x for x in foot_res if x.get("fiable", True)], key=lambda x: x["proba"], reverse=True)
    foot_partiels = sorted([x for x in foot_res if not x.get("fiable", True)], key=lambda x: x["proba"], reverse=True)
    tous_matchs_foot = sorted(foot_complets + foot_partiels, key=lambda x: x["proba"], reverse=True)

    toutes_simples_tennis = sorted(t_simples_ok + t_simples_nc, key=lambda x: x["proba"], reverse=True)
    principaux_complets = sorted(foot_complets + t_simples_ok, key=lambda x: x["proba"], reverse=True)

    top_sur = sorted(tous_matchs_foot + toutes_simples_tennis, key=lambda x: x["proba"], reverse=True)
    toutes_alertes = filtrer_alertes_uniques({**alertes_f, **alertes_t})

    print(f"\nMatchs Foot trouves : {len(foot_res)} (dont {len(foot_complets)} avec historique)", flush=True)
    print("Couverture Foot :", " • ".join(f"{k}: {v}" for k, v in stats_comp.items()), flush=True)

    print("\n=== 1. PRONOSTICS LES PLUS SÛRS (FOOT & TENNIS) ===", flush=True)
    if top_sur:
        for i, it in enumerate(top_sur[:10], 1):
            mise_txt = f" | Mise: {it['mise_u']:.2f}u" if it.get("mise_u", 0.0) > 0 else ""
            print(
                f"{i}. [{it['sport']}] {it['proba']}% [{it['prudence']}] -> {it['choix']}{mise_txt}",
                flush=True,
            )
            print(f"    Match : {it['match']} ({it['date_match']})", flush=True)
    else:
        print("Aucun match trouvé sur cette fenêtre.", flush=True)

    print("\n=== 2. TOUS LES MATCHS DE FOOT (PRONOSTIC, FORME & SYNTHÈSE) ===", flush=True)
    if tous_matchs_foot:
        for i, it in enumerate(tous_matchs_foot, 1):
            print(f"{i}. {it['proba']}% [{it['prudence']}] -> {it['choix']}", flush=True)
            print(f"    Match : {it['match']} ({it['date_match']})", flush=True)
            print(f"    Forme / Infos : {it['detail']}", flush=True)
            if it["match"] in toutes_alertes:
                print(f"    Synthèse santé : {' // '.join(toutes_alertes[it['match']])}", flush=True)
    else:
        print("Aucun match de foot trouvé sur cette fenêtre.", flush=True)

    print("\n=== 3. TOUS LES MATCHS DE TENNIS (PRONOSTIC, FORME & SYNTHÈSE) ===", flush=True)
    if toutes_simples_tennis:
        for i, it in enumerate(toutes_simples_tennis, 1):
            print(f"{i}. {it['proba']}% [{it['prudence']}] -> {it['choix']}", flush=True)
            print(f"    Match : {it['match']} ({it['date_match']})", flush=True)
            print(f"    Forme / Infos : {it['detail']}", flush=True)
            alertes_match = []
            for cible, titres in toutes_alertes.items():
                if cible.lower() in it["match"].lower() or cible.lower() in it["choix"].lower():
                    alertes_match.extend(titres)
            if alertes_match:
                print(f"    Synthèse santé : {' // '.join(dict.fromkeys(alertes_match))}", flush=True)
    else:
        print("Aucun match de tennis en simple trouvé sur cette fenêtre.", flush=True)

    bilan = mettre_a_jour_et_sauver_historique(
        principaux_complets,
        foot_res,
        COMPETITIONS_CIBLES,
        mapping_ids=mapping_ids,
    )
    print("\n=== HISTORIQUE & BILAN (RAPPEL : ERREUR TOUJOURS POSSIBLE) ===", flush=True)
    print(
        f"Total suivis : {bilan['total']} | En attente : {bilan['en_attente']} | "
        f"Vérifiés : {bilan['verifies']} | Gagnés : {bilan['gagnes']} ({bilan['taux']}%)",
        flush=True,
    )
    print(
        f"Bankroll -> Mises : {bilan['mises_engagees']:.2f}u | PnL : {bilan['pnl_total']:+.2f}u | "
        f"ROI : {bilan['roi_pct']:+.1f}%",
        flush=True,
    )
    print(
        f"Détail -> Foot 1X2 : {bilan['bilan_foot']} | Buts Foot : {bilan['bilan_buts']} | "
        f"Tennis : {bilan['bilan_tennis']}",
        flush=True,
    )

    print("\n=== SYNTHESE GLOBALE FORME & SANTE ===", flush=True)
    print(generer_synthese(top_sur or foot_partiels, toutes_alertes), flush=True)


if __name__ == "__main__":
    executer_analyse()
