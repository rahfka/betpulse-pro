import csv
import importlib
import inspect
import json
import math
import os
import re
import sys
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, make_response, redirect, render_template_string, url_for

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


@dataclass(frozen=True)
class Config:
    saison_foot: str = os.getenv("SAISON_FOOT", "2026").strip() or "2026"
    n_derniers_matchs: int = int(os.getenv("N_DERNIERS_MATCHS", "8"))
    fenetre_heures: int = int(os.getenv("FENETRE_HEURES", "48"))
    timeout_http: int = int(os.getenv("TIMEOUT_HTTP", "3"))
    cache_ttl_sec: int = int(os.getenv("CACHE_TTL_SEC", "900"))


CFG = Config()


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
FICHIER_RAPPORT = "/tmp/rapport.json"

COMPETITIONS_CIBLES = [
    {"slug": "ligue-1", "label": "Ligue 1", "mots": ("ligue 1", "ligue-1", "fra.1", "france"), "equipes": ["160", "174", "176"]},
    {"slug": "la-liga", "label": "Liga Espagnole", "mots": ("la liga", "laliga", "la-liga", "esp.1", "spain"), "equipes": ["86", "83", "81"]},
    {"slug": "bundesliga", "label": "Bundesliga", "mots": ("bundesliga", "ger.1", "germany"), "equipes": ["132", "124", "167"]},
    {"slug": "portuguese-liga", "label": "Liga Portugaise", "mots": ("liga portugal", "primeira liga", "portuguese-liga", "por.1", "portugal"), "equipes": ["1929", "1927", "1930"]},
    {"slug": "premier-league", "label": "Premier League", "mots": ("premier league", "premier-league", "eng.1", "england"), "equipes": ["364", "359", "360"]},
    {"slug": "serie-a", "label": "Serie A", "mots": ("serie a", "serie-a", "ita.1", "italy"), "equipes": ["111", "110", "103"]},
    {"slug": "uefa-champions-league", "label": "Ligue des Champions", "mots": ("champions league", "champions-league", "uefa.champions", "ucl"), "equipes": ["160", "86", "83", "364"]},
    {"slug": "uefa-europa-league", "label": "Europa League", "mots": ("europa league", "europa-league", "uefa.europa", "uel"), "equipes": ["176", "174", "360", "111"]},
]

STATUTS_TERMINES = {"closed", "final", "post", "completed", "finished", "ft", "aet", "pen", "status_final", "status_post"}
STATUTS_TERMINES_OU_EN_COURS = STATUTS_TERMINES | {"in_progress", "live"}

_CACHE_FOOT_SCHED = {}
_CACHE_SANTE = {}
_STATE_LOCK = threading.Lock()
_ANALYSE_EN_COURS = False

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

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
    )
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
    except Exception:
        pass


def _sauvegarder_rapport_disque(rapport):
    try:
        with open(FICHIER_RAPPORT, "w", encoding="utf-8") as f:
            json.dump(rapport, f, ensure_ascii=False)
    except Exception:
        pass


def _cache_get(cache_dict, cle):
    entree = cache_dict.get(cle)
    if not entree:
        return None
    ts, val = entree
    if (datetime.now(timezone.utc).timestamp() - ts) > CFG.cache_ttl_sec:
        cache_dict.pop(cle, None)
        return None
    return val


def _cache_set(cache_dict, cle, val):
    cache_dict[cle] = (datetime.now(timezone.utc).timestamp(), val)


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


def get_foot_schedule_cached(team_id, league_slug="ligue-1", season_year="2026"):
    cle = (str(team_id), str(league_slug), str(season_year))
    cached = _cache_get(_CACHE_FOOT_SCHED, cle)
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
            except Exception:
                pass
        except Exception as e:
            print(f"[FOOT] get_team_schedule({cle}) erreur: {e}", flush=True)

    _cache_set(_CACHE_FOOT_SCHED, cle, res)
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


def _est_match_a_venir_dans_fenetre(ev, heures_max):
    statut = str(ev.get("status") or "").strip().lower()
    if statut in STATUTS_TERMINES_OU_EN_COURS:
        return False

    date_str = str(ev.get("start_time") or ev.get("date") or "").strip()
    if not date_str:
        return False

    dt = _parse_date_utc(date_str)
    if dt is None:
        return False

    maintenant = datetime.now(timezone.utc)
    return (maintenant - timedelta(minutes=15)) <= dt <= (maintenant + timedelta(hours=heures_max))


def poisson_prob(k, lam):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (math.exp(-lam) * (lam ** k)) / math.factorial(k)


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


def moyenne_ponderee(valeurs, defaut):
    if not valeurs:
        return defaut
    n = len(valeurs)
    poids = [1.0 + (i * 0.25) for i in range(n)]
    return sum(v * p for v, p in zip(valeurs, poids)) / sum(poids)


def stats_foot_detail(team_id, team_name="", events_override=None, n_derniers=8):
    events = list(events_override or [])
    joues = [ev for ev in events if str(ev.get("status", "")).lower() in STATUTS_TERMINES]
    joues.sort(key=lambda ev: str(ev.get("start_time") or ev.get("date") or ""))

    bp_all, bc_all = [], []
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

        scores_par_role = []
        moi = None
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
            scores_par_role.append((tid, tnm, sc_int))
            if (team_id and tid and tid == str(team_id)) or (nom_l and tnm and (tnm == nom_l or nom_l in tnm or tnm in nom_l)):
                if moi is None:
                    moi = sc_int

        adv = None
        if moi is not None and len(scores_par_role) >= 2:
            for tid, tnm, sc_int in scores_par_role:
                if not ((team_id and tid and tid == str(team_id)) or (nom_l and tnm and tnm == nom_l)):
                    adv = sc_int
                    break
            if adv is None:
                for _, _, sc_int in scores_par_role:
                    if sc_int != moi:
                        adv = sc_int
                        break
                if adv is None:
                    adv = moi

        if moi is not None and adv is not None:
            bp_all.append(moi)
            bc_all.append(adv)
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
    a_historique = (v + n + d) > 0

    return {
        "forme": f"{v}V-{n}N-{d}D" if a_historique else "Forme récente",
        "a_historique": a_historique,
        "bp": moyenne_ponderee(bp_all, 1.25),
        "bc": moyenne_ponderee(bc_all, 1.20),
    }


def _requete_rss(requete_brute, max_titres=2):
    q = urllib.parse.quote(requete_brute)
    url = f"https://news.google.com/rss/search?q={q}&hl=fr&gl=FR&ceid=FR:fr"
    titres = []
    try:
        r = requests.get(url, headers=_HTTP_HEADERS, timeout=CFG.timeout_http)
        if r.status_code == 200:
            root = ET.fromstring(r.text)
            for item in root.findall(".//item")[:4]:
                t = (item.findtext("title") or "").strip()
                if t:
                    titres.append(t)
    except Exception:
        pass
    return titres[:max_titres]


def scanner_sante_cible(cible, max_titres=2):
    cle = (str(cible).strip().lower(), max_titres)
    cached = _cache_get(_CACHE_SANTE, cle)
    if cached is not None:
        return list(cached)

    res = _requete_rss(f'"{cible}" (blessure OR forfait OR incertain OR absent) when:3d', max_titres)
    _cache_set(_CACHE_SANTE, cle, res)
    return list(res)


def _construire_entree_foot(ev, label_comp, events_comp):
    ev_id = str(ev.get("id") or "")
    start_str = str(ev.get("start_time") or ev.get("date") or "")
    h_info, a_info = _normaliser_home_away(ev)
    h_nom = h_info.get("short_name") or h_info.get("name") or "Home"
    a_nom = a_info.get("short_name") or a_info.get("name") or "Away"

    st_h = stats_foot_detail(h_info.get("id", ""), h_nom, events_comp, CFG.n_derniers_matchs)
    st_a = stats_foot_detail(a_info.get("id", ""), a_nom, events_comp, CFG.n_derniers_matchs)
    fiable = st_h["a_historique"] or st_a["a_historique"]

    lam_h = max(((st_h["bp"] * 1.10) + st_a["bc"]) / 2, 0.35)
    lam_a = max(((st_a["bp"] * 0.90) + st_h["bc"]) / 2, 0.35)
    p_h, p_n, p_a, p_ov25, p_un25 = simuler_poisson_foot(lam_h, lam_a)

    choix, prob = max(
        [(f"Victoire {h_nom}", p_h), ("Match nul", p_n), (f"Victoire {a_nom}", p_a)],
        key=lambda x: x[1],
    )

    proba_pct = round(max(0.05, prob) * 100, 1)
    buts_label = "+2.5 buts" if p_ov25 >= p_un25 else "-2.5 buts"
    buts_proba = round(max(p_ov25, p_un25) * 100, 1)

    return {
        "sport": "FOOT",
        "competition": label_comp,
        "fiable": fiable,
        "event_id": ev_id,
        "match": f"{h_nom} vs {a_nom}",
        "date_match": _formater_date_locale(start_str),
        "detail": f"[{label_comp}] {h_nom} [{st_h['forme']}] vs {a_nom} [{st_a['forme']}] | Buts: {buts_label} ({buts_proba:.0f}%)",
        "choix": choix,
        "proba": proba_pct,
        "prudence": "ELEVE" if proba_pct >= 72 else ("MODERE" if proba_pct >= 56 else "PRUDENT"),
        "mise_u": 0.0,
        "equipes": [h_nom, a_nom],
    }, {}


def analyser_foot_competitions(competitions, heures_max=48):
    resultats_a_venir = []
    alertes_globales = {}
    stats_par_comp = {}
    apercu_lignes = []

    aujourdhui = datetime.now(timezone.utc).date()

    taches_sb = []
    for comp in competitions:
        for delta in range(2):
            jour = (aujourdhui + timedelta(days=delta)).strftime("%Y%m%d")
            taches_sb.append((comp["slug"], jour))

    def charger_sb(args):
        slug, jour = args
        try:
            return _extraire_events(fb.get_fixtures_by_date(slug, jour))
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=8) as ex:
        res_sb = list(ex.map(charger_sb, taches_sb))

    events_upcoming_par_slug = {c["slug"]: [] for c in competitions}
    for i, (slug, _) in enumerate(taches_sb):
        events_upcoming_par_slug[slug].extend(res_sb[i])

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
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=10) as ex:
        res_hist = list(ex.map(charger_hist, taches_hist))

    events_hist_par_slug = {c["slug"]: [] for c in competitions}
    for i, (_, slug) in enumerate(taches_hist):
        events_hist_par_slug[slug].extend(res_hist[i])

    def analyser_comp(comp):
        slug = comp["slug"]
        label_comp = comp["label"]

        raw_comp_events = events_hist_par_slug[slug] + events_upcoming_par_slug[slug]
        dedup_comp = {}
        for idx, ev in enumerate(raw_comp_events):
            eid = str(ev.get("id") or f"{slug}_{idx}")
            if eid not in dedup_comp:
                dedup_comp[eid] = ev

        events_comp = list(dedup_comp.values())
        a_venir_48h = [ev for ev in events_comp if _est_match_a_venir_dans_fenetre(ev, heures_max)]
        a_venir_48h.sort(key=lambda e: str(e.get("start_time") or e.get("date") or ""))

        nb_clos = sum(1 for ev in events_comp if str(ev.get("status") or "").strip().lower() in STATUTS_TERMINES)

        res_comp = []
        alertes_comp = {}
        apercu_comp = []

        for ev in a_venir_48h:
            entree, _ = _construire_entree_foot(ev, label_comp, events_comp)
            res_comp.append(entree)

            if len(apercu_comp) < 2:
                h, a = _normaliser_home_away(ev)
                apercu_comp.append(
                    f"{slug} | {_formater_date_locale(ev.get('start_time') or ev.get('date'))} | "
                    f"{h.get('short_name') or h.get('name') or '?'} vs {a.get('short_name') or a.get('name') or '?'}"
                )

        return label_comp, res_comp, alertes_comp, nb_clos, len(events_comp), apercu_comp

    with ThreadPoolExecutor(max_workers=6) as ex:
        res_comps = list(ex.map(analyser_comp, competitions))

    for label_comp, res_comp, alertes_comp, nb_clos, total_events, apercu_comp in res_comps:
        resultats_a_venir.extend(res_comp)
        alertes_globales.update(alertes_comp)
        apercu_lignes.extend(apercu_comp)
        stats_par_comp[label_comp] = f"À venir: {len(res_comp)} | Terminés: {nb_clos} (bruts: {total_events})"

    print("Couverture Foot :", " • ".join(f"{k}: {v}" for k, v in stats_par_comp.items()), flush=True)
    return resultats_a_venir, alertes_globales, stats_par_comp, "\n".join(apercu_lignes[:12])


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
    except Exception:
        return [], {}

    simples = []
    alertes_tennis = {}

    for t in sb.get("data", {}).get("tournaments", []):
        t_name = t.get("name")
        for d in t.get("draws", []):
            if "Doubles" in d.get("name", ""):
                continue
            rk_map = atp_rk if "Men" in d.get("name", "") else wta_rk
            for m in d.get("matches", []):
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
                diff = (math.log((r2 or 150) + 1) - math.log((r1 or 150) + 1)) * (0.85 if complet else 0.35)
                p1 = 1 / (1 + math.exp(-diff))
                favori, prob = (j1, p1) if p1 >= 0.5 else (j2, 1 - p1)
                if complet:
                    prob = min(max(prob, 0.50), 0.82)

                proba_pct = round(max(0.05, prob) * 100, 1)
                simples.append({
                    "sport": "TENNIS",
                    "competition": t_name or "Tennis",
                    "fiable": complet,
                    "event_id": str(m.get("id", "")),
                    "match": f"{j1} (#{r1 or 'NC'}) vs {j2} (#{r2 or 'NC'})",
                    "date_match": _formater_date_locale(str(m.get("date", ""))),
                    "detail": f"{t_name}",
                    "choix": f"Victoire {favori}",
                    "proba": proba_pct,
                    "prudence": "MODERE" if proba_pct >= 56 else "PRUDENT",
                    "mise_u": 0.0,
                    "equipes": [favori],
                })
    return simples, alertes_tennis


def _enrichir_sante_top(top_items):
    if not top_items:
        return {}

    equipes_a_scanner = set()
    for it in top_items[:8]:
        for nom in it.get("equipes", []):
            if nom:
                equipes_a_scanner.add(nom)

    alertes_globales = {}
    penalites = {}

    def scanner(nom):
        try:
            return nom, scanner_sante_cible(nom, 1)
        except Exception:
            return nom, []

    with ThreadPoolExecutor(max_workers=6) as ex:
        for nom, titres in ex.map(scanner, equipes_a_scanner):
            if titres:
                alertes_globales[nom] = titres
                penalites[nom] = len(titres)

    for it in top_items:
        nb = sum(penalites.get(n, 0) for n in it.get("equipes", []))
        if nb:
            it["proba"] = round(max(0.05, it["proba"] / 100 - min(nb * 0.02, 0.08)) * 100, 1)
            it["prudence"] = (
                "ELEVE" if it["proba"] >= 72
                else ("MODERE" if it["proba"] >= 56 else "PRUDENT (alerte)")
            )

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


def mettre_a_jour_historique(items):
    champs = ["date_analyse", "sport", "event_id", "match", "date_match", "choix", "proba", "prudence", "mise_u", "statut"]
    lignes = []
    cles = set()
    if os.path.exists(FICHIER_HISTORIQUE):
        try:
            with open(FICHIER_HISTORIQUE, "r", newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if any((v or "").strip() for v in row.values()):
                        lignes.append(row)
                        cles.add(f"{row.get('sport')}_{row.get('event_id')}_{row.get('choix')}")
        except Exception:
            pass

    now_str = datetime.now(TZ_PARIS).strftime("%Y-%m-%d %H:%M")
    for p in items:
        cle = f"{p['sport']}_{p['event_id']}_{p['choix']}"
        if cle not in cles:
            lignes.append({
                "date_analyse": now_str,
                "sport": p["sport"],
                "event_id": p["event_id"],
                "match": p["match"],
                "date_match": p["date_match"],
                "choix": p["choix"],
                "proba": str(p["proba"]),
                "prudence": p["prudence"],
                "mise_u": str(p.get("mise_u", 0.0)),
                "statut": "EN_ATTENTE",
            })
            cles.add(cle)

    try:
        os.makedirs(os.path.dirname(FICHIER_HISTORIQUE) or ".", exist_ok=True)
        with open(FICHIER_HISTORIQUE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=champs)
            writer.writeheader()
            for r in lignes:
                writer.writerow({k: r.get(k, "") for k in champs})
    except Exception:
        pass

    return _calculer_bilan(lignes)


def executer_analyse():
    global _DERNIER_RAPPORT, _ANALYSE_EN_COURS
    with _STATE_LOCK:
        if _ANALYSE_EN_COURS:
            return _DERNIER_RAPPORT
        _ANALYSE_EN_COURS = True

    try:
        print(f"Analyse en cours (fenetre: {CFG.fenetre_heures}h)...", flush=True)

        try:
            foot_res, alertes_f, stats_comp, apercu_bruts = analyser_foot_competitions(
                COMPETITIONS_CIBLES, CFG.fenetre_heures
            )
        except Exception as e:
            print(f"[ERREUR] Foot: {e}", flush=True)
            foot_res, alertes_f, stats_comp, apercu_bruts = [], {}, {}, f"Erreur Foot: {e}"

        try:
            tennis_res, alertes_t = analyser_tennis(CFG.fenetre_heures)
        except Exception as e:
            print(f"[ERREUR] Tennis: {e}", flush=True)
            tennis_res, alertes_t = [], {}

        foot_sorted = sorted(foot_res, key=lambda x: x["proba"], reverse=True)
        tennis_sorted = sorted(tennis_res, key=lambda x: x["proba"], reverse=True)
        top_sur = sorted(foot_sorted + tennis_sorted, key=lambda x: x["proba"], reverse=True)[:10]

        try:
            bilan = mettre_a_jour_historique([x for x in top_sur if x.get("fiable", True)])
        except Exception as e:
            print(f"[ERREUR] Historique: {e}", flush=True)
            with _STATE_LOCK:
                bilan = dict(_DERNIER_RAPPORT.get("bilan") or {})

        alertes_combines = {**alertes_f, **alertes_t}
        try:
            alertes_sante = _enrichir_sante_top(top_sur)
            alertes_combines.update(alertes_sante)
            top_sur = sorted(top_sur, key=lambda x: x["proba"], reverse=True)
            foot_sorted = sorted(foot_sorted, key=lambda x: x["proba"], reverse=True)
        except Exception as e:
            print(f"[ERREUR] Santé: {e}", flush=True)

        synthese = "\n".join(
            f"• [{x['sport']} | {x['prudence']}] {x['match']} ({x['date_match']}) -> {x['choix']} ({x['proba']}%)"
            for x in top_sur[:8]
        ) or "Aucun match à venir dans les prochaines 48h."

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
        print("Analyse terminée et rapport mis à jour.", flush=True)
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
  <title>BetPulse Pro — Accueil</title>
  <style>
    :root { --bg: #0f172a; --card: #1e293b; --text: #e2e8f0; --muted: #94a3b8; --accent: #38bdf8; --green: #10b981; --border: #334155; }
    body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 24px; background: var(--bg); color: var(--text); }
    .container { max-width: 980px; margin: 0 auto; }
    .hero { background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%); border: 1px solid var(--border); border-radius: 14px; padding: 32px; margin-bottom: 24px; }
    .badge-status { display: inline-block; padding: 5px 12px; border-radius: 999px; font-size: 0.8em; font-weight: bold; background: rgba(56, 189, 248, 0.15); color: var(--accent); margin-bottom: 12px; }
    .badge-ready { background: rgba(16, 185, 129, 0.15); color: var(--green); }
    h1 { margin: 0 0 10px 0; color: var(--accent); font-size: 2rem; }
    p { color: var(--muted); line-height: 1.6; margin: 0 0 20px 0; }
    .actions { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }
    .btn { background: var(--accent); color: #0f172a; border: none; padding: 11px 18px; border-radius: 8px; cursor: pointer; font-weight: bold; text-decoration: none; display: inline-block; font-size: 0.95rem; }
    .btn-secondary { background: var(--card); color: var(--text); border: 1px solid var(--border); }
    .btn:hover { opacity: 0.9; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; }
    .box { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px; }
    .box h3 { margin: 0 0 8px 0; font-size: 1rem; color: var(--text); }
    .box .val { font-size: 1.4rem; font-weight: bold; color: var(--accent); margin-bottom: 6px; }
    .box .sub { font-size: 0.85rem; color: var(--muted); }
  </style>
</head>
<body>
  <div class="container">
    <div class="hero">
      <span class="badge-status {% if r.mis_a_jour %}badge-ready{% endif %}">
        {% if en_cours %}Analyse en cours en arrière-plan{% elif r.mis_a_jour %}Données prêtes{% else %}Initialisation{% endif %}
      </span>
      <h1>BetPulse Pro</h1>
      <p>
        Suivi des rencontres Football et Tennis sur une fenêtre de {{ fenetre_heures }}h, estimation probabiliste par modèle de Poisson et classement ATP/WTA, et contrôle des alertes blessures récentes.
      </p>
      <div class="actions">
        <a href="/app" class="btn">Accéder aux matchs &amp; pronostics</a>
        <a href="/bilan" class="btn btn-secondary">Voir le bilan &amp; la synthèse</a>
        <form method="post" action="/refresh" style="margin: 0;">
          <button type="submit" class="btn btn-secondary">Lancer une actualisation</button>
        </form>
      </div>
    </div>

    <div class="grid">
      <div class="box">
        <h3>Dernière mise à jour</h3>
        <div class="val">{{ r.mis_a_jour or "En attente" }}</div>
        <div class="sub">Cache actif : {{ cache_ttl }}s</div>
      </div>
      <div class="box">
        <h3>Matchs à venir ({{ fenetre_heures }}h)</h3>
        <div class="val">{{ (r.foot|length) + (r.tennis|length) }}</div>
        <div class="sub">{{ r.foot|length }} Football · {{ r.tennis|length }} Tennis</div>
      </div>
      <div class="box">
        <h3>Historique vérifié</h3>
        <div class="val">{{ r.get("bilan", {}).get("taux", 0.0) }}%</div>
        <div class="sub">{{ r.get("bilan", {}).get("gagnes", 0) }} / {{ r.get("bilan", {}).get("verifies", 0) }} validés</div>
      </div>
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
  <title>BetPulse Pro — Préparation des données…</title>
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
  <p id="status-msg">Récupération des matchs et calcul des probabilités en cours…</p>
  <a href="/">← Retour à l'accueil</a>
  <script>
    async function verifierEtat() {
      try {
        const res = await fetch("/api/status", { cache: "no-store" });
        if (res.ok) {
          const data = await res.json();
          if (data.ready) {
            window.location.replace("/app");
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
  <title>BetPulse Pro — Matchs à venir (48h)</title>
  <style>
    :root { --bg: #0f172a; --card: #1e293b; --text: #e2e8f0; --accent: #38bdf8; --green: #10b981; --orange: #f59e0b; --red: #ef4444; }
    body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 20px; background: var(--bg); color: var(--text); }
    h1, h2 { color: var(--accent); margin-bottom: 15px; }
    .container { max-width: 1200px; margin: 0 auto; }
    .header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; padding-bottom: 15px; border-bottom: 1px solid #334155; }
    .header .meta { color: #94a3b8; font-size: 0.9em; }
    button, .btn { background: var(--accent); color: var(--bg); border: none; padding: 8px 14px; border-radius: 6px; cursor: pointer; font-weight: bold; text-decoration: none; display: inline-block; font-size: 0.9em; }
    .btn-ghost { background: #1e293b; color: var(--text); border: 1px solid #334155; }
    button:hover, .btn:hover { opacity: 0.88; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 15px; }
    .card { background: var(--card); padding: 15px; border-radius: 8px; border-left: 4px solid var(--accent); box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
    .card.ELEVE { border-left-color: var(--green); }
    .card.MODERE { border-left-color: var(--orange); }
    .card.PRUDENT { border-left-color: var(--red); }
    .card-title { font-size: 1.1em; font-weight: bold; margin-bottom: 8px; display: flex; justify-content: space-between; gap: 8px; }
    .badge { font-size: 0.75em; padding: 3px 8px; border-radius: 12px; font-weight: bold; white-space: nowrap; }
    .badge.ELEVE { background: var(--green); color: #000; }
    .badge.MODERE { background: var(--orange); color: #000; }
    .badge.PRUDENT { background: var(--red); color: #fff; }
    .card-meta { font-size: 0.85em; color: #94a3b8; margin-bottom: 8px; line-height: 1.4; }
    .card-detail { font-size: 0.8em; color: #cbd5e1; background: #0f172a; padding: 8px; border-radius: 4px; margin-top: 8px; font-family: monospace; }
    details { background: var(--card); border-radius: 8px; margin-bottom: 15px; overflow: hidden; }
    summary { padding: 15px; cursor: pointer; font-weight: bold; color: var(--accent); user-select: none; }
    summary:hover { background: #334155; }
    .details-content { padding: 0 15px 15px 15px; }
    pre { white-space: pre-wrap; background: #0f172a; padding: 15px; border-radius: 6px; overflow-x: auto; font-size: 0.85em; }
    .stats-bar { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 20px; }
    .stat-item { background: var(--card); padding: 8px 12px; border-radius: 20px; font-size: 0.85em; }
    .notice { background: rgba(56, 189, 248, 0.12); border: 1px solid rgba(56, 189, 248, 0.35); color: #bae6fd; padding: 10px 14px; border-radius: 8px; margin-bottom: 18px; font-size: 0.9em; }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div>
        <h1 style="margin: 0;">Matchs &amp; Pronostics</h1>
        <span class="meta">Dernière mise à jour : {{ r.mis_a_jour or "En attente" }}</span>
      </div>
      <div style="display: flex; flex-wrap: wrap; gap: 8px;">
        <a href="/" class="btn btn-ghost">Accueil</a>
        <a href="/bilan" class="btn btn-ghost">Bilan</a>
        <form method="post" action="/refresh" style="margin: 0;">
          <button type="submit">Actualiser</button>
        </form>
        <a href="/api/analyse" class="btn">JSON</a>
        <a href="/api/debug-foot" class="btn">Debug</a>
      </div>
    </div>

    {% if en_cours %}
    <div class="notice">Mise à jour en arrière-plan en cours — les données affichées restent accessibles.</div>
    {% endif %}

    <h2>Bilan historique</h2>
    <div class="stats-bar">
      <span class="stat-item">Total : <strong>{{ r.get("bilan", {}).get("total", 0) }}</strong></span>
      <span class="stat-item">En attente : <strong>{{ r.get("bilan", {}).get("en_attente", 0) }}</strong></span>
      <span class="stat-item">Vérifiés : <strong>{{ r.get("bilan", {}).get("verifies", 0) }}</strong></span>
      <span class="stat-item">Réussite : <strong>{{ r.get("bilan", {}).get("taux", 0.0) }}%</strong></span>
    </div>

    <h2>Couverture Foot</h2>
    <div class="stats-bar">
      {% for k, v in r.get("stats_comp", {}).items() %}
        <span class="stat-item"><strong>{{ k }}</strong> : {{ v }}</span>
      {% endfor %}
    </div>

    <h2>Top pronostics (Foot &amp; Tennis)</h2>
    <div class="grid">
      {% for it in r.top_sur %}
      <div class="card {{ (it.prudence or 'PRUDENT').split(' ')[0] }}">
        <div class="card-title">
          <span>{{ it.choix }}</span>
          <span class="badge {{ (it.prudence or 'PRUDENT').split(' ')[0] }}">{{ it.proba }}%</span>
        </div>
        <div class="card-meta">
          <strong>{{ it.match }}</strong><br>
          {{ it.date_match }} — {{ it.competition }}
        </div>
        {% if it.detail %}
        <div class="card-detail">{{ it.detail }}</div>
        {% endif %}
      </div>
      {% else %}
      <div class="card">Aucun pronostic disponible.</div>
      {% endfor %}
    </div>

    <h2>Matchs de Foot à venir — 48h ({{ r.foot|length }})</h2>
    <div class="grid">
      {% for it in r.foot %}
      <div class="card {{ (it.prudence or 'PRUDENT').split(' ')[0] }}">
        <div class="card-title">
          <span>{{ it.choix }}</span>
          <span class="badge {{ (it.prudence or 'PRUDENT').split(' ')[0] }}">{{ it.proba }}%</span>
        </div>
        <div class="card-meta">
          <strong>{{ it.match }}</strong><br>
          {{ it.date_match }}
        </div>
        <div class="card-detail">{{ it.detail }}</div>
      </div>
      {% else %}
      <div class="card">Aucun match de foot à venir dans les prochaines 48h.</div>
      {% endfor %}
    </div>

    <h2>Matchs de Tennis à venir — 48h ({{ r.tennis|length }})</h2>
    <div class="grid">
      {% for it in r.tennis %}
      <div class="card {{ (it.prudence or 'PRUDENT').split(' ')[0] }}">
        <div class="card-title">
          <span>{{ it.choix }}</span>
          <span class="badge {{ (it.prudence or 'PRUDENT').split(' ')[0] }}">{{ it.proba }}%</span>
        </div>
        <div class="card-meta">
          <strong>{{ it.match }}</strong><br>
          {{ it.date_match }} — {{ it.competition }}
        </div>
      </div>
      {% else %}
      <div class="card">Aucun match de tennis à venir dans les prochaines 48h.</div>
      {% endfor %}
    </div>

    <details>
      <summary>🚑 Alertes blessures / forfaits (3j)</summary>
      <div class="details-content">
        {% for cible, titres in r.get("alertes", {}).items() %}
          <p style="margin-bottom: 10px;">
            <strong style="color: #fca5a5;">{{ cible }}</strong> :<br>
            <span style="font-size: 0.9em; color: #cbd5e1;">{{ titres | join(" | ") }}</span>
          </p>
        {% else %}
          <span class="meta">Aucune alerte récente.</span>
        {% endfor %}
      </div>
    </details>

    <details>
      <summary>🔍 Aperçu données brutes Foot</summary>
      <div class="details-content">
        <pre>{{ r.apercu_bruts or "Aucun événement brut." }}</pre>
      </div>
    </details>

    <details>
      <summary>📋 Synthèse Textuelle</summary>
      <div class="details-content">
        <pre>{{ r.synthese }}</pre>
      </div>
    </details>
  </div>
</body>
</html>
"""

_HTML_BILAN = """<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BetPulse Pro — Bilan &amp; Synthèse</title>
  <style>
    body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 24px; background: #0f172a; color: #e2e8f0; }
    .container { max-width: 960px; margin: 0 auto; }
    h1, h2 { color: #38bdf8; }
    .top-bar { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; border-bottom: 1px solid #334155; padding-bottom: 14px; }
    .btn { background: #38bdf8; color: #0f172a; padding: 8px 14px; border-radius: 6px; text-decoration: none; font-weight: bold; font-size: 0.9rem; }
    .btn-ghost { background: #1e293b; color: #e2e8f0; border: 1px solid #334155; }
    .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 20px; }
    .card { background: #1e293b; padding: 16px; border-radius: 8px; border: 1px solid #334155; }
    .num { font-size: 1.4rem; font-weight: bold; color: #38bdf8; margin-top: 6px; }
    pre { white-space: pre-wrap; background: #1e293b; padding: 16px; border-radius: 8px; border: 1px solid #334155; line-height: 1.5; }
  </style>
</head>
<body>
  <div class="container">
    <div class="top-bar">
      <h1 style="margin: 0;">Bilan &amp; Synthèse</h1>
      <div style="display: flex; gap: 8px;">
        <a href="/" class="btn btn-ghost">Accueil</a>
        <a href="/app" class="btn">Matchs &amp; Pronostics</a>
      </div>
    </div>

    <div class="stats">
      <div class="card"><div>Total enregistrés</div><div class="num">{{ r.get("bilan", {}).get("total", 0) }}</div></div>
      <div class="card"><div>En attente</div><div class="num">{{ r.get("bilan", {}).get("en_attente", 0) }}</div></div>
      <div class="card"><div>Vérifiés</div><div class="num">{{ r.get("bilan", {}).get("verifies", 0) }}</div></div>
      <div class="card"><div>Taux réussite</div><div class="num">{{ r.get("bilan", {}).get("taux", 0.0) }}%</div></div>
    </div>

    <h2>Synthèse textuelle</h2>
    <pre>{{ r.synthese or "Aucune synthèse disponible pour le moment." }}</pre>
  </div>
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
            print(f"[ERREUR FATALE] analyse background: {e}", flush=True)

    threading.Thread(target=_runner, daemon=True).start()


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


@app.route("/api/debug-foot")
def api_debug_foot():
    fonctions = [n for n in dir(fb) if not n.startswith("_") and callable(getattr(fb, n, None))]
    try:
        source_sched = inspect.getsource(getattr(fb, "get_team_schedule"))
    except Exception as e:
        source_sched = f"Non disponible: {e}"

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
        "source_get_team_schedule": source_sched,
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
        fenetre_heures=CFG.fenetre_heures,
        cache_ttl=CFG.cache_ttl_sec,
    ), 200)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/app")
def app_view():
    with _STATE_LOCK:
        rapport = dict(_DERNIER_RAPPORT)
        en_cours = _ANALYSE_EN_COURS

    if not rapport.get("mis_a_jour"):
        if not en_cours:
            _demarrer_analyse_si_besoin(0.0)
        resp = make_response(render_template_string(_HTML_LOADING), 200)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp

    resp = make_response(render_template_string(_HTML_PAGE, r=rapport, en_cours=en_cours), 200)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/bilan")
def bilan_view():
    with _STATE_LOCK:
        rapport = dict(_DERNIER_RAPPORT)
        en_cours = _ANALYSE_EN_COURS

    if not rapport.get("mis_a_jour") and not en_cours:
        _demarrer_analyse_si_besoin(0.0)

    resp = make_response(render_template_string(_HTML_BILAN, r=rapport), 200)
    resp.headers["Cache-Control"] = "no-store"
    return resp


_charger_rapport_disque()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
