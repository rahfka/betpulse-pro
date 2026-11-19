import csv
import importlib
import inspect
import math
import os
import re
import sys
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, redirect, render_template_string, url_for

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
    n_derniers_matchs: int = int(os.getenv("N_DERNIERS_MATCHS", "10"))
    fenetre_heures: int = int(os.getenv("FENETRE_HEURES", "48"))
    timeout_http: int = int(os.getenv("TIMEOUT_HTTP", "8"))
    cache_ttl_sec: int = int(os.getenv("CACHE_TTL_SEC", "300"))


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
EQUIPES_FOOT_IDS = ["160", "174", "176", "166"]

COMPETITIONS_CIBLES = [
    {"slug": "ligue-1", "label": "Ligue 1", "mots": ("ligue 1", "ligue-1", "fra.1", "france"), "equipes": ["160", "174", "176", "166"]},
    {"slug": "la-liga", "label": "Liga Espagnole", "mots": ("la liga", "laliga", "la-liga", "esp.1", "spain"), "equipes": ["86", "83", "81", "89"]},
    {"slug": "bundesliga", "label": "Bundesliga", "mots": ("bundesliga", "ger.1", "germany"), "equipes": ["132", "124", "167", "129"]},
    {"slug": "portuguese-liga", "label": "Liga Portugaise", "mots": ("liga portugal", "primeira liga", "portuguese-liga", "por.1", "portugal"), "equipes": ["1929", "1927", "1930", "1936"]},
    {"slug": "premier-league", "label": "Premier League", "mots": ("premier league", "premier-league", "eng.1", "england"), "equipes": ["364", "359", "360", "382"]},
    {"slug": "serie-a", "label": "Serie A", "mots": ("serie a", "serie-a", "ita.1", "italy"), "equipes": ["111", "110", "103", "114"]},
    {"slug": "uefa-champions-league", "label": "Ligue des Champions", "mots": ("champions league", "champions-league", "uefa.champions", "ucl"), "equipes": ["160", "86", "83", "364", "132", "110"]},
    {"slug": "uefa-europa-league", "label": "Europa League", "mots": ("europa league", "europa-league", "uefa.europa", "uel"), "equipes": ["176", "174", "360", "111", "1929", "129"]},
]

STATUTS_TERMINES = {"closed", "final", "post", "completed", "finished", "ft", "aet", "pen", "status_final", "status_post"}
STATUTS_TERMINES_OU_EN_COURS = STATUTS_TERMINES | {"in_progress", "live"}

_CACHE_FOOT_SCHED = {}
_CACHE_SANTE = {}
_ANALYSE_LOCK = threading.Lock()
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

_HTTP_SESSION = requests.Session()
_HTTP_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
    )
})


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


def stats_foot_detail(team_id, team_name="", events_override=None, n_derniers=10):
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
        r = _HTTP_SESSION.get(url, timeout=CFG.timeout_http)
        if r.status_code == 200:
            root = ET.fromstring(r.text)
            for item in root.findall(".//item")[:5]:
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

    res = _requete_rss(f'"{cible}" (blessure OR forfait OR incertain) when:3d', max_titres)
    if not res:
        res = _requete_rss(f'{cible} (blessure OR forfait OR cuisse OR genou OR cheville OR retrait) when:5d', max_titres)

    _cache_set(_CACHE_SANTE, cle, res)
    return list(res)


def _construire_entree_foot(ev, label_comp, events_comp):
    ev_id = str(ev.get("id") or "")
    start_str = str(ev.get("start_time") or ev.get("date") or "")
    statut_raw = str(ev.get("status") or "")
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

    titres_sante = scanner_sante_cible(h_nom, 1) + scanner_sante_cible(a_nom, 1)
    titres_sante = list(dict.fromkeys(titres_sante))[:2]
    proba_pct = round(max(0.05, prob - min(len(titres_sante) * 0.02, 0.08)) * 100, 1)

    buts_label = "+2.5 buts" if p_ov25 >= p_un25 else "-2.5 buts"
    buts_proba = round(max(p_ov25, p_un25) * 100, 1)

    return {
        "sport": "FOOT",
        "competition": label_comp,
        "fiable": fiable,
        "event_id": ev_id,
        "match": f"{h_nom} vs {a_nom}",
        "date_match": _formater_date_locale(start_str),
        "detail": (
            f"[{label_comp} | {statut_raw}] {h_nom} [{st_h['forme']}] vs {a_nom} [{st_a['forme']}] "
            f"| Buts: {buts_label} ({buts_proba:.0f}%)"
        ),
        "choix": choix,
        "proba": proba_pct,
        "prudence": "ELEVE" if proba_pct >= 72 else ("MODERE" if proba_pct >= 56 else "PRUDENT"),
        "mise_u": 0.0,
    }, titres_sante


def analyser_foot_competitions(competitions, equipes_ids=None, heures_max=48):
    resultats_a_venir = []
    alertes_globales = {}
    stats_par_comp = {}
    apercu_lignes = []
    vus_globaux = set()

    for comp in competitions:
        slug = comp["slug"]
        label_comp = comp["label"]
        ids_equipes = list(dict.fromkeys((comp.get("equipes") or []) + (equipes_ids or [])))

        raw_comp_events = []
        for tid in ids_equipes:
            sched = get_foot_schedule_cached(tid, slug, CFG.saison_foot)
            evs = _extraire_events(sched)
            raw_comp_events.extend(evs)
            if len(apercu_lignes) < 12 and evs:
                ev0 = evs[0]
                hi, ai = _normaliser_home_away(ev0)
                apercu_lignes.append(
                    f"{slug} team={tid} | {ev0.get('start_time') or ev0.get('date')} | "
                    f"{hi.get('short_name') or hi.get('name') or '?'} vs {ai.get('short_name') or ai.get('name') or '?'}"
                )

        dedup_comp = {}
        for idx, ev in enumerate(raw_comp_events):
            comp_obj = ev.get("competition") or ev.get("league") or ev.get("tournament") or {}
            comp_txt = (
                " ".join(str(comp_obj.get(k) or "").lower() for k in ("id", "slug", "name", "short_name"))
                if isinstance(comp_obj, dict)
                else str(comp_obj).lower()
            )
            if comp_txt.strip() and not any(m in comp_txt for m in comp["mots"]):
                continue
            eid = str(ev.get("id") or f"{slug}_{idx}")
            if eid not in dedup_comp:
                dedup_comp[eid] = ev

        events_comp = list(dedup_comp.values())
        a_venir_48h = [ev for ev in events_comp if _est_match_a_venir_dans_fenetre(ev, heures_max)]
        a_venir_48h.sort(key=lambda e: str(e.get("start_time") or e.get("date") or ""))

        nb_clos = sum(
            1 for ev in events_comp
            if str(ev.get("status") or "").strip().lower() in STATUTS_TERMINES
        )

        ajoutes_comp = 0
        for ev in a_venir_48h:
            cle_globale = str(ev.get("id") or f"{ev.get('start_time')}_{ajoutes_comp}")
            if cle_globale in vus_globaux:
                continue
            vus_globaux.add(cle_globale)
            entree, titres_s = _construire_entree_foot(ev, label_comp, events_comp)
            resultats_a_venir.append(entree)
            ajoutes_comp += 1
            if titres_s:
                alertes_globales[entree["match"]] = titres_s

        stats_par_comp[label_comp] = f"48h:{ajoutes_comp} | clos:{nb_clos} (bruts:{len(events_comp)})"

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
    atp_rk = {
        " ".join(p["name"].lower().split()): p["rank"]
        for p in tn.get_rankings(tour="atp").get("data", {}).get("rankings", [])
    }
    wta_rk = {
        " ".join(p["name"].lower().split()): p["rank"]
        for p in tn.get_rankings(tour="wta").get("data", {}).get("rankings", [])
    }
    sb = tn.get_scoreboard()
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
                sante = scanner_sante_cible(favori, 1) if complet else []
                if sante:
                    alertes_tennis[favori] = sante
                proba_pct = round(max(0.05, prob - len(sante) * 0.03) * 100, 1)
                simples.append({
                    "sport": "TENNIS",
                    "competition": t_name or "Tennis",
                    "fiable": complet,
                    "event_id": str(m.get("id", "")),
                    "match": f"{j1} (#{r1 or 'NC'}) vs {j2} (#{r2 or 'NC'})",
                    "date_match": _formater_date_locale(str(m.get("date", ""))),
                    "detail": f"{t_name} | Alertes 3j: {len(sante)}",
                    "choix": f"Victoire {favori}",
                    "proba": proba_pct,
                    "prudence": "PRUDENT (alerte blessure)" if sante else ("MODERE" if proba_pct >= 56 else "PRUDENT"),
                    "mise_u": 0.0,
                })
    return simples, alertes_tennis


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
    global _DERNIER_RAPPORT
    with _ANALYSE_LOCK:
        print(f"Analyse en cours (fenetre: {CFG.fenetre_heures}h, matchs a venir uniquement)...", flush=True)
        foot_res, alertes_f, stats_comp, apercu_bruts = analyser_foot_competitions(
            COMPETITIONS_CIBLES, EQUIPES_FOOT_IDS, CFG.fenetre_heures
        )
        tennis_res, alertes_t = analyser_tennis(CFG.fenetre_heures)

        foot_sorted = sorted(foot_res, key=lambda x: x["proba"], reverse=True)
        tennis_sorted = sorted(tennis_res, key=lambda x: x["proba"], reverse=True)
        top_sur = sorted(foot_sorted + tennis_sorted, key=lambda x: x["proba"], reverse=True)[:10]
        alertes = {**alertes_f, **alertes_t}
        bilan = mettre_a_jour_historique([x for x in top_sur if x.get("fiable", True)])

        synthese = "\n".join(
            f"• [{x['sport']} | {x['prudence']}] {x['match']} ({x['date_match']}) -> {x['choix']} ({x['proba']}%)"
            for x in top_sur[:8]
        ) or "Aucun match à venir dans les prochaines 48h."

        _DERNIER_RAPPORT = {
            "mis_a_jour": datetime.now(TZ_PARIS).strftime("%Y-%m-%d %H:%M %Z"),
            "stats_comp": stats_comp,
            "apercu_bruts": apercu_bruts,
            "top_sur": top_sur,
            "foot": foot_sorted,
            "tennis": tennis_sorted,
            "alertes": alertes,
            "bilan": bilan,
            "synthese": synthese,
        }
        return _DERNIER_RAPPORT


app = Flask(__name__, static_folder="statique", static_url_path="/statique")

_HTML_PAGE = """<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BetPulse Pro — Matchs à venir (48h)</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 16px; background: #0f172a; color: #e2e8f0; }
    h1, h2 { color: #38bdf8; }
    .card { background: #1e293b; padding: 12px 14px; border-radius: 8px; margin-bottom: 10px; }
    .meta { color: #94a3b8; font-size: 0.9em; }
    a { color: #38bdf8; }
    button { background: #38bdf8; color: #0f172a; border: none; padding: 6px 12px; border-radius: 6px; cursor: pointer; font-weight: bold; }
    pre { white-space: pre-wrap; background: #1e293b; padding: 12px; border-radius: 8px; }
  </style>
</head>
<body>
  <h1>BetPulse Pro</h1>
  <form method="post" action="/refresh" style="display:inline;">
    <span class="meta">Dernière mise à jour : {{ r.mis_a_jour or "En attente" }} —</span>
    <button type="submit">Actualiser maintenant</button>
    <span class="meta">· <a href="/api/analyse">JSON</a> · <a href="/api/debug-foot">Debug Foot</a></span>
  </form>

  <h2>Bilan historique</h2>
  <div class="card">
    <strong>Total suivis</strong> : {{ r.get("bilan", {}).get("total", 0) }} ·
    <strong>En attente</strong> : {{ r.get("bilan", {}).get("en_attente", 0) }} ·
    <strong>Vérifiés</strong> : {{ r.get("bilan", {}).get("verifies", 0) }} ·
    <strong>Réussite</strong> : {{ r.get("bilan", {}).get("taux", 0.0) }}%
  </div>

  <h2>Couverture Foot</h2>
  <div class="card">
    {% for k, v in r.get("stats_comp", {}).items() %}
      <strong>{{ k }}</strong> : {{ v }}{% if not loop.last %} · {% endif %}
    {% endfor %}
  </div>

  <h2>Matchs de Foot à venir — 48h ({{ r.foot|length }})</h2>
  {% for it in r.foot %}
  <div class="card">
    <strong>{{ it.proba }}% [{{ it.prudence }}] -> {{ it.choix }}</strong><br>
    <span class="meta">{{ it.match }} ({{ it.date_match }}) — {{ it.detail }}</span>
  </div>
  {% else %}
  <div class="card">Aucun match de foot à venir dans les prochaines 48h.</div>
  {% endfor %}

  <h2>Matchs de Tennis à venir — 48h ({{ r.tennis|length }})</h2>
  {% for it in r.tennis %}
  <div class="card">
    <strong>{{ it.proba }}% [{{ it.prudence }}] -> {{ it.choix }}</strong><br>
    <span class="meta">{{ it.match }} ({{ it.date_match }}) — {{ it.detail }}</span>
  </div>
  {% else %}
  <div class="card">Aucun match de tennis à venir dans les prochaines 48h.</div>
  {% endfor %}

  <h2>Top pronostics à venir (Foot &amp; Tennis)</h2>
  {% for it in r.top_sur %}
  <div class="card">
    <strong>[{{ it.sport }}] {{ it.proba }}% [{{ it.prudence }}] -> {{ it.choix }}</strong><br>
    <span class="meta">{{ it.match }} ({{ it.date_match }})</span>
  </div>
  {% else %}
  <div class="card">Aucun pronostic disponible sur les prochaines 48h.</div>
  {% endfor %}

  <h2>Alertes blessures / forfaits (3j)</h2>
  <div class="card">
    {% for cible, titres in r.get("alertes", {}).items() %}
      <p><strong>{{ cible }}</strong> : {{ titres | join(" | ") }}</p>
    {% else %}
      <span class="meta">Aucune alerte récente.</span>
    {% endfor %}
  </div>

  <h2>Aperçu données brutes Foot</h2>
  <pre>{{ r.apercu_bruts or "Aucun événement brut." }}</pre>

  <h2>Synthèse</h2>
  <pre>{{ r.synthese }}</pre>
</body>
</html>
"""


@app.route("/health")
def health():
    return "ok", 200


@app.route("/api/analyse")
def api_analyse():
    if not _DERNIER_RAPPORT["mis_a_jour"]:
        executer_analyse()
    return jsonify(_DERNIER_RAPPORT)


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
    executer_analyse()
    return redirect(url_for("index"))


@app.route("/")
def index():
    if not _DERNIER_RAPPORT["mis_a_jour"]:
        executer_analyse()
    return render_template_string(_HTML_PAGE, r=_DERNIER_RAPPORT)


if __name__ == "__main__":
    executer_analyse()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
