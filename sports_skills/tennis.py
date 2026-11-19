import requests

_HEADERS = {"User-Agent": "Mozilla/5.0"}


def get_rankings(tour="atp"):
    t = "atp" if str(tour).lower().strip() != "wta" else "wta"
    url = f"https://site.api.espn.com/apis/site/v2/sports/tennis/{t}/rankings"
    rankings_out = []

    try:
        r = requests.get(url, headers=_HEADERS, timeout=12)
        if r.status_code == 200:
            data = r.json()
            for grp in data.get("rankings", []):
                for entry in grp.get("ranks", []):
                    athlete = entry.get("athlete") or {}
                    nom = athlete.get("displayName") or athlete.get("fullName") or ""
                    rk = entry.get("current")
                    if nom and rk is not None:
                        rankings_out.append({"name": nom, "rank": int(rk)})
    except Exception:
        pass

    return {"data": {"rankings": rankings_out}}


def _map_tennis_status(status_obj):
    if not isinstance(status_obj, dict):
        return "not_started"
    st_type = status_obj.get("type") or {}
    if st_type.get("completed") or str(st_type.get("state", "")).lower() == "post":
        return "closed"
    return "not_started"


def get_scoreboard():
    tournaments_out = []

    for tour_key, tour_label in (("atp", "Men"), ("wta", "Women")):
        url = f"https://site.api.espn.com/apis/site/v2/sports/tennis/{tour_key}/scoreboard"
        try:
            r = requests.get(url, headers=_HEADERS, timeout=12)
            if r.status_code != 200:
                continue
            data = r.json()
        except Exception:
            continue

        for ev in data.get("events", []):
            t_name = ev.get("name") or f"Tournoi {tour_key.upper()}"
            draws_out = []
            groupings = ev.get("groupings") or []

            if groupings:
                for g in groupings:
                    g_info = g.get("grouping") or {}
                    draw_name = g_info.get("displayName") or f"{tour_label} Singles"
                    matches_out = []

                    for comp in g.get("competitions", []):
                        competitors_out = []
                        for c in comp.get("competitors", []):
                            athlete = c.get("athlete") or {}
                            roster = c.get("roster") or {}
                            nom = (
                                athlete.get("displayName")
                                or roster.get("displayName")
                                or c.get("displayName")
                                or "TBD"
                            )
                            seed_obj = c.get("curatedRank") or {}
                            seed_val = seed_obj.get("current") if isinstance(seed_obj, dict) else None
                            competitors_out.append({
                                "name": nom,
                                "seed": seed_val,
                                "winner": bool(c.get("winner")),
                            })

                        res_txt = ""
                        notes = comp.get("notes") or []
                        if notes and isinstance(notes[0], dict):
                            res_txt = str(notes[0].get("text") or "")

                        if not res_txt and len(competitors_out) == 2:
                            c_a, c_b = competitors_out
                            if c_a["winner"] or c_b["winner"]:
                                g_nom, p_nom = (c_a["name"], c_b["name"]) if c_a["winner"] else (c_b["name"], c_a["name"])
                                res_txt = f"{g_nom} bt {p_nom}"

                        matches_out.append({
                            "id": str(comp.get("id", "")),
                            "status": _map_tennis_status(comp.get("status")),
                            "date": comp.get("date") or ev.get("date") or "",
                            "competitors": competitors_out,
                            "result": res_txt,
                        })

                    draws_out.append({"name": draw_name, "matches": matches_out})

            tournaments_out.append({"name": t_name, "draws": draws_out})

    return {"data": {"tournaments": tournaments_out}}
