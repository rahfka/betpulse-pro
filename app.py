import csv
import inspect
import math
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, redirect, render_template_string, request, url_for

import sports_skills.betting as bt
import sports_skills.football as fb
import sports_skills.tennis as tn

app = Flask(__name__, static_folder="static", static_url_path="/static")

# =====================================================================
# CONFIGURATION GENERALE (Clés optionnelles + Toutes compétitions)
# =====================================================================
_DOSSIER_SCRIPT = os.path.dirname(os.path.abspath(__file__))
FICHIER_HISTORIQUE = os.path.join(_DOSSIER_SCRIPT, "historique_paris.csv")

OPENROUTER_API_KEY_LOCAL = ""
GEMINI_API_KEY_LOCAL = ""

COMPETITIONS_FOOT = [
    {
        "slug": "ligue-1",
        "tsdb_id": "4334",
        "label": "Ligue 1",
        "aliases": {"ligue-1", "fra.1", "french ligue 1", "ligue 1", "ligue 1 mcdonald's"},
    },
    {
        "slug": "la-liga",
        "tsdb_id": "4335",
        "label": "La Liga",
        "aliases": {"la-liga", "esp.1", "spanish la liga", "la liga", "laliga", "primera division"},
    },
    {
        "slug": "bundesliga",
        "tsdb_id": "4331",
        "label": "Bundesliga",
        "aliases": {"bundesliga", "ger.1", "german bundesliga", "1. bundesliga"},
    },
    {
        "slug": "portuguese-liga",
        "tsdb_id": "4344",
        "label": "Liga Portugal",
        "aliases": {
            "portuguese-liga", "primeira-liga", "liga-portugal", "por.1",
            "portuguese liga", "liga portugal", "primeira liga", "liga portugal betclic",
        },
    },
    {
        "slug": "premier-league",
        "tsdb_id": "4328",
        "label": "Premier League",
        "aliases": {"premier-league", "eng.1", "english premier league", "premier league", "epl"},
    },
    {
        "slug": "serie-a",
        "tsdb_id": "4332",
        "label": "Serie A",
        "aliases": {"serie-a", "ita.1", "italian serie a", "serie a"},
    },
    {
        "slug": "uefa-champions-league",
        "tsdb_id": "4480",
        "label": "Ligue des Champions",
        "aliases": {
            "uefa-champions-league", "champions-league", "uefa.champions",
            "uefa champions league", "ligue des champions", "ucl",
        },
    },
    {
        "slug": "uefa-europa-league",
        "tsdb_id": "4481",
        "label": "Ligue Europa",
        "aliases": {
            "uefa-europa-league", "europa-league", "uefa.europa",
            "uefa europa league", "ligue europa", "uel",
        },
    },
]

SAISON_FALLBACK_YEAR = "2026"
N_DERNIERS_MATCHS = 10
FENETRE_HEURES = 48  # Matchs à venir sur 2 jours (48h)
MAX_SCAN_SANTE_MATCHS = 25

FRACTION_KELLY = 0.25
MISE_MIN_UNITES = 0.25
MISE_MAX_UNITES = 2.0
SEUIL_EV_POSITIF_PCT = 3.0

_CACHE_FOOT_SCHED = {}
_CACHE_SEASON_ID = {}
_CACHE_COMP_EVENTS = {}
_DERNIERES_ERREURS_FOOT = []
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
    "synthese": "Clique sur « Actualiser » pour lancer l'analyse complète (fenêtre 48h / 2 jours).",
}


def _get_openrouter_key():
    return (os.getenv("OPENROUTER_API_KEY") or OPENROUTER_API_KEY_LOCAL or "").strip()


def _get_gemini_key():
    return (os.getenv("GEMINI_API_KEY") or GEMINI_API_KEY_LOCAL or "").strip()


def _noter_erreur_foot(msg):
    texte = str(msg).strip()
    if texte and texte not in _DERNIERES_ERREURS_FOOT and len(_DERNIERES_ERREURS_FOOT) < 4:
        _DERNIERES_ERREURS_FOOT.append(texte)


def _appeler_avec_signatures(fn, candidats_kwargs):
    if not callable(fn):
        return None
    nom_fn = getattr(fn, "__name__", "fb_fn")
    try:
        sig = inspect.signature(fn)
        params_acceptes = set(sig.parameters.keys())
        accepte_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
    except Exception:
        params_acceptes = set()
        accepte_var_kw = True

    for kwargs in candidats_kwargs:
        if not accepte_var_kw and params_acceptes:
            if not set(kwargs.keys()).issubset(params_acceptes):
                continue
        try:
            res = fn(**kwargs)
            if res is not None:
                return res
        except Exception as exc:
            _noter_erreur_foot(f"{nom_fn}: {exc}")
    return None


def _extraire_evenements_depuis_reponse(payload):
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
    for cle in ("events", "matches", "fixtures", "games", "schedules", "results", "items"):
        val = data.get(cle)
        if isinstance(val, list):
            for item in val:
                if not isinstance(item, dict):
                    continue
                if any(k in item for k in ("competitors", "teams", "home_team", "away_team", "home", "away")):
                    evenements.append(item)
                else:
                    evenements.extend(_extraire_evenements_depuis_reponse(item))

    for cle_bloc in ("leagues", "competitions", "seasons", "days", "tournaments"):
        bloc = data.get(cle_bloc)
        if isinstance(bloc, list):
            for sub in bloc:
                if isinstance(sub, dict):
                    meta_comp = {}
                    for m_key in ("slug", "id", "name", "short_name", "abbreviation", "league_slug", "competition_id"):
                        if sub.get(m_key) and f"_bloc_{m_key}" not in meta_comp:
                            meta_comp[f"_bloc_{m_key}"] = sub.get(m_key)
                    for ev in _extraire_evenements_depuis_reponse(sub):
                        copie = dict(ev)
                        for mk, mv in meta_comp.items():
                            copie.setdefault(mk, mv)
                        evenements.append(copie)

    return evenements


def _normaliser_event_tsdb(ev_raw, comp_cfg, est_clos=False):
    """Normalise un événement TheSportsDB vers le format standard attendu."""
    if not isinstance(ev_raw, dict):
        return None
    h_nom = (ev_raw.get("strHomeTeam") or "").strip()
    a_nom = (ev_raw.get("strAwayTeam") or "").strip()
    if not h_nom or not a_nom:
        return None

    date_str = (ev_raw.get("strTimestamp") or "").strip()
    if not date_str:
        d_ev = (ev_raw.get("dateEvent") or "").strip()
        t_ev = (ev_raw.get("strTime") or "12:00:00").strip()
        if d_ev:
            date_str = f"{d_ev}T{t_ev}+00:00"

    h_score = ev_raw.get("intHomeScore")
    a_score = ev_raw.get("intAwayScore")
    statut_raw = str(ev_raw.get("strStatus") or "").strip().lower()

    if est_clos or (h_score is not None and a_score is not None and statut_raw in ("ft", "aet", "pen", "finished", "match finished")):
        statut = "closed"
    elif statut_raw in ("postponed", "pst", "cancelled", "canc", "abd", "abandoned"):
        statut = "postponed"
    elif statut_raw in ("1h", "2h", "ht", "live", "in play"):
        statut = "in_progress"
    else:
        statut = "not_started"

    return {
        "id": str(ev_raw.get("idEvent") or ""),
        "start_time": date_str,
        "status": statut,
        "league_slug": comp_cfg["slug"],
        "competitors": [
            {
                "qualifier": "home",
                "homeAway": "home",
                "score": h_score,
                "team": {
                    "id": str(ev_raw.get("idHomeTeam") or ""),
                    "name": h_nom,
                    "short_name": h_nom,
                },
            },
            {
                "qualifier": "away",
                "homeAway": "away",
                "score": a_score,
                "team": {
                    "id": str(ev_raw.get("idAwayTeam") or ""),
                    "name": a_nom,
                    "short_name": a_nom,
                },
            },
        ],
    }


def _fetch_tsdb_events(comp_cfg):
    """Récupère les matchs à venir et récents via TheSportsDB (clé publique 3)."""
    tsdb_id = comp_cfg.get("tsdb_id")
    if not tsdb_id:
        return []

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
        )
    }
    evenements = []

    # 1. Prochains matchs de la compétition
    try:
        r_next = requests.get(
            f"https://www.thesportsdb.com/api/v1/json/3/eventsnextleague.php?id={tsdb_id}",
            headers=headers,
            timeout=10,
        )
        if r_next.status_code == 200:
            for ev in (r_next.json().get("events") or []):
                ev_n = _normaliser_event_tsdb(ev, comp_cfg, est_clos=False)
                if ev_n:
                    evenements.append(ev_n)
    except Exception:
        pass

    # 2. Derniers matchs joués (pour la forme et xG / Poisson)
    try:
        r_last = requests.get(
            f"https://www.thesportsdb.com/api/v1/json/3/eventspastleague.php?id={tsdb_id}",
            headers=headers,
            timeout=10,
        )
        if r_last.status_code == 200:
            for ev in (r_last.json().get("events") or []):
                ev_n = _normaliser_event_tsdb(ev, comp_cfg, est_clos=True)
                if ev_n:
                    evenements.append(ev_n)
    except Exception:
        pass

    return evenements


def recuperer_evenements_competition(comp_cfg):
    slug = comp_cfg["slug"]
    if slug in _CACHE_COMP_EVENTS:
        return _CACHE_COMP_EVENTS[slug]

    evenements = []

    # 1. Essai via sports_skills.football si disponible
    for fn_name in ("get_season_schedule", "get_daily_schedule", "get_team_schedule"):
        fn = getattr(fb, fn_name, None)
        if not callable(fn):
            continue
        res = _appeler_avec_signatures(
            fn,
            [
                {"season_id": f"{slug}-{SAISON_FALLBACK_YEAR}"},
                {"league_slug": slug},
                {"competition_id": slug},
            ],
        )
        evs = _extraire_evenements_depuis_reponse(res)
        if evs:
            evenements.extend(evs)
            break

    # 2. Source principale fiable pour les matchs à venir + forme : TheSportsDB
    evenements.extend(_fetch_tsdb_events(comp_cfg))

    dedup = {}
    for idx, ev in enumerate(evenements):
        if not isinstance(ev, dict):
            continue
        eid = str(ev.get("id") or ev.get("event_id") or f"{slug}_idx_{idx}")
        dedup[eid] = ev

    liste_finale = list(dedup.values())
    _CACHE_COMP_EVENTS[slug] = liste_finale
    return liste_finale


def get_foot_schedule_cached(team_id, league_slug="ligue-1", season_year=SAISON_FALLBACK_YEAR):
    cle = (str(team_id), str(league_slug), str(season_year))
    if cle not in _CACHE_FOOT_SCHED:
        res = _appeler_avec_signatures(
            fb.get_team_schedule,
            [
                {"team_id": cle[0], "league_slug": cle[1], "season_year": cle[2], "competition_id": cle[1]},
                {"team_id": cle[0], "competition_id": cle[1]},
                {"team_id": cle[0]},
            ],
        )
        _CACHE_FOOT_SCHED[cle] = res if isinstance(res, dict) else {"data": {"events": []}}
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


def _extraire_date_evenement(ev):
    for champ in ("start_time", "date", "commence_time", "startDate", "utc_date"):
        if ev.get(champ):
            return str(ev.get(champ))
    return ""


def _lire_statut_evenement(ev):
    statut = ev.get("status")
    if isinstance(statut, dict):
        statut = statut.get("type") or statut.get("name") or statut.get("state") or statut.get("detail") or ""
    return str(statut or "").strip().lower()


def _est_match_clos(ev):
    statut = _lire_statut_evenement(ev)
    return statut in (
        "closed", "final", "post", "completed",
        "finished", "status_final", "full_time", "ft"
    )


def _est_match_a_venir(ev):
    if _est_match_clos(ev):
        return False
    statut = _lire_statut_evenement(ev)
    if statut in ("in_progress", "live", "halftime", "ht", "canceled", "cancelled", "postponed"):
        return False
    return True


def dans_fenetre_heures(date_str, heures_max=FENETRE_HEURES):
    if heures_max is None or not date_str:
        return True
    try:
        s = str(date_str).strip()
        maintenant = datetime.now(timezone.utc)
        if len(s) == 10 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            dt_jour = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            jour_debut = maintenant.replace(hour=0, minute=0, second=0, microsecond=0)
            jour_fin = maintenant.replace(hour=23, minute=59, second=59, microsecond=0) + timedelta(hours=heures_max)
            return jour_debut <= dt_jour <= jour_fin

        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        limite = maintenant + timedelta(hours=heures_max)
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
        "injury", "injured", "doubt", "withdraw",
    ]
    mots_exclus = [
        "roland-garros", "roland garros", "rétrospective", "retrospective",
        "histoire", "archive", "2022", "2023", "2024", "2025",
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


def _extraire_score_numerique(val):
    if val is None:
        return None
    if isinstance(val, dict):
        val = val.get("value") or val.get("score")
    if val is None:
        return None
    try:
        return int(float(str(val).strip()))
    except Exception:
        return None


def stats_foot_detail(team_id, team_name="", league_slug="ligue-1", season_year=SAISON_FALLBACK_YEAR, n_derniers=10, events_comp=None):
    events = []
    if events_comp:
        events = events_comp
    elif team_id:
        sched = get_foot_schedule_cached(team_id, league_slug=league_slug, season_year=season_year)
        events = _extraire_evenements_depuis_reponse(sched)

    joues = [ev for ev in events if _est_match_clos(ev)]
    joues.sort(key=lambda ev: _extraire_date_evenement(ev))

    bp_all, bc_all = [], []
    bp_home, bc_home = [], []
    bp_away, bc_away = [], []
    v = n = d = 0

    nom_cible = (team_name or "").strip().lower()

    for ev in joues:
        comps = ev.get("competitors", [])
        moi = adv = None
        mon_qual = None
        match_equipe = False

        for c in comps:
            t_obj = c.get("team", {}) if isinstance(c.get("team"), dict) else c
            tid = str(t_obj.get("id") or c.get("id") or "")
            t_nm = str(t_obj.get("name") or t_obj.get("short_name") or "").strip().lower()
            sc = _extraire_score_numerique(c.get("score"))
            qual = str(c.get("qualifier") or c.get("homeAway") or "").lower()

            est_moi = (
                (team_id and tid and tid == str(team_id))
                or (nom_cible and t_nm and (t_nm == nom_cible or nom_cible in t_nm or t_nm in nom_cible))
            )
            if est_moi and sc is not None:
                moi = sc
                mon_qual = qual
                match_equipe = True
            elif sc is not None and adv is None and not est_moi:
                adv = sc

        if match_equipe and moi is not None and adv is not None:
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

    bp_base = moyenne_ponderee(bp_all, 1.35)
    bc_base = moyenne_ponderee(bc_all, 1.20)

    return {
        "forme": f"{v}V-{n}N-{d}D" if (v + n + d) > 0 else "Forme récente",
        "bp_home": moyenne_ponderee(bp_home, bp_base),
        "bc_home": moyenne_ponderee(bc_home, bc_base),
        "bp_away": moyenne_ponderee(bp_away, bp_base),
        "bc_away": moyenne_ponderee(bc_away, bc_base),
    }


def simuler_poisson_foot(lam_h, lam_a):
    p_h = p_n = p_a = p_over_25 = p_btts = 0.0
    meilleur_score = ("1-0", 0.0)

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
            if h > 0 and a > 0:
                p_btts += pr
            if pr > meilleur_score[1]:
                meilleur_score = (f"{h}-{a}", pr)

    tot = p_h + p_n + p_a
    p_h, p_n, p_a = p_h / tot, p_n / tot, p_a / tot
    return p_h, p_n, p_a, p_over_25, max(0.0, 1.0 - p_over_25), p_btts, meilleur_score[0]


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


def analyser_cote_us(details_str, fair_prob, alerte_grave=False):
    m = re.search(r"([+-]\d+)", str(details_str))
    if not m:
        return "", None, "Neutre", None, 0.0
    cote_us = m.group(1)
    conv = bt.convert_odds(odds=cote_us, from_format="american")
    data_c = conv.get("data", {}) if isinstance(conv, dict) else {}
    imp = data_c.get("implied_probability") or data_c.get("probability")
    dec = data_c.get("decimal")

    try:
        dec_val = float(dec) if dec is not None else None
    except Exception:
        dec_val = None

    if dec_val and dec_val > 1.0:
        ev_pct = (fair_prob * dec_val - 1.0) * 100.0
        mise_u = calculer_mise_kelly(fair_prob, dec_val, ev_pct, alerte_grave=alerte_grave)
        if ev_pct >= SEUIL_EV_POSITIF_PCT:
            label_val = f"+Valeur ({ev_pct:+.1f}%)"
        elif ev_pct <= -8.0:
            label_val = f"Cote courte ({ev_pct:+.1f}%)"
        else:
            label_val = f"Ajusté ({ev_pct:+.1f}%)"

        imp_txt = f", implicite {float(imp)*100:.1f}%" if imp is not None else ""
        mise_txt = f" | Mise: {mise_u:.2f}u" if mise_u > 0 else ""
        txt = f" | Cote {cote_us} (dec {dec_val:.2f}{imp_txt}) -> {label_val}{mise_txt}"
        return txt, ev_pct, label_val, dec_val, mise_u

    return f" | Cote {cote_us}", None, "Neutre", dec_val, 0.0


def niveau_prudence(proba_pct, nb_alertes_sante, label_val="Neutre", alerte_grave=False):
    if alerte_grave:
        return "PRUDENT (alerte blessure)"
    if "Cote courte" in label_val:
        return "PRUDENT (cote courte)"
    if proba_pct >= 72 and nb_alertes_sante == 0:
        return "ELEVE"
    if proba_pct >= 54 and nb_alertes_sante <= 1:
        return "MODERE"
    return "PRUDENT"


def _extraire_home_away(ev):
    h_info = {}
    a_info = {}
    for c in ev.get("competitors", []):
        qual = str(c.get("qualifier") or c.get("homeAway") or "").strip().lower()
        t_obj = c.get("team") if isinstance(c.get("team"), dict) else c
        if qual == "home":
            h_info = t_obj or {}
        elif qual == "away":
            a_info = t_obj or {}
    if (not h_info or not a_info) and len(ev.get("competitors", [])) >= 2:
        c0, c1 = ev["competitors"][0], ev["competitors"][1]
        if not h_info:
            h_info = c0.get("team") if isinstance(c0.get("team"), dict) else c0
        if not a_info:
            a_info = c1.get("team") if isinstance(c1.get("team"), dict) else c1
    if not h_info and isinstance(ev.get("home_team"), dict):
        h_info = ev["home_team"]
    if not a_info and isinstance(ev.get("away_team"), dict):
        a_info = ev["away_team"]
    return h_info or {}, a_info or {}


def analyser_tous_matchs_foot(competitions, n_derniers=10, heures_max=FENETRE_HEURES):
    vus_evenements = set()
    matchs_candidats = []
    diagnostics = []

    for comp in competitions:
        slug = comp["slug"]
        label_comp = comp.get("label", slug)
        evenements = recuperer_evenements_competition(comp)
        nb_bruts = len(evenements)
        nb_retenus_comp = 0

        for ev in evenements:
            if not _est_match_a_venir(ev):
                continue
            ev_id = str(ev.get("id") or ev.get("event_id") or "")
            cle_ev = f"{slug}_{ev_id}" if ev_id else f"{slug}_{_extraire_date_evenement(ev)}_{len(vus_evenements)}"
            if cle_ev in vus_evenements:
                continue

            start_str = _extraire_date_evenement(ev)
            if not dans_fenetre_heures(start_str, heures_max):
                continue

            h_info, a_info = _extraire_home_away(ev)
            if not h_info or not a_info:
                continue

            vus_evenements.add(cle_ev)
            nb_retenus_comp += 1
            matchs_candidats.append((slug, label_comp, evenements, ev, h_info, a_info))

        diagnostics.append(f"{label_comp}: {nb_retenus_comp}/{nb_bruts}")

    matchs_candidats.sort(key=lambda item: _extraire_date_evenement(item[3]))

    resultats = []
    alertes_globales = {}
    scans_sante_effectues = 0

    for slug, label_comp, evenements_comp, ev, h_info, a_info in matchs_candidats:
        ev_id = str(ev.get("id") or ev.get("event_id") or "")
        start_str = _extraire_date_evenement(ev)

        h_nom = (
            h_info.get("short_name")
            or h_info.get("shortDisplayName")
            or h_info.get("name")
            or h_info.get("displayName")
            or "Home"
        )
        a_nom = (
            a_info.get("short_name")
            or a_info.get("shortDisplayName")
            or a_info.get("name")
            or a_info.get("displayName")
            or "Away"
        )

        st_h = stats_foot_detail(
            h_info.get("id", ""), team_name=h_nom, league_slug=slug, n_derniers=n_derniers, events_comp=evenements_comp
        )
        st_a = stats_foot_detail(
            a_info.get("id", ""), team_name=a_nom, league_slug=slug, n_derniers=n_derniers, events_comp=evenements_comp
        )

        lam_h = max((st_h["bp_home"] + st_a["bc_away"]) / 2, 0.35)
        lam_a = max((st_a["bp_away"] + st_h["bc_home"]) / 2, 0.35)
        p_h, p_n, p_a, p_ov25, p_un25, p_btts, score_prob = simuler_poisson_foot(lam_h, lam_a)

        options = [
            (f"Victoire {h_nom}", p_h),
            ("Match nul", p_n),
            (f"Victoire {a_nom}", p_a),
        ]
        choix, prob = max(options, key=lambda x: x[1])

        titres_sante = []
        if scans_sante_effectues < MAX_SCAN_SANTE_MATCHS:
            scans_sante_effectues += 1
            for cible in [h_nom, a_nom]:
                autres = [x for x in [h_nom, a_nom] if x != cible]
                titres_sante += scanner_sante_cible(cible, max_titres=2, autres_cibles=autres)
            titres_sante = list(dict.fromkeys(titres_sante))[:3]

        grave = alerte_grave_detectee(titres_sante)
        malus_sante = min(len(titres_sante) * 0.02 + (0.04 if grave else 0.0), 0.10)
        prob_ajustee = max(0.05, prob - malus_sante)

        if titres_sante:
            alertes_globales[f"{h_nom} vs {a_nom}"] = titres_sante

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

        ov_pct = round(p_ov25 * 100, 1)
        un_pct = round(p_un25 * 100, 1)
        btts_pct = round(p_btts * 100, 1)

        if ov_pct >= un_pct:
            buts_label = "+2.5 buts"
            buts_proba = ov_pct
        else:
            buts_label = "-2.5 buts"
            buts_proba = un_pct

        buts_opt = f"{buts_label} ({buts_proba:.0f}%)"
        focus_buts = " [Signal buts fort]" if max(ov_pct, un_pct) >= 75 else ""

        proba_pct = round(prob_ajustee * 100, 1)
        prudence = niveau_prudence(proba_pct, len(titres_sante), label_val, alerte_grave=grave)

        resultats.append({
            "sport": "FOOT",
            "competition": label_comp,
            "competition_slug": slug,
            "fiable": True,
            "event_id": ev_id,
            "match": f"{h_nom} vs {a_nom}",
            "date_match": start_str[:16].replace("T", " ") if len(start_str) >= 16 else (start_str[:10] or "À venir"),
            "detail": (
                f"[{label_comp}] {h_nom} [{st_h['forme']}] vs {a_nom} [{st_a['forme']}] | "
                f"xG: {lam_h:.2f}-{lam_a:.2f} (Score type {score_prob}) | "
                f"Buts: {buts_opt}{focus_buts} | BTTS: {btts_pct:.0f}% | "
                f"Alertes 3j: {len(titres_sante)}{cote_txt}"
            ),
            "choix": choix,
            "proba": proba_pct,
            "prudence": prudence,
            "a_valeur": "+Valeur" in label_val,
            "ev_pct": ev_val,
            "cote_dec": cote_dec,
            "mise_u": mise_u,
            "buts_choix": buts_label,
            "buts_proba": buts_proba,
        })

    diag_txt = " · ".join(diagnostics)
    return resultats, alertes_globales, diag_txt


def analyser_tennis(heures_max=FENETRE_HEURES):
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
    doubles_liste = []
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
            is_doubles = "Doubles" in draw_name

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

                if is_doubles:
                    s1 = comps[0].get("seed")
                    s2 = comps[1].get("seed")
                    doubles_liste.append(
                        f"{j1} (seed {s1 or '-'}) vs {j2} (seed {s2 or '-'}) "
                        f"[{t_name} | {date_m[:16]} UTC]"
                    )
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
                    "date_match": date_m[:10],
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

    return simples_fiables, simples_nc, doubles_liste, alertes_tennis


def selectionner_meilleurs_paris_filtres(principaux, foot_res):
    filtres = []
    for it in principaux:
        pr = it.get("prudence", "")
        if "blessure" in pr.lower():
            continue
        if it.get("a_valeur") and it.get("mise_u", 0.0) > 0:
            filtres.append({
                "type": "VALEUR (EV+)",
                "sport": it["sport"],
                "match": it["match"],
                "date_match": it["date_match"],
                "choix": it["choix"],
                "proba": it["proba"],
                "prudence": pr,
                "mise_u": it.get("mise_u", 0.0),
            })
        elif pr in ("ELEVE", "MODERE"):
            filtres.append({
                "type": "SÉLECTION" if it.get("cote_dec") else "WATCH-LIST",
                "sport": it["sport"],
                "match": it["match"],
                "date_match": it["date_match"],
                "choix": it["choix"],
                "proba": it["proba"],
                "prudence": pr,
                "mise_u": it.get("mise_u", 0.0),
            })

    for f in foot_res:
        if f.get("buts_proba", 0.0) >= 68.0:
            filtres.append({
                "type": "BUTS",
                "sport": "FOOT",
                "match": f["match"],
                "date_match": f["date_match"],
                "choix": f["buts_choix"],
                "proba": f["buts_proba"],
                "prudence": "MODERE (buts)",
                "mise_u": 0.0,
            })

    filtres.sort(key=lambda x: (x["type"] != "VALEUR (EV+)", -x["proba"]))
    return filtres[:12]


def _calculer_pnl_ligne(row):
    statut = row.get("statut")
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


def mettre_a_jour_et_sauver_historique(pistes, foot_res, competitions):
    champs = [
        "date_analyse", "sport", "event_id", "match", "date_match",
        "choix", "proba", "prudence", "cote_dec", "mise_u", "pnl_u", "statut",
    ]
    lignes_existantes = []
    cles_deja_enregistrees = set()

    if os.path.exists(FICHIER_HISTORIQUE):
        with open(FICHIER_HISTORIQUE, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                lignes_existantes.append(row)
                cle = f"{row.get('sport')}_{row.get('event_id')}_{row.get('choix')}"
                cles_deja_enregistrees.add(cle)

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
    for comp in competitions:
        try:
            events = recuperer_evenements_competition(comp)
            for ev in events:
                if _est_match_clos(ev):
                    eid = str(ev.get("id") or ev.get("event_id") or "")
                    comps = ev.get("competitors", [])
                    if len(comps) == 2:
                        c0, c1 = comps[0], comps[1]
                        s0 = _extraire_score_numerique(c0.get("score"))
                        s1 = _extraire_score_numerique(c1.get("score"))
                        if s0 is not None and s1 is not None:
                            total_buts = s0 + s1
                            if s0 == s1:
                                resultats_foot_clos[eid] = {
                                    "issue": "DRAW",
                                    "noms": set(),
                                    "total_buts": total_buts,
                                }
                            else:
                                gagnant_comp = c0 if s0 > s1 else c1
                                t_obj = gagnant_comp.get("team", {}) if isinstance(gagnant_comp.get("team"), dict) else gagnant_comp
                                noms = {
                                    str(t_obj.get(k, "")).strip().lower()
                                    for k in ("name", "short_name", "abbreviation", "displayName")
                                    if t_obj.get(k)
                                }
                                resultats_foot_clos[eid] = {
                                    "issue": "WIN",
                                    "noms": noms,
                                    "total_buts": total_buts,
                                }
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
                if res_f:
                    if "+2.5 buts" in choix_l:
                        row["statut"] = "GAGNE" if res_f["total_buts"] > 2 else "PERDU"
                    elif "-2.5 buts" in choix_l:
                        row["statut"] = "GAGNE" if res_f["total_buts"] < 3 else "PERDU"
                    elif res_f["issue"] == "DRAW":
                        row["statut"] = "GAGNE" if "nul" in choix_l else "PERDU"
                    else:
                        gagne = any(nm and nm in choix_l for nm in res_f["noms"])
                        row["statut"] = "GAGNE" if gagne else "PERDU"

        pnl = _calculer_pnl_ligne(row)
        row["pnl_u"] = "" if pnl is None else f"{pnl:+.2f}"

    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M")
    a_enregistrer = list(pistes)
    for f in foot_res:
        if f.get("buts_choix") and f.get("buts_proba") is not None:
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

    with open(FICHIER_HISTORIQUE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=champs)
        writer.writeheader()
        for r in lignes_existantes:
            writer.writerow({k: r.get(k, "") for k in champs})

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
    or_key = _get_openrouter_key()
    if or_key:
        try:
            r = requests.post(
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

    gm_key = _get_gemini_key()
    if gm_key:
        try:
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"gemini-2.5-flash:generateContent?key={gm_key}"
            )
            r = requests.post(
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


def generer_synthese(pistes, alertes_sante, diag_foot=""):
    lignes_pistes = [
        f"- [{p['sport']}] {p['match']} -> {p['choix']} ({p['proba']}%, niveau {p['prudence']}) | {p['detail']}"
        for p in pistes[:8]
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

    lignes = [f"Fenêtre active : {FENETRE_HEURES}h (2 jours) · Compétitions Foot : 8"]
    if diag_foot:
        lignes.append(f"• Couverture Foot : {diag_foot}")
    for p in pistes[:8]:
        mise_str = f" · Mise {p['mise_u']:.2f}u" if p.get("mise_u", 0.0) > 0 else ""
        lignes.append(f"• [{p['prudence']}] {p['match']} → {p['choix']} ({p['proba']}%{mise_str})")
    for cible, titres in alertes_sante.items():
        lignes.append(f"• Santé {cible} : {titres[0]}")
    return "\n".join(lignes)


def executer_analyse_complete(heures_max=FENETRE_HEURES):
    _CACHE_FOOT_SCHED.clear()
    _CACHE_SEASON_ID.clear()
    _CACHE_COMP_EVENTS.clear()
    _DERNIERES_ERREURS_FOOT.clear()

    foot_res, alertes_f, diag_foot = analyser_tous_matchs_foot(
        COMPETITIONS_FOOT,
        n_derniers=N_DERNIERS_MATCHS,
        heures_max=heures_max,
    )
    t_simples_ok, t_simples_nc, t_doubles, alertes_t = analyser_tennis(heures_max=heures_max)

    principaux = sorted(foot_res + t_simples_ok, key=lambda x: x["proba"], reverse=True)
    partiels = sorted(t_simples_nc, key=lambda x: x["proba"], reverse=True)
    filtres = selectionner_meilleurs_paris_filtres(principaux, foot_res)
    bilan = mettre_a_jour_et_sauver_historique(principaux, foot_res, COMPETITIONS_FOOT)
    toutes_alertes = filtrer_alertes_uniques({**alertes_f, **alertes_t})
    synthese = generer_synthese(principaux, toutes_alertes, diag_foot=diag_foot)

    return {
        "maj": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "principaux": principaux,
        "filtres": filtres,
        "partiels": partiels,
        "doubles": t_doubles,
        "bilan": bilan,
        "synthese": synthese,
    }


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover" />
  <meta name="theme-color" content="#0b0f17" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
  <title>BetPulse Pro</title>
  <link rel="manifest" href="/static/manifest.json" />
  <style>
    :root {
      --bg: #0b0f17;
      --card: #131a26;
      --card-2: #192232;
      --border: #243044;
      --text: #eef2f7;
      --muted: #93a4bc;
      --accent: #22c55e;
      --accent-soft: rgba(34, 197, 94, 0.14);
      --warn: #f59e0b;
      --warn-soft: rgba(245, 158, 11, 0.14);
      --danger: #ef4444;
      --danger-soft: rgba(239, 68, 68, 0.14);
      --info: #38bdf8;
      --info-soft: rgba(56, 189, 248, 0.14);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      padding: 0 0 40px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.45;
    }
    .topbar {
      position: sticky;
      top: 0;
      z-index: 10;
      backdrop-filter: blur(12px);
      background: rgba(11, 15, 23, 0.88);
      border-bottom: 1px solid var(--border);
      padding: 14px 16px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .brand-title { font-size: 1.05rem; font-weight: 700; letter-spacing: -0.01em; }
    .brand-sub { font-size: 0.76rem; color: var(--muted); }
    .btn-refresh {
      background: var(--accent);
      color: #062111;
      border: none;
      border-radius: 10px;
      padding: 10px 14px;
      font-weight: 700;
      font-size: 0.85rem;
      cursor: pointer;
      white-space: nowrap;
    }
    .btn-refresh:active { transform: scale(0.98); }
    .container { max-width: 780px; margin: 0 auto; padding: 16px; }
    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 10px;
      margin-bottom: 16px;
    }
    @media (min-width: 640px) { .kpi-grid { grid-template-columns: repeat(4, 1fr); } }
    .kpi {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 12px;
    }
    .kpi-label { font-size: 0.72rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
    .kpi-val { font-size: 1.25rem; font-weight: 700; margin-top: 4px; }
    .pos { color: var(--accent); }
    .neg { color: var(--danger); }
    h2 {
      font-size: 0.92rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--muted);
      margin: 22px 0 10px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 14px;
      margin-bottom: 10px;
    }
    .card-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 6px;
    }
    .badge {
      font-size: 0.72rem;
      font-weight: 700;
      padding: 3px 8px;
      border-radius: 999px;
      background: var(--info-soft);
      color: var(--info);
    }
    .badge-ev { background: var(--accent-soft); color: var(--accent); }
    .badge-warn { background: var(--warn-soft); color: var(--warn); }
    .badge-danger { background: var(--danger-soft); color: var(--danger); }
    .choix { font-size: 1rem; font-weight: 700; margin: 4px 0; }
    .match { font-size: 0.88rem; color: var(--text); }
    .meta { font-size: 0.78rem; color: var(--muted); margin-top: 6px; }
    .synth {
      white-space: pre-line;
      font-size: 0.86rem;
      color: #dce4ef;
      background: var(--card-2);
      border-radius: 12px;
      padding: 12px;
    }
    .sub-bilan { font-size: 0.8rem; color: var(--muted); margin-top: 8px; }
    .disclaimer {
      margin-top: 20px;
      font-size: 0.75rem;
      color: var(--muted);
      text-align: center;
    }
  </style>
</head>
<body>
  <div class="topbar">
    <div>
      <div class="brand-title">BetPulse Pro</div>
      <div class="brand-sub">{% if data.maj %}MAJ : {{ data.maj }}{% else %}Prêt pour analyse (48h / 2 jours){% endif %}</div>
    </div>
    <form method="post" action="/refresh" style="margin:0;">
      <button type="submit" class="btn-refresh">Actualiser</button>
    </form>
  </div>

  <div class="container">
    <div class="kpi-grid">
      <div class="kpi">
        <div class="kpi-label">PnL Net</div>
        <div class="kpi-val {% if data.bilan.pnl_total > 0 %}pos{% elif data.bilan.pnl_total < 0 %}neg{% endif %}">
          {{ "%+.2f"|format(data.bilan.pnl_total) }}u
        </div>
      </div>
      <div class="kpi">
        <div class="kpi-label">ROI</div>
        <div class="kpi-val {% if data.bilan.roi_pct > 0 %}pos{% elif data.bilan.roi_pct < 0 %}neg{% endif %}">
          {{ "%+.1f"|format(data.bilan.roi_pct) }}%
        </div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Réussite</div>
        <div class="kpi-val">{{ data.bilan.taux }}%</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Suivis</div>
        <div class="kpi-val">{{ data.bilan.verifies }}/{{ data.bilan.total }}</div>
      </div>
    </div>

    <h2>Sélection Filtrée</h2>
    {% if data.filtres %}
      {% for mf in data.filtres %}
      <div class="card">
        <div class="card-head">
          <span class="badge {% if 'VALEUR' in mf.type %}badge-ev{% endif %}">{{ mf.type }} · {{ mf.sport }}</span>
          <span class="badge {% if 'blessure' in mf.prudence|lower or 'courte' in mf.prudence|lower %}badge-danger{% elif mf.prudence == 'ELEVE' %}badge-ev{% else %}badge-warn{% endif %}">
            {{ mf.proba }}% · {{ mf.prudence }}
          </span>
        </div>
        <div class="choix">{{ mf.choix }}{% if mf.mise_u and mf.mise_u > 0 %} — Mise {{ "%.2f"|format(mf.mise_u) }}u{% endif %}</div>
        <div class="match">{{ mf.match }} <span class="meta">({{ mf.date_match }})</span></div>
      </div>
      {% endfor %}
    {% else %}
      <div class="card"><div class="meta">Aucune piste validée par les filtres actuels. Clique sur Actualiser.</div></div>
    {% endif %}

    <h2>Toutes les Pistes (Données Complètes)</h2>
    {% for it in data.principaux %}
    <div class="card">
      <div class="card-head">
        <span class="badge">{{ it.sport }}{% if it.competition %} · {{ it.competition }}{% endif %}</span>
        <span class="badge {% if 'blessure' in it.prudence|lower or 'courte' in it.prudence|lower %}badge-danger{% elif it.prudence == 'ELEVE' %}badge-ev{% else %}badge-warn{% endif %}">
          {{ it.proba }}% · {{ it.prudence }}
        </span>
      </div>
      <div class="choix">{{ it.choix }}{% if it.mise_u and it.mise_u > 0 %} — Mise {{ "%.2f"|format(it.mise_u) }}u{% endif %}</div>
      <div class="match">{{ it.match }} ({{ it.date_match }})</div>
      <div class="meta">{{ it.detail }}</div>
    </div>
    {% endfor %}

    <h2>Bilan Détaillé</h2>
    <div class="card">
      <div class="sub-bilan">
        Foot 1X2 : <strong>{{ data.bilan.bilan_foot }}</strong> ·
        Buts Foot : <strong>{{ data.bilan.bilan_buts }}</strong> ·
        Tennis : <strong>{{ data.bilan.bilan_tennis }}</strong> ·
        En attente : <strong>{{ data.bilan.en_attente }}</strong>
      </div>
    </div>

    <h2>Synthèse Forme &amp; Santé</h2>
    <div class="card">
      <div class="synth">{{ data.synthese }}</div>
    </div>

    <div class="disclaimer">Outil d'aide à l'analyse statistique — aucun résultat n'est garanti.</div>
  </div>

  <script>
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/static/sw.js").catch(() => {});
    }
  </script>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def index():
    return render_template_string(HTML_TEMPLATE, data=_DERNIER_SNAPSHOT)


@app.route("/refresh", methods=["GET", "POST"])
def refresh():
    global _DERNIER_SNAPSHOT
    if request.method == "GET":
        return redirect(url_for("index"))
    try:
        _DERNIER_SNAPSHOT = executer_analyse_complete(heures_max=FENETRE_HEURES)
    except Exception as exc:
        _DERNIER_SNAPSHOT["synthese"] = f"Erreur lors de l'analyse : {exc}"
    return render_template_string(HTML_TEMPLATE, data=_DERNIER_SNAPSHOT)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
