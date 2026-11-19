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
# CONFIGURATION GENERALE
# =====================================================================
_DOSSIER_SCRIPT = os.path.dirname(os.path.abspath(__file__))
FICHIER_HISTORIQUE = os.path.join(_DOSSIER_SCRIPT, "historique_paris.csv")

OPENROUTER_API_KEY_LOCAL = ""
GEMINI_API_KEY_LOCAL = ""

COMPETITIONS_FOOT = [
    {"slug": "ligue-1", "label": "Ligue 1"},
    {"slug": "premier-league", "label": "Premier League"},
    {"slug": "la-liga", "label": "La Liga"},
    {"slug": "bundesliga", "label": "Bundesliga"},
    {"slug": "serie-a", "label": "Serie A"},
    {"slug": "portuguese-liga", "label": "Liga Portugal"},
    {"slug": "uefa-champions-league", "label": "Ligue des Champions"},
    {"slug": "uefa-europa-league", "label": "Ligue Europa"},
]

SAISON_FALLBACK_YEAR = "2026"
N_DERNIERS_MATCHS = 10
FENETRE_HEURES = 72
MAX_SCAN_SANTE_MATCHS = 25

FRACTION_KELLY = 0.25
MISE_MIN_UNITES = 0.25
MISE_MAX_UNITES = 2.0
SEUIL_EV_POSITIF_PCT = 3.0

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
    "synthese": "Clique sur « Actualiser » pour lancer l'analyse complète (fenêtre 72h).",
}


def _get_openrouter_key():
    return (os.getenv("OPENROUTER_API_KEY") or OPENROUTER_API_KEY_LOCAL or "").strip()


def _get_gemini_key():
    return (os.getenv("GEMINI_API_KEY") or GEMINI_API_KEY_LOCAL or "").strip()


def _appel_sûr(fn, candidats_kwargs):
    if not callable(fn):
        return None
    for kwargs in candidats_kwargs:
        try:
            res = fn(**kwargs)
            if res is not None:
                return res
        except TypeError:
            continue
        except Exception:
            continue
    return None


def _extraire_evenements(payload):
    if not payload:
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
                if isinstance(item, dict):
                    if item.get("competitors") or item.get("teams") or (item.get("home_team") and item.get("away_team")):
                        evenements.append(item)
                    else:
                        evenements.extend(_extraire_evenements(item))

    for cle_bloc in ("leagues", "competitions", "seasons", "days", "tournaments"):
        bloc = data.get(cle_bloc)
        if isinstance(bloc, list):
            for sub in bloc:
                if isinstance(sub, dict):
                    evenements.extend(_extraire_evenements(sub))

    return evenements


def recuperer_evenements_competition(slug):
    evenements = []

    if hasattr(fb, "get_season_schedule"):
        for sid in (f"{slug}-{SAISON_FALLBACK_YEAR}", f"{slug}-2026-2027", slug):
            res = _appel_sûr(
                fb.get_season_schedule,
                [
                    {"season_id": sid},
                    {"competition_id": slug, "season_id": sid},
                    {"league_slug": slug, "season_id": sid},
                ],
            )
            evs = _extraire_evenements(res)
            if evs:
                evenements.extend(evs)
                break

    if hasattr(fb, "get_daily_schedule"):
        now_utc = datetime.now(timezone.utc)
        for offset in range(-1, 4):
            jour = (now_utc + timedelta(days=offset)).strftime("%Y-%m-%d")
            res = _appel_sûr(fb.get_daily_schedule, [{"date": jour}, {"day": jour}])
            for ev in _extraire_evenements(res):
                texte_comp = str(
                    ev.get("league_slug")
                    or ev.get("competition_id")
                    or ev.get("competition")
                    or ev.get("league")
                    or ""
                ).lower()
                if slug in texte_comp or slug.replace("-", " ") in texte_comp or not texte_comp:
                    evenements.append(ev)

    dedup = {}
    for i, ev in enumerate(evenements):
        eid = str(ev.get("id") or ev.get("event_id") or f"{slug}_{i}")
        dedup[eid] = ev
    return list(dedup.values())


def _extraire_date_evenement(ev):
    for champ in ("start_time", "date", "commence_time", "startDate", "utc_date"):
        if ev.get(champ):
            return str(ev.get(champ))
    return ""


def _lire_statut(ev):
    st = ev.get("status")
    if isinstance(st, dict):
        st = st.get("type") or st.get("name") or st.get("state") or st.get("detail") or ""
    return str(st or "").strip().lower()


def _est_match_clos(ev):
    return _lire_statut(ev) in (
        "closed", "final", "post", "completed", "finished", "status_final", "full_time", "ft"
    )


def _est_match_a_venir(ev):
    st = _lire_statut(ev)
    if _est_match_clos(ev) or st in ("in_progress", "live", "halftime", "ht", "canceled", "cancelled", "postponed"):
        return False
    return True


def dans_fenetre_heures(date_str, heures_max):
    if not heures_max or not date_str:
        return True
    try:
        s = str(date_str).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            s = f"{s}T15:00:00+00:00"
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        maintenant = datetime.now(timezone.utc)
        return (maintenant - timedelta(hours=12)) <= dt <= (maintenant + timedelta(hours=heures_max))
    except Exception:
        return True


def _extraire_home_away(ev):
    competitors = ev.get("competitors")
    if isinstance(competitors, list) and len(competitors) >= 2:
        h_info = a_info = None
        for c in competitors:
            qual = str(c.get("qualifier") or c.get("homeAway") or "").strip().lower()
            t_obj = c.get("team") if isinstance(c.get("team"), dict) else c
            if qual == "home" and h_info is None:
                h_info = t_obj
            elif qual == "away" and a_info is None:
                a_info = t_obj
        if h_info is None:
            h_info = competitors[0].get("team") if isinstance(competitors[0].get("team"), dict) else competitors[0]
        if a_info is None:
            a_info = competitors[1].get("team") if isinstance(competitors[1].get("team"), dict) else competitors[1]
        return h_info or {}, a_info or {}

    if isinstance(ev.get("home_team"), dict) and isinstance(ev.get("away_team"), dict):
        return ev["home_team"], ev["away_team"]
    return {}, {}


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


def poisson_prob(k, lam):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (math.exp(-lam) * (lam ** k)) / math.factorial(k)


def moyenne_ponderee(valeurs, defaut):
    if not valeurs:
        return defaut
    poids = [1.0 + (i * 0.25) for i in range(len(valeurs))]
    return sum(v * p for v, p in zip(valeurs, poids)) / sum(poids)


def stats_foot_detail(team_id, events_comp, n_derniers=10):
    joues = [ev for ev in events_comp if _est_match_clos(ev)]
    joues.sort(key=lambda ev: _extraire_date_evenement(ev))

    bp_all, bc_all = [], []
    bp_home, bc_home = [], []
    bp_away, bc_away = [], []
    v = n = d = 0

    for ev in joues[-n_derniers:]:
        comps = ev.get("competitors", [])
        if len(comps) < 2:
            continue
        moi = adv = None
        mon_qual = None

        for c in comps:
            t_obj = c.get("team") if isinstance(c.get("team"), dict) else c
            tid = str(t_obj.get("id") or c.get("id") or "")
            sc = _extraire_score_numerique(c.get("score"))
            if sc is None:
                continue
            if team_id and tid == str(team_id):
                moi = sc
                mon_qual = str(c.get("qualifier") or c.get("homeAway") or "").lower()
            elif moi is None and adv is None:
                moi = sc
                mon_qual = str(c.get("qualifier") or c.get("homeAway") or "").lower()
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

    bp_base = moyenne_ponderee(bp_all, 1.30)
    bc_base = moyenne_ponderee(bc_all, 1.20)
    return {
        "forme": f"{v}V-{n}N-{d}D",
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
    kelly_plein = (b * prob - (1.0 - prob)) / b
    if kelly_plein <= 0:
        return 0.0
    mise = kelly_plein * FRACTION_KELLY * 10.0
    if alerte_grave:
        mise *= 0.5
    mise = min(mise, MISE_MAX_UNITES)
    return round(mise, 2) if mise >= MISE_MIN_UNITES else 0.0


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
    if proba_pct >= 56 and nb_alertes_sante <= 1:
        return "MODERE"
    return "PRUDENT"


def scanner_sante_cible(cible, max_titres=2):
    mots_alerte = (
        "bless", "forfait", "incertain", "infirmerie", "absent", "pépin", "pepin",
        "ischio", "genou", "cheville", "cuisse", "mollet", "suspend", "injury", "injured"
    )
    q = urllib.parse.quote(f'"{cible}" (blessure OR forfait OR incertain) when:3d')
    url = f"https://news.google.com/rss/search?q={q}&hl=fr&gl=FR&ceid=FR:fr"
    titres = []
    try:
        r = requests.get(url, timeout=6)
        if r.status_code == 200:
            root = ET.fromstring(r.text)
            for item in root.findall(".//item")[:6]:
                titre = (item.findtext("title") or "").strip()
                if any(m in titre.lower() for m in mots_alerte):
                    titres.append(titre)
    except Exception:
        pass
    return titres[:max_titres]


def alerte_grave_detectee(titres_sante):
    texte = " ".join(titres_sante).lower()
    return any(mg in texte for mg in ("forfait", "ischio", "abandon", "withdraw"))


def analyser_tous_matchs_foot(competitions, n_derniers=10, heures_max=72):
    vus_ids = set()
    matchs_candidats = []
    diag = []

    for comp in competitions:
        slug = comp["slug"]
        label_comp = comp.get("label", slug)
        evenements = recuperer_evenements_competition(slug)
        retenus = 0

        for ev in evenements:
            if not _est_match_a_venir(ev):
                continue
            start_str = _extraire_date_evenement(ev)
            if not dans_fenetre_heures(start_str, heures_max):
                continue
            h_info, a_info = _extraire_home_away(ev)
            if not h_info or not a_info:
                continue

            ev_id = str(ev.get("id") or ev.get("event_id") or f"{slug}_{_extraire_date_evenement(ev)}_{retenus}")
            cle = f"{slug}_{ev_id}"
            if cle in vus_ids:
                continue
            vus_ids.add(cle)
            retenus += 1
            matchs_candidats.append((slug, label_comp, evenements, ev_id, start_str, h_info, a_info))

        diag.append(f"{label_comp}: {retenus}/{len(evenements)}")

    matchs_candidats.sort(key=lambda x: x[4])

    resultats = []
    alertes_globales = {}
    scans_sante = 0

    for slug, label_comp, evenements_comp, ev_id, start_str, h_info, a_info in matchs_candidats:
        h_nom = h_info.get("short_name") or h_info.get("shortDisplayName") or h_info.get("name") or h_info.get("displayName") or "Home"
        a_nom = a_info.get("short_name") or a_info.get("shortDisplayName") or a_info.get("name") or a_info.get("displayName") or "Away"

        st_h = stats_foot_detail(h_info.get("id", ""), evenements_comp, n_derniers=n_derniers)
        st_a = stats_foot_detail(a_info.get("id", ""), evenements_comp, n_derniers=n_derniers)

        lam_h = max((st_h["bp_home"] + st_a["bc_away"]) / 2, 0.35)
        lam_a = max((st_a["bp_away"] + st_h["bc_home"]) / 2, 0.35)
        p_h, p_n, p_a, p_ov25, p_un25, p_btts, score_prob = simuler_poisson_foot(lam_h, lam_a)

        choix, prob = max(
            [(f"Victoire {h_nom}", p_h), ("Match nul", p_n), (f"Victoire {a_nom}", p_a)],
            key=lambda x: x[1],
        )

        titres_sante = []
        if scans_sante < MAX_SCAN_SANTE_MATCHS:
            scans_sante += 1
            titres_sante = (scanner_sante_cible(h_nom, 1) + scanner_sante_cible(a_nom, 1))[:2]

        grave = alerte_grave_detectee(titres_sante)
        prob_ajustee = max(0.05, prob - min(len(titres_sante) * 0.02 + (0.04 if grave else 0.0), 0.10))
        if titres_sante:
            alertes_globales[f"{h_nom} vs {a_nom}"] = titres_sante

        cote_txt = ""
        ev_val = None
        label_val = "Neutre"
        cote_dec = None
        mise_u = 0.0
        odds_obj = ev_info = None
        for item in evenements_comp:
            if str(item.get("id") or item.get("event_id") or "") == ev_id:
                odds_obj = item.get("odds")
                break
        if isinstance(odds_obj, dict) and odds_obj.get("details"):
            cote_txt, ev_val, label_val, cote_dec, mise_u = analyser_cote_us(
                odds_obj.get("details"), prob_ajustee, alerte_grave=grave
            )

        ov_pct = round(p_ov25 * 100, 1)
        un_pct = round(p_un25 * 100, 1)
        btts_pct = round(p_btts * 100, 1)
        buts_label = "+2.5 buts" if ov_pct >= un_pct else "-2.5 buts"
        buts_proba = max(ov_pct, un_pct)
        proba_pct = round(prob_ajustee * 100, 1)

        resultats.append({
            "sport": "FOOT",
            "competition": label_comp,
            "competition_slug": slug,
            "fiable": True,
            "event_id": ev_id,
            "match": f"{h_nom} vs {a_nom}",
            "date_match": start_str[:10] if start_str else "À venir",
            "detail": (
                f"[{label_comp}] {h_nom} [{st_h['forme']}] vs {a_nom} [{st_a['forme']}] | "
                f"xG: {lam_h:.2f}-{lam_a:.2f} (Score {score_prob}) | "
                f"Buts: {buts_label} ({buts_proba:.0f}%) | BTTS: {btts_pct:.0f}%{cote_txt}"
            ),
            "choix": choix,
            "proba": proba_pct,
            "prudence": niveau_prudence(proba_pct, len(titres_sante), label_val, alerte_grave=grave),
            "a_valeur": "+Valeur" in label_val,
            "ev_pct": ev_val,
            "cote_dec": cote_dec,
            "mise_u": mise_u,
            "buts_choix": buts_label,
            "buts_proba": buts_proba,
        })

    return resultats, alertes_globales, " · ".join(diag)


def trouver_rang(nom, rk_map):
    if not nom or not rk_map:
        return None
    nom_l = " ".join(nom.lower().split())
    if nom_l in rk_map:
        return rk_map[nom_l]
    for k, v in rk_map.items():
        if len(nom_l) >= 5 and (nom_l in k or k in nom_l):
            return v
    return None


def analyser_tennis(heures_max=72):
    atp_rk = {" ".join(p["name"].lower().split()): p["rank"] for p in tn.get_rankings(tour="atp").get("data", {}).get("rankings", [])}
    wta_rk = {" ".join(p["name"].lower().split()): p["rank"] for p in tn.get_rankings(tour="wta").get("data", {}).get("rankings", [])}
    sb = tn.get_scoreboard()

    simples_fiables, simples_nc, doubles_liste, alertes_tennis = [], [], [], {}
    for t in sb.get("data", {}).get("tournaments", []):
        t_name = t.get("name")
        for d in t.get("draws", []):
            is_doubles = "Doubles" in d.get("name", "")
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
                if "TBD" in (j1, j2):
                    continue
                if is_doubles:
                    doubles_liste.append(f"{j1} vs {j2} [{t_name}]")
                    continue

                rk_map = atp_rk if "Men" in d.get("name", "") else wta_rk
                r1, r2 = trouver_rang(j1, rk_map), trouver_rang(j2, rk_map)
                complet = r1 is not None and r2 is not None
                diff = (math.log((r2 or 150) + 1) - math.log((r1 or 150) + 1)) * (0.85 if complet else 0.35)
                p1 = 1 / (1 + math.exp(-diff))
                favori, prob = (j1, p1) if p1 >= 0.5 else (j2, 1 - p1)
                if complet:
                    prob = min(max(prob, 0.50), 0.82)

                titres = scanner_sante_cible(favori, 1) if complet else []
                if titres:
                    alertes_tennis[favori] = titres
                grave = alerte_grave_detectee(titres)
                proba_pct = round(max(0.05, prob - min(len(titres) * 0.03 + (0.06 if grave else 0.0), 0.12)) * 100, 1)

                entree = {
                    "sport": "TENNIS",
                    "competition": t_name or "Tennis",
                    "fiable": complet,
                    "event_id": str(m.get("id", "")),
                    "match": f"{j1} (#{r1 or 'NC'}) vs {j2} (#{r2 or 'NC'})",
                    "date_match": date_m[:10],
                    "detail": f"{t_name} | {date_m[:16]} UTC",
                    "choix": f"Victoire {favori}",
                    "proba": proba_pct,
                    "prudence": niveau_prudence(proba_pct, len(titres), alerte_grave=grave) if complet else "PRUDENT (NC)",
                    "a_valeur": False,
                    "ev_pct": None,
                    "cote_dec": None,
                    "mise_u": 0.0,
                }
                (simples_fiables if complet else simples_nc).append(entree)

    return simples_fiables, simples_nc, doubles_liste, alertes_tennis


def selectionner_meilleurs_paris_filtres(principaux, foot_res):
    filtres = []
    for it in principaux:
        pr = it.get("prudence", "")
        if "cote courte" in pr.lower() or "alerte blessure" in pr.lower():
            continue
        if it.get("a_valeur") and it.get("mise_u", 0.0) > 0:
            filtres.append({**it, "type": "VALEUR (EV+)"})
        elif it.get("cote_dec") is None and pr in ("ELEVE", "MODERE"):
            filtres.append({**it, "type": "WATCH-LIST (SANS COTE)"})

    for f in foot_res:
        if f.get("buts_proba", 0.0) >= 75.0:
            filtres.append({
                "type": "BUTS FORT",
                "sport": "FOOT",
                "match": f["match"],
                "date_match": f["date_match"],
                "choix": f["buts_choix"],
                "proba": f["buts_proba"],
                "prudence": "MODERE (buts)",
                "mise_u": 0.0,
            })

    filtres.sort(key=lambda x: (x["type"] != "VALEUR (EV+)", -x["proba"]))
    return filtres[:8]


def _calculer_pnl_ligne(row):
    if row.get("statut") not in ("GAGNE", "PERDU"):
        return None
    try:
        mise = float(row.get("mise_u") or 0.0)
        cote = float(row.get("cote_dec") or 0.0)
    except Exception:
        return 0.0
    if mise <= 0 or cote <= 1.0:
        return 0.0
    return round(mise * (cote - 1.0), 2) if row["statut"] == "GAGNE" else round(-mise, 2)


def mettre_a_jour_et_sauver_historique(pistes, foot_res):
    champs = [
        "date_analyse", "sport", "event_id", "match", "date_match",
        "choix", "proba", "prudence", "cote_dec", "mise_u", "pnl_u", "statut"
    ]
    lignes, cles_connues = [], set()

    if os.path.exists(FICHIER_HISTORIQUE):
        with open(FICHIER_HISTORIQUE, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                lignes.append(row)
                cles_connues.add(f"{row.get('sport')}_{row.get('event_id')}_{row.get('choix')}")

    for row in lignes:
        pnl = _calculer_pnl_ligne(row)
        row["pnl_u"] = "" if pnl is None else f"{pnl:+.2f}"

    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M")
    a_ajouter = list(pistes) + [
        {
            "sport": "FOOT",
            "event_id": f["event_id"],
            "match": f["match"],
            "date_match": f["date_match"],
            "choix": f["buts_choix"],
            "proba": f["buts_proba"],
            "prudence": "MODERE" if f["buts_proba"] >= 75 else "PRUDENT",
            "cote_dec": "",
            "mise_u": 0.0,
        }
        for f in foot_res
        if f.get("buts_choix") and f.get("buts_proba") is not None
    ]

    for p in a_ajouter:
        cle = f"{p['sport']}_{p['event_id']}_{p['choix']}"
        if cle not in cles_connues:
            lignes.append({
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
            cles_connues.add(cle)

    with open(FICHIER_HISTORIQUE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=champs)
        writer.writeheader()
        for r in lignes:
            writer.writerow({k: r.get(k, "") for k in champs})

    def resume(pred):
        sous = [r for r in lignes if pred(r) and r.get("statut") in ("GAGNE", "PERDU")]
        ok = [r for r in sous if r.get("statut") == "GAGNE"]
        return f"{len(ok)}/{len(sous)} ({(len(ok)/len(sous)*100) if sous else 0.0:.1f}%)"

    verifies = [r for r in lignes if r.get("statut") in ("GAGNE", "PERDU")]
    gagnes = [r for r in verifies if r.get("statut") == "GAGNE"]
    mises_engagees = sum(float(r.get("mise_u") or 0.0) for r in verifies if float(r.get("mise_u") or 0.0) > 0)
    pnl_total = sum(float(r.get("pnl_u") or 0.0) for r in verifies if float(r.get("mise_u") or 0.0) > 0)

    return {
        "total": len(lignes),
        "en_attente": sum(1 for r in lignes if r.get("statut") == "EN_ATTENTE"),
        "verifies": len(verifies),
        "gagnes": len(gagnes),
        "taux": round((len(gagnes) / len(verifies) * 100) if verifies else 0.0, 1),
        "mises_engagees": round(mises_engagees, 2),
        "pnl_total": round(pnl_total, 2),
        "roi_pct": round((pnl_total / mises_engagees * 100.0) if mises_engagees > 0 else 0.0, 1),
        "bilan_foot": resume(lambda r: r.get("sport") == "FOOT" and "2.5 buts" not in r.get("choix", "")),
        "bilan_buts": resume(lambda r: r.get("sport") == "FOOT" and "2.5 buts" in r.get("choix", "")),
        "bilan_tennis": resume(lambda r: r.get("sport") == "TENNIS"),
    }


def appeler_ia_gratuite(prompt_texte):
    or_key = _get_openrouter_key()
    if or_key:
        try:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {or_key}", "Content-Type": "application/json"},
                json={
                    "model": "openrouter/free",
                    "messages": [
                        {"role": "system", "content": "Analyste sportif concis en français. Aucune garantie de résultat."},
                        {"role": "user", "content": prompt_texte},
                    ],
                    "max_tokens": 500,
                },
                timeout=20,
            )
            if r.status_code == 200:
                txt = r.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                if txt:
                    return txt
        except Exception:
            pass

    gm_key = _get_gemini_key()
    if gm_key:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gm_key}"
            r = requests.post(
                url,
                headers={"Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": prompt_texte}]}], "generationConfig": {"maxOutputTokens": 500}},
                timeout=20,
            )
            if r.status_code == 200:
                parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
                txt = "".join(p.get("text", "") for p in parts).strip()
                if txt:
                    return txt
        except Exception:
            pass

    return None


def generer_synthese(pistes, alertes_sante, diag_foot=""):
    lignes_pistes = [f"- [{p['sport']}] {p['match']} -> {p['choix']} ({p['proba']}%, {p['prudence']})" for p in pistes[:8]]
    lignes_sante = [f"- {c} : {' // '.join(t)}" for c, t in alertes_sante.items()]
    prompt = (
        "Synthèse courte en français (forme, blessures, prudence) :\n"
        + "\n".join(lignes_pistes)
        + "\nAlertes :\n"
        + ("\n".join(lignes_sante) if lignes_sante else "Aucune")
    )
    rep = appeler_ia_gratuite(prompt)
    if rep:
        return rep

    lignes = ["Mode synthétique local."]
    if not any(p.get("sport") == "FOOT" for p in pistes) and diag_foot:
        lignes.append(f"• Diagnostic Foot : {diag_foot}")
    for p in pistes[:6]:
        lignes.append(f"• [{p['prudence']}] {p['match']} → {p['choix']} ({p['proba']}%)")
    return "\n".join(lignes)


def executer_analyse_complete(heures_max=FENETRE_HEURES):
    foot_res, alertes_f, diag_foot = analyser_tous_matchs_foot(
        COMPETITIONS_FOOT, n_derniers=N_DERNIERS_MATCHS, heures_max=heures_max
    )
    t_simples_ok, t_simples_nc, t_doubles, alertes_t = analyser_tennis(heures_max=heures_max)

    principaux = sorted(foot_res + t_simples_ok, key=lambda x: x["proba"], reverse=True)
    partiels = sorted(t_simples_nc, key=lambda x: x["proba"], reverse=True)
    filtres = selectionner_meilleurs_paris_filtres(principaux, foot_res)
    bilan = mettre_a_jour_et_sauver_historique(principaux, foot_res)
    synthese = generer_synthese(principaux, {**alertes_f, **alertes_t}, diag_foot=diag_foot)

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
  <title>BetPulse Pro</title>
  <style>
    :root {
      --bg: #0b0f17; --card: #131a26; --card-2: #192232; --border: #243044;
      --text: #eef2f7; --muted: #93a4bc; --accent: #22c55e; --warn: #f59e0b; --danger: #ef4444; --info: #38bdf8;
    }
    * { box-sizing: border-box; }
    body { margin: 0; padding: 0 0 40px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); }
    .topbar { position: sticky; top: 0; z-index: 10; backdrop-filter: blur(12px); background: rgba(11,15,23,0.9); border-bottom: 1px solid var(--border); padding: 14px 16px; display: flex; justify-content: space-between; align-items: center; }
    .brand-title { font-size: 1.05rem; font-weight: 700; }
    .brand-sub { font-size: 0.76rem; color: var(--muted); }
    .btn-refresh { background: var(--accent); color: #062111; border: none; border-radius: 10px; padding: 10px 14px; font-weight: 700; cursor: pointer; }
    .container { max-width: 780px; margin: 0 auto; padding: 16px; }
    .kpi-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-bottom: 16px; }
    @media (min-width: 640px) { .kpi-grid { grid-template-columns: repeat(4, 1fr); } }
    .kpi { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 12px; }
    .kpi-label { font-size: 0.72rem; color: var(--muted); text-transform: uppercase; }
    .kpi-val { font-size: 1.25rem; font-weight: 700; margin-top: 4px; }
    h2 { font-size: 0.92rem; text-transform: uppercase; color: var(--muted); margin: 22px 0 10px; }
    .card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 14px; margin-bottom: 10px; }
    .card-head { display: flex; justify-content: space-between; gap: 8px; flex-wrap: wrap; margin-bottom: 6px; }
    .badge { font-size: 0.72rem; font-weight: 700; padding: 3px 8px; border-radius: 999px; background: rgba(56,189,248,0.14); color: var(--info); }
    .choix { font-size: 1rem; font-weight: 700; margin: 4px 0; }
    .meta { font-size: 0.78rem; color: var(--muted); margin-top: 6px; }
    .synth { white-space: pre-line; font-size: 0.86rem; background: var(--card-2); border-radius: 12px; padding: 12px; }
  </style>
</head>
<body>
  <div class="topbar">
    <div>
      <div class="brand-title">BetPulse Pro</div>
      <div class="brand-sub">{% if data.maj %}MAJ : {{ data.maj }}{% else %}Prêt pour analyse (72h){% endif %}</div>
    </div>
    <form method="post" action="/refresh" style="margin:0;">
      <button type="submit" class="btn-refresh">Actualiser</button>
    </form>
  </div>

  <div class="container">
    <div class="kpi-grid">
      <div class="kpi"><div class="kpi-label">PnL Net</div><div class="kpi-val">{{ "%+.2f"|format(data.bilan.pnl_total) }}u</div></div>
      <div class="kpi"><div class="kpi-label">ROI</div><div class="kpi-val">{{ "%+.1f"|format(data.bilan.roi_pct) }}%</div></div>
      <div class="kpi"><div class="kpi-label">Réussite</div><div class="kpi-val">{{ data.bilan.taux }}%</div></div>
      <div class="kpi"><div class="kpi-label">Suivis</div><div class="kpi-val">{{ data.bilan.verifies }}/{{ data.bilan.total }}</div></div>
    </div>

    <h2>Sélection Filtrée</h2>
    {% for mf in data.filtres %}
    <div class="card">
      <div class="card-head"><span class="badge">{{ mf.type }} · {{ mf.sport }}</span><span class="badge">{{ mf.proba }}% · {{ mf.prudence }}</span></div>
      <div class="choix">{{ mf.choix }}{% if mf.mise_u and mf.mise_u > 0 %} — Mise {{ "%.2f"|format(mf.mise_u) }}u{% endif %}</div>
      <div class="meta">{{ mf.match }} ({{ mf.date_match }})</div>
    </div>
    {% else %}
    <div class="card"><div class="meta">Aucune piste filtrée pour l'instant.</div></div>
    {% endfor %}

    <h2>Toutes les Pistes</h2>
    {% for it in data.principaux %}
    <div class="card">
      <div class="card-head"><span class="badge">{{ it.sport }} · {{ it.competition }}</span><span class="badge">{{ it.proba }}% · {{ it.prudence }}</span></div>
      <div class="choix">{{ it.choix }}{% if it.mise_u and it.mise_u > 0 %} — Mise {{ "%.2f"|format(it.mise_u) }}u{% endif %}</div>
      <div class="meta">{{ it.match }} ({{ it.date_match }}) — {{ it.detail }}</div>
    </div>
    {% else %}
    <div class="card"><div class="meta">Aucun match remonté sur la fenêtre 72h.</div></div>
    {% endfor %}

    <h2>Synthèse Forme &amp; Santé</h2>
    <div class="card"><div class="synth">{{ data.synthese }}</div></div>
  </div>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def index():
    return render_template_string(HTML_TEMPLATE, data=_DERNIER_SNAPSHOT)


@app.route("/refresh", methods=["POST"])
def refresh():
    global _DERNIER_SNAPSHOT
    try:
        _DERNIER_SNAPSHOT = executer_analyse_complete(heures_max=FENETRE_HEURES)
    except Exception as exc:
        _DERNIER_SNAPSHOT["synthese"] = f"Erreur lors de l'analyse : {exc}"
    return render_template_string(HTML_TEMPLATE, data=_DERNIER_SNAPSHOT)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
