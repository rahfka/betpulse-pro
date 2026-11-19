import requests
from datetime import datetime, timedelta, timezone

_HEADERS = {"User-Agent": "Mozilla/5.0"}

_LEAGUE_MAP = {
    "ligue-1": "fra.1",
    "premier-league": "eng.1",
    "la-liga": "esp.1",
    "serie-a": "ita.1",
    "bundesliga": "ger.1",
    "portuguese-liga": "por.1",
    "uefa-champions-league": "uefa.champions",
    "uefa-europa-league": "uefa.europa",
}


def _map_status(state_str, completed):
    st = str(state_str or "").lower()
    if completed or st in ("post", "final", "closed"):
        return "closed"
    if st in ("pre", "scheduled", "not_started"):
        return "not_started"
    if st in ("in", "in_progress", "live"):
        return "in_progress"
    return st or "not_started"


def _parse_odds_details(comp):
    odds_list = comp.get("odds")
    if not isinstance(odds_list, list) or not odds_list:
        return None
    first = odds_list[0] if isinstance(odds_list[0], dict) else {}
    details = first.get("details")
    if details:
        return {"details": str(details)}
    return None


def _parse_event(ev):
    comps = ev.get("competitions", [])
    comp = comps[0] if comps else {}
    status_obj = comp.get("status") or ev.get("status") or {}
    type_obj = status_obj.get("type") if isinstance(status_obj, dict) else {}
    state = type_obj.get("state") if isinstance(type_obj, dict) else None
    completed = bool(type_obj.get("completed")) if isinstance(type_obj, dict) else False

    competitors_out = []
    for c in comp.get("competitors", []):
        team_obj = c.get("team") or {}
        score_raw = c.get("score")
        score_val = None
        if isinstance(score_raw, dict):
            val = score_raw.get("value")
            if val is not None:
                score_val = int(float(val))
        elif score_raw is not None and str(score_raw).strip() != "":
            try:
                score_val = int(float(str(score_raw)))
            except Exception:
                score_val = None

        competitors_out.append({
            "team": {
                "id": str(team_obj.get("id", "")),
                "name": team_obj.get("displayName") or team_obj.get("name") or "",
                "short_name": team_obj.get("shortDisplayName") or team_obj.get("name") or "",
                "abbreviation": team_obj.get("abbreviation") or "",
            },
            "qualifier": c.get("homeAway"),
            "score": score_val,
        })

    return {
        "id": str(ev.get("id", "")),
        "start_time": comp.get("date") or ev.get("date") or "",
        "status": _map_status(state, completed),
        "competitors": competitors_out,
        "odds": _parse_odds_details(comp),
    }


def get_team_schedule(team_id, league_slug="ligue-1", season_year=None, competition_id=None):
    """
    Récupère l'historique des matchs d'une équipe (utilisé pour les stats de forme).
    Note : on ne passe plus le paramètre 'season' qui forçait l'API à ne renvoyer
    que les matchs passés de la saison spécifiée.
    """
    slug = league_slug or competition_id or "ligue-1"
    espn_league = _LEAGUE_MAP.get(str(slug).lower(), str(slug))
    url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{espn_league}/teams/{team_id}/schedule"
    try:
        r = requests.get(url, headers=_HEADERS, timeout=12)
        if r.status_code != 200:
            return {"data": {"events": []}}
        payload = r.json()
    except Exception:
        return {"data": {"events": []}}

    events_out = [_parse_event(ev) for ev in payload.get("events", [])]
    return {"data": {"events": events_out}}


def get_fixtures_by_date(league_slug, date_str):
    """
    NOUVEAU : récupère les matchs d'une compétition pour une date donnée (YYYYMMDD).
    Utilise l'endpoint scoreboard d'ESPN, qui renvoie bien les matchs à venir.
    """
    espn_league = _LEAGUE_MAP.get(str(league_slug).lower(), str(league_slug))
    url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{espn_league}/scoreboard"
    try:
        r = requests.get(url, params={"dates": date_str}, headers=_HEADERS, timeout=12)
        if r.status_code != 200:
            return {"data": {"events": []}}
        payload = r.json()
    except Exception:
        return {"data": {"events": []}}

    events_out = [_parse_event(ev) for ev in payload.get("events", [])]
    return {"data": {"events": events_out}}
