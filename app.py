import base64
import csv
import hmac
import importlib
import io
import json
import logging
import math
import os
import re
import struct
import sys
import threading
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
import zlib
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import requests
from flask import Flask, Response, jsonify, make_response, redirect, render_template_string, request, url_for

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


fb = _charger_module_sport("compétences_sportives.football", "sports_skills.football")
tn = _charger_module_sport("compétences_sportives.tennis", "sports_skills.tennis")

TZ_PARIS = ZoneInfo("Europe/Paris")

STATUTS_TERMINES = {
    "closed", "final", "post", "completed", "finished",
    "ft", "aet", "pen", "status_final", "status_post",
}
STATUTS_EN_COURS = {
    "in_progress", "live", "ht", "halftime", "1st_half", "2nd_half",
    "et", "extra_time", "pen_live", "playing", "status_in_progress",
}
STATUTS_EXCLUS_AVENIR = STATUTS_TERMINES | STATUTS_EN_COURS | {
    "postponed", "cancelled", "canceled", "suspended",
}

MOTS_ALERTE_FORT = (
    "forfait", "blessure", "blessé", "blessée", "opéré", "opérée",
    "rupture", "suspendu", "injured",
)
MOTS_ALERTE_FAIBLE = ("incertain", "incertaine", "doute", "gêne", "doubtful", "knock")
MOTS_BRUIT_RSS = (
    "rumeur", "mercato", "transfert", "interview", "podcast", "quiz",
    "pronostic", "pronostics", "cote", "cotes", "direct live", "résumé",
)

COEFFS_COMP_FOOT = {
    "Ligue 1": {"base_buts": 1.28, "mult_home": 1.12, "mult_away": 0.90},
    "Liga Espagnole": {"base_buts": 1.26, "mult_home": 1.13, "mult_away": 0.89},
    "Bundesliga": {"base_buts": 1.44, "mult_home": 1.14, "mult_away": 0.93},
    "Liga Portugaise": {"base_buts": 1.32, "mult_home": 1.15, "mult_away": 0.88},
    "Premier League": {"base_buts": 1.38, "mult_home": 1.11, "mult_away": 0.92},
    "Serie A": {"base_buts": 1.27, "mult_home": 1.12, "mult_away": 0.89},
    "Ligue des Champions": {"base_buts": 1.36, "mult_home": 1.10, "mult_away": 0.91},
    "Europa League": {"base_buts": 1.34, "mult_home": 1.11, "mult_away": 0.90},
}
COEFF_COMP_DEFAUT = {"base_buts": 1.32, "mult_home": 1.12, "mult_away": 0.90}


@dataclass(frozen=True)
class Config:
    saison_foot: str = field(default_factory=lambda: (os.getenv("SAISON_FOOT") or "2026").strip())
    n_derniers_matchs: int = field(default_factory=lambda: max(1, int(os.getenv("N_DERNIERS_MATCHS", "8"))))
    fenetre_heures: int = field(default_factory=lambda: max(1, int(os.getenv("FENETRE_HEURES", "48"))))
    max_age_rss_heures: int = field(default_factory=lambda: max(6, int(os.getenv("MAX_AGE_RSS_HEURES", "48"))))
    timeout_http: float = field(default_factory=lambda: max(1.0, float(os.getenv("TIMEOUT_HTTP", "4"))))
    cache_ttl_sec: int = field(default_factory=lambda: max(60, int(os.getenv("CACHE_TTL_SEC", "900"))))
    mise_unite_defaut: float = field(default_factory=lambda: max(0.0, float(os.getenv("MISE_UNITE_DEFAUT", "1.0"))))
    mise_max_unites: float = field(default_factory=lambda: max(0.5, float(os.getenv("MISE_MAX_UNITES", "1.5"))))
    marge_value_pct: float = field(default_factory=lambda: max(0.0, float(os.getenv("MARGE_VALUE_PCT", "4.0"))))
    auto_refresh_sec: int = field(default_factory=lambda: max(0, int(os.getenv("AUTO_REFRESH_SEC", "0"))))
    activer_debug_routes: bool = field(
        default_factory=lambda: os.getenv("ACTIVER_DEBUG_ROUTES", "false").strip().lower() in ("1", "true", "yes")
    )
    odds_api_key: str = field(default_factory=lambda: os.getenv("ODDS_API_KEY", "").strip())
    odds_cache_ttl_sec: int = field(default_factory=lambda: max(300, int(os.getenv("ODDS_CACHE_TTL_SEC", "7200"))))
    admin_token: str = field(default_factory=lambda: (os.getenv("ADMIN_TOKEN") or os.getenv("BETPULSE_ADMIN_TOKEN") or "").strip())
    refresh_rate_limit_sec: float = field(default_factory=lambda: max(0.0, float(os.getenv("REFRESH_RATE_LIMIT_SEC", "15"))))
    statut_rate_limit_sec: float = field(default_factory=lambda: max(0.0, float(os.getenv("STATUT_RATE_LIMIT_SEC", "2"))))


CFG = Config()

ICONE_SVG_MARKUP = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <defs>
    <linearGradient id="bgGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#0b1324"/>
      <stop offset="55%" stop-color="#0f1e36"/>
      <stop offset="100%" stop-color="#091629"/>
    </linearGradient>
    <linearGradient id="pulseGrad" x1="0%" y1="100%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#10b981"/>
      <stop offset="55%" stop-color="#38bdf8"/>
      <stop offset="100%" stop-color="#a7f3d0"/>
    </linearGradient>
    <linearGradient id="ballGrad" x1="20%" y1="15%" x2="85%" y2="85%">
      <stop offset="0%" stop-color="#ffffff"/>
      <stop offset="65%" stop-color="#e2e8f0"/>
      <stop offset="100%" stop-color="#94a3b8"/>
    </linearGradient>
    <radialGradient id="glowSpot" cx="72%" cy="28%" r="45%">
      <stop offset="0%" stop-color="#38bdf8" stop-opacity="0.22"/>
      <stop offset="100%" stop-color="#38bdf8" stop-opacity="0"/>
    </radialGradient>
  </defs>

  <rect width="512" height="512" rx="118" fill="url(#bgGrad)"/>
  <rect width="512" height="512" rx="118" fill="url(#glowSpot)"/>

  <path d="M 96 152 H 416 M 96 256 H 416 M 96 360 H 416" stroke="#38bdf8" stroke-opacity="0.08" stroke-width="2"/>
  <path d="M 152 96 V 416 M 256 96 V 416 M 360 96 V 416" stroke="#38bdf8" stroke-opacity="0.08" stroke-width="2"/>

  <path d="M 104 358 A 186 186 0 0 1 398 134" fill="none" stroke="url(#pulseGrad)" stroke-width="14" stroke-linecap="round" opacity="0.45"/>

  <polyline points="92,334 176,334 218,224 266,368 318,176 414,176"
            fill="none" stroke="url(#pulseGrad)" stroke-width="22" stroke-linecap="round" stroke-linejoin="round"/>

  <circle cx="414" cy="176" r="18" fill="#10b981"/>
  <circle cx="414" cy="176" r="8" fill="#ecfdf5"/>

  <circle cx="136" cy="366" r="54" fill="url(#ballGrad)" stroke="#0f172a" stroke-width="6"/>
  <polygon points="136,336 157,351 149,377 123,377 115,351" fill="#0f172a"/>
  <path d="M136,336 L136,312 M157,351 L180,343 M149,377 L164,398 M123,377 L108,398 M115,351 L92,343"
        stroke="#0f172a" stroke-width="5" stroke-linecap="round"/>
</svg>"""


def _encoder_png_rgba(largeur, hauteur, pixels_rgba):
    def _chunk(tag, data):
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack("!I", len(data)) + tag + data + struct.pack("!I", crc)

    raw_rows = bytearray()
    stride = largeur * 4
    for y in range(hauteur):
        raw_rows.append(0)  # filtre type 0 (None)
        start = y * stride
        raw_rows.extend(pixels_rgba[start:start + stride])

    ihdr = struct.pack("!IIBBBBB", largeur, hauteur, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw_rows), level=6)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )


def _dist_segment(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    c1 = vx * wx + vy * wy
    if c1 <= 0.0:
        return math.hypot(px - ax, py - ay)
    c2 = vx * vx + vy * vy
    if c2 <= c1:
        return math.hypot(px - bx, py - by)
    b = c1 / c2
    pbx, pby = ax + b * vx, ay + b * vy
    return math.hypot(px - pbx, py - pby)


def _generer_png_pur_python(taille):
    w = h = int(taille)
    buf = bytearray(w * h * 4)

    pts = [
        (w * 0.18, h * 0.65),
        (w * 0.34, h * 0.65),
        (w * 0.43, h * 0.43),
        (w * 0.52, h * 0.72),
        (w * 0.62, h * 0.34),
        (w * 0.81, h * 0.34),
    ]
    stroke_w = max(2.2, w * 0.042)
    dot_x, dot_y, dot_r = w * 0.81, h * 0.34, max(3.5, w * 0.036)
    ball_x, ball_y, ball_r = w * 0.27, h * 0.72, max(6.0, w * 0.105)
    ring_r_out, ring_r_in = w * 0.36, w * 0.335
    cx_center, cy_center = w * 0.50, h * 0.50

    for y in range(h):
        for x in range(w):
            # Fond bleu nuit profond (#0b1324 -> #11223b)
            t_y = y / max(1, h - 1)
            t_x = x / max(1, w - 1)
            r = 11 + int(8 * t_x + 6 * (1.0 - t_y))
            g = 19 + int(15 * t_x + 12 * (1.0 - t_y))
            b = 36 + int(28 * t_x + 18 * (1.0 - t_y))
            a = 255

            # Arc subtil extérieur
            dist_c = math.hypot(x - cx_center, y - cy_center)
            ang = math.atan2(y - cy_center, x - cx_center)
            if ring_r_in <= dist_c <= ring_r_out and (-2.6 <= ang <= 0.4):
                r = int(r * 0.65 + 56 * 0.35)
                g = int(g * 0.65 + 189 * 0.35)
                b = int(b * 0.65 + 248 * 0.35)

            # Courbe de tendance (dégradé vert émeraude -> cyan)
            min_d = 1e9
            seg_t = 0.0
            for i in range(len(pts) - 1):
                ax, ay = pts[i]
                bx, by = pts[i + 1]
                d = _dist_segment(x, y, ax, ay, bx, by)
                if d < min_d:
                    min_d = d
                    seg_t = (i + 0.5) / max(1, len(pts) - 1)

            if min_d <= stroke_w * 1.35:
                alpha_line = max(0.0, min(1.0, (stroke_w * 1.35 - min_d) / max(0.8, stroke_w * 0.45)))
                lr = int(16 * (1.0 - seg_t) + 56 * seg_t)
                lg = int(185 * (1.0 - seg_t) + 189 * seg_t)
                lb = int(129 * (1.0 - seg_t) + 248 * seg_t)
                r = int(r * (1.0 - alpha_line) + lr * alpha_line)
                g = int(g * (1.0 - alpha_line) + lg * alpha_line)
                b = int(b * (1.0 - alpha_line) + lb * alpha_line)

            # Point lumineux en bout de courbe
            d_dot = math.hypot(x - dot_x, y - dot_y)
            if d_dot <= dot_r:
                r, g, b = 236, 253, 245

            # Sphère sportive en bas à gauche
            d_ball = math.hypot(x - ball_x, y - ball_y)
            if d_ball <= ball_r:
                if d_ball >= ball_r * 0.78:
                    r, g, b = 15, 23, 42
                elif d_ball <= ball_r * 0.34:
                    r, g, b = 15, 23, 42
                else:
                    r, g, b = 241, 245, 249

            idx = (y * w + x) * 4
            buf[idx] = max(0, min(255, r))
            buf[idx + 1] = max(0, min(255, g))
            buf[idx + 2] = max(0, min(255, b))
            buf[idx + 3] = a

    return _encoder_png_rgba(w, h, bytes(buf))


def _construire_png_icone(taille=192):
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return _generer_png_pur_python(taille)

    img = Image.new("RGBA", (taille, taille), (11, 19, 36, 255))
    draw = ImageDraw.Draw(img)
    pad = max(2, int(taille * 0.04))
    draw.rounded_rectangle([pad, pad, taille - pad, taille - pad], radius=int(taille * 0.22), fill=(15, 30, 54, 255))

    arc_pad = int(taille * 0.15)
    draw.arc([arc_pad, arc_pad, taille - arc_pad, taille - arc_pad], start=205, end=25, fill=(56, 189, 248, 130), width=max(2, int(taille * 0.025)))

    pts = [
        (taille * 0.18, taille * 0.65),
        (taille * 0.34, taille * 0.65),
        (taille * 0.43, taille * 0.43),
        (taille * 0.52, taille * 0.72),
        (taille * 0.62, taille * 0.34),
        (taille * 0.81, taille * 0.34),
    ]
    draw.line(pts, fill=(16, 185, 129, 255), width=max(3, int(taille * 0.045)), joint="curve")

    r_dot = max(4, int(taille * 0.04))
    cx, cy = taille * 0.81, taille * 0.34
    draw.ellipse([cx - r_dot, cy - r_dot, cx + r_dot, cy + r_dot], fill=(56, 189, 248, 255))

    r_ball = max(8, int(taille * 0.105))
    bx, by = taille * 0.27, taille * 0.72
    draw.ellipse([bx - r_ball, by - r_ball, bx + r_ball, by + r_ball], fill=(241, 245, 249, 255), outline=(15, 23, 42, 255), width=max(2, int(taille * 0.015)))
    r_core = max(3, int(r_ball * 0.34))
    draw.ellipse([bx - r_core, by - r_core, bx + r_core, by + r_core], fill=(15, 23, 42, 255))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


_PNG_192 = _construire_png_icone(192)
_PNG_512 = _construire_png_icone(512)

FAVICON_HEAD_TAGS = """
  <link rel="icon" type="image/svg+xml" href="/icon.svg">
  <link rel="apple-touch-icon" sizes="180x180" href="/icon-192.png">
  <link rel="manifest" href="/manifest.json">
  <meta name="theme-color" content="#0b1324">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="BetPulse Pro">
"""


class _LimiteDebit:
    def __init__(self, max_cles=2048):
        self._max_cles = max(64, int(max_cles))
        self._dernier_appel = OrderedDict()
        self._lock = threading.Lock()

    def verifier_et_enregistrer(self, cle, min_interval_sec):
        if min_interval_sec <= 0:
            return True, 0.0
        now = time.monotonic()
        with self._lock:
            prev = self._dernier_appel.get(cle)
            if prev is not None:
                ecoule = now - prev
                if ecoule < min_interval_sec:
                    return False, round(min_interval_sec - ecoule, 1)
            self._dernier_appel[cle] = now
            self._dernier_appel.move_to_end(cle)
            while len(self._dernier_appel) > self._max_cles:
                self._dernier_appel.popitem(last=False)
        return True, 0.0


_RATE_LIMITER = _LimiteDebit(max_cles=2048)


def _identifiant_client():
    xff = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return xff or (request.remote_addr or "local").strip()


def _extraire_token_requete(payload=None):
    token_hdr = (request.headers.get("X-Admin-Token") or "").strip()
    if token_hdr:
        return token_hdr
    auth_hdr = (request.headers.get("Authorization") or "").strip()
    if auth_hdr.lower().startswith("bearer "):
        return auth_hdr[7:].strip()
    if isinstance(payload, dict) and payload.get("admin_token"):
        return str(payload.get("admin_token")).strip()
    form_tok = (request.form.get("admin_token") or "").strip()
    if form_tok:
        return form_tok
    return (request.args.get("admin_token") or "").strip()


def _verifier_acces_admin(route_tag, min_interval_sec, payload=None, json_mode=False):
    if CFG.admin_token:
        fourni = _extraire_token_requete(payload)
        if not fourni or not hmac.compare_digest(fourni, CFG.admin_token):
            logger.warning("Accès refusé (token invalide ou manquant) sur %s depuis %s", route_tag, _identifiant_client())
            if json_mode:
                return make_response(jsonify({"ok": False, "error": "Token d'administration requis ou invalide"}), 403)
            return make_response("Accès refusé : token d'administration invalide.", 403)

    cle_rl = f"{route_tag}:{_identifiant_client()}"
    autorise, retry_sec = _RATE_LIMITER.verifier_et_enregistrer(cle_rl, min_interval_sec)
    if not autorise:
        logger.warning("Rate-limit atteint sur %s (%s) — réessayer dans %.1fs", route_tag, _identifiant_client(), retry_sec)
        if json_mode:
            resp = make_response(
                jsonify({"ok": False, "error": f"Trop de requêtes. Réessayez dans {retry_sec}s.", "retry_after": retry_sec}),
                429,
            )
        else:
            resp = make_response(f"Trop de requêtes sur {route_tag}. Réessayez dans {retry_sec}s.", 429)
        resp.headers["Retry-After"] = str(max(1, int(math.ceil(retry_sec))))
        return resp
    return None


MAP_COMP_ODDS_API = {
    "ligue-1": "soccer_france_ligue_one",
    "la-liga": "soccer_spain_la_liga",
    "bundesliga": "soccer_germany_bundesliga",
    "portuguese-liga": "soccer_portugal_primeira_liga",
    "premier-league": "soccer_epl",
    "serie-a": "soccer_italy_serie_a",
    "uefa-champions-league": "soccer_uefa_champs_league",
    "uefa-europa-league": "soccer_uefa_europa_league",
}

BOOKMAKERS_FR_PRIORITAIRES = {
    "winamax_fr", "winamax_de", "betclic_fr", "parionssport_fr",
    "unibet_fr", "pmu_fr", "bwin_fr", "zebet_fr",
}


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

    def clear(self):
        with self._lock:
            self._data.clear()


def _resoudre_chemin_persistant(env_var, nom_fichier_defaut):
    env_path = os.getenv(env_var, "").strip()
    if env_path:
        return env_path
    dossier_script = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
    try:
        os.makedirs(dossier_script, exist_ok=True)
        test_file = os.path.join(dossier_script, ".write_test")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test_file)
        return os.path.join(dossier_script, nom_fichier_defaut)
    except Exception:
        return os.path.join("/tmp", nom_fichier_defaut)


FICHIER_HISTORIQUE = _resoudre_chemin_persistant("FICHIER_HISTORIQUE", "historique_paris.csv")
FICHIER_FAVORIS = _resoudre_chemin_persistant("FICHIER_FAVORIS", "favoris_matchs.json")
FICHIER_RAPPORT = os.getenv("FICHIER_RAPPORT", "/tmp/rapport.json").strip() or "/tmp/rapport.json"


def _nettoyer_fichiers_tmp_orphelins():
    cibles = []
    for chemin in (FICHIER_RAPPORT, FICHIER_HISTORIQUE, FICHIER_FAVORIS):
        if chemin:
            dossier = os.path.dirname(os.path.abspath(chemin))
            base = os.path.basename(chemin)
            cibles.append((dossier, f"{base}.tmp"))

    cibles.append(("/tmp", "rapport.json.tmp"))
    cibles.append(("/tmp", "historique_paris.csv.tmp"))
    cibles.append(("/tmp", "favoris_matchs.json.tmp"))

    vus = set()
    supprimes = 0
    for dossier, nom_tmp in cibles:
        chemin_tmp = os.path.join(dossier, nom_tmp)
        if chemin_tmp in vus:
            continue
        vus.add(chemin_tmp)
        try:
            if os.path.isfile(chemin_tmp):
                os.remove(chemin_tmp)
                supprimes += 1
        except Exception as e:
            logger.debug("Impossible de supprimer le fichier tmp %s : %s", chemin_tmp, e)

    if supprimes:
        logger.info("Nettoyage au démarrage : %d fichier(s) .tmp orphelin(s) supprimé(s).", supprimes)


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
_CACHE_COTES_MARCHE = _CacheBorne(max_taille=64)
_STATE_LOCK = threading.Lock()
_CSV_LOCK = threading.Lock()
_FAV_LOCK = threading.Lock()
_ANALYSE_EN_COURS = False
_SCHEDULER_DEMARRE = False

_FAVORIS_ACTIFS = {}

_DERNIER_RAPPORT = {
    "mis_a_jour": None,
    "stats_comp": {},
    "apercu_bruts": "",
    "en_cours_matchs": [],
    "top_sur": [],
    "foot": [],
    "tennis": [],
    "favoris_suivis": [],
    "alertes": {},
    "bilan": {},
    "synthese": "",
}


def _normaliser_nom_equipe(nom):
    s = unicodedata.normalize("NFKD", str(nom or "")).encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"\b(fc|sc|ac|as|rc|rcd|cf|ud|cd|afc|ssc|sl|sd)\b", " ", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return " ".join(s.split())


def _memes_equipes(nom_a, nom_b):
    na = _normaliser_nom_equipe(nom_a)
    nb = _normaliser_nom_equipe(nom_b)
    if not na or not nb:
        return False
    return na == nb or (len(na) >= 4 and (na in nb or nb in na))


def _construire_cle_favori(sport, event_id="", match="", date_match=""):
    sp = str(sport or "").strip().upper() or "SPORT"
    eid = str(event_id or "").strip()
    if eid:
        return f"{sp}:{eid}"
    m_norm = re.sub(r"\s+", " ", _normaliser_nom_equipe(match))
    d_norm = str(date_match or "").strip()[:16]
    return f"{sp}:{m_norm}:{d_norm}"


def _charger_favoris_disque():
    global _FAVORIS_ACTIFS
    with _FAV_LOCK:
        if not os.path.exists(FICHIER_FAVORIS):
            _FAVORIS_ACTIFS = {}
            return
        try:
            with open(FICHIER_FAVORIS, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _FAVORIS_ACTIFS = {str(k): v for k, v in data.items() if isinstance(v, dict)}
        except Exception as e:
            logger.warning("Impossible de charger les favoris disque : %s", e)
            _FAVORIS_ACTIFS = {}


def _sauvegarder_favoris_disque_locked():
    tmp_path = f"{FICHIER_FAVORIS}.tmp"
    try:
        parent = os.path.dirname(FICHIER_FAVORIS)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(_FAVORIS_ACTIFS, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, FICHIER_FAVORIS)
    except Exception as e:
        logger.warning("Impossible de sauvegarder les favoris disque : %s", e)
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def _obtenir_cles_favoris():
    with _FAV_LOCK:
        return set(_FAVORIS_ACTIFS.keys())


def _safe_float(val, default=None):
    if val is None or str(val).strip() == "":
        return default
    try:
        return float(str(val).strip().replace(",", "."))
    except Exception:
        return default


def basculer_favori(item_data, force_etat=None):
    cle = str(item_data.get("cle_favori") or "").strip() or _construire_cle_favori(
        item_data.get("sport"),
        item_data.get("event_id"),
        item_data.get("match"),
        item_data.get("date_match"),
    )

    with _FAV_LOCK:
        deja_present = cle in _FAVORIS_ACTIFS
        if force_etat is True:
            ajouter = True
        elif force_etat is False:
            ajouter = False
        else:
            ajouter = not deja_present

        if ajouter:
            existant = _FAVORIS_ACTIFS.get(cle, {})
            proba_val = _safe_float(item_data.get("proba"), existant.get("proba"))
            cote_val = _safe_float(item_data.get("cote"), existant.get("cote"))
            mise_val = _safe_float(item_data.get("mise_u"), existant.get("mise_u"))
            conf_sc_val = _safe_float(item_data.get("confiance_score"), existant.get("confiance_score"))
            ts_val = _safe_float(item_data.get("ts_match"), existant.get("ts_match") or 9_999_999_999.0)

            _FAVORIS_ACTIFS[cle] = {
                "cle_favori": cle,
                "sport": str(item_data.get("sport") or existant.get("sport") or "FOOT").upper(),
                "event_id": str(item_data.get("event_id") or existant.get("event_id") or ""),
                "match": str(item_data.get("match") or existant.get("match") or ""),
                "competition": str(item_data.get("competition") or existant.get("competition") or ""),
                "date_match": str(item_data.get("date_match") or existant.get("date_match") or ""),
                "ts_match": ts_val,
                "choix": str(item_data.get("choix") or existant.get("choix") or ""),
                "proba": proba_val,
                "cote": cote_val,
                "mise_u": mise_val,
                "confiance": str(item_data.get("confiance") or existant.get("confiance") or "MOYENNE"),
                "confiance_score": conf_sc_val,
                "detail": str(item_data.get("detail") or existant.get("detail") or ""),
                "etat_match": str(item_data.get("etat_match") or existant.get("etat_match") or "A_VENIR").upper(),
                "ajoute_le": existant.get("ajoute_le") or datetime.now(TZ_PARIS).strftime("%Y-%m-%d %H:%M"),
            }
        else:
            _FAVORIS_ACTIFS.pop(cle, None)

        _sauvegarder_favoris_disque_locked()
        cles_actuelles = list(_FAVORIS_ACTIFS.keys())

    return ajouter, cles_actuelles


def nettoyer_favoris_termines():
    with _STATE_LOCK:
        suivis = list(_DERNIER_RAPPORT.get("favoris_suivis") or [])
        cles_a_supprimer = {
            (it.get("cle_favori") or _construire_cle_favori(it.get("sport"), it.get("event_id"), it.get("match"), it.get("date_match")))
            for it in suivis
            if str(it.get("etat_match") or "").upper() == "TERMINE"
        }

    with _FAV_LOCK:
        for cle, meta in list(_FAVORIS_ACTIFS.items()):
            if str((meta or {}).get("etat_match") or "").upper() == "TERMINE":
                cles_a_supprimer.add(cle)
        supprimes = 0
        for cle in cles_a_supprimer:
            if _FAVORIS_ACTIFS.pop(cle, None) is not None:
                supprimes += 1
        if supprimes:
            _sauvegarder_favoris_disque_locked()
        restants = len(_FAVORIS_ACTIFS)

    return supprimes, restants


def _charger_rapport_disque():
    global _DERNIER_RAPPORT
    try:
        if os.path.exists(FICHIER_RAPPORT):
            with open(FICHIER_RAPPORT, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and data.get("mis_a_jour"):
                    data.setdefault("en_cours_matchs", [])
                    data.setdefault("favoris_suivis", [])
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


def _parse_pubdate_utc(pubdate_str):
    if not pubdate_str:
        return None
    s = str(pubdate_str).strip()
    try:
        dt = parsedate_to_datetime(s)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return _parse_date_utc(s)


def _timestamp_match(date_str):
    dt = _parse_date_utc(date_str)
    if dt is not None:
        return dt.timestamp()
    return 9_999_999_999.0


def _formater_date_locale(date_str):
    dt = _parse_date_utc(date_str)
    if dt is None:
        return str(date_str or "")[:16].replace("T", " ")
    return dt.astimezone(TZ_PARIS).strftime("%Y-%m-%d %H:%M")


def _cle_cle_chronologique(item):
    return (
        float(item.get("ts_match") if item.get("ts_match") is not None else _timestamp_match(item.get("date_match"))),
        -(float(item.get("confiance_score") or item.get("proba") or 0.0)),
        -(float(item.get("proba") or 0.0)),
    )


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


def _priorite_statut_evenement(ev):
    statut = str(ev.get("status") or "").strip().lower()
    if statut in STATUTS_EN_COURS or "live" in statut or "progress" in statut:
        return 3
    if statut not in STATUTS_EXCLUS_AVENIR:
        return 2
    if statut in STATUTS_TERMINES:
        return 1
    return 0


def _fusionner_evenements(existant, nouveau):
    p_ex = _priorite_statut_evenement(existant)
    p_nv = _priorite_statut_evenement(nouveau)

    if p_nv > p_ex:
        base, complement = dict(nouveau), existant
    else:
        base, complement = dict(existant), nouveau

    for k, v in complement.items():
        if k not in base or base[k] in (None, "", [], {}):
            if v not in (None, "", [], {}):
                base[k] = v
    return base


def _est_match_en_cours(ev):
    statut = str(ev.get("status") or "").strip().lower()
    if statut in STATUTS_TERMINES or statut in {"postponed", "cancelled", "canceled", "suspended", "scheduled", "pre"}:
        return False
    if statut in STATUTS_EN_COURS or "live" in statut or "progress" in statut:
        return True

    clock = str(ev.get("clock") or ev.get("minute") or ev.get("display_clock") or "").strip()
    dt = _parse_date_utc(ev.get("start_time") or ev.get("date"))
    if clock and dt:
        maintenant = datetime.now(timezone.utc)
        if timedelta(minutes=0) <= (maintenant - dt) <= timedelta(hours=4):
            return True
    return False


def _est_match_a_venir_dans_fenetre(ev, heures_max):
    if _est_match_en_cours(ev):
        return False
    statut = str(ev.get("status") or "").strip().lower()
    if statut in STATUTS_EXCLUS_AVENIR:
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
    best_h = best_a = 0
    max_pr = -1.0
    matrice_scores = {}

    for h in range(max_buts):
        pr_h = poisson_prob(h, lam_h)
        for a in range(max_buts):
            pr = pr_h * poisson_prob(a, lam_a)
            matrice_scores[(h, a)] = pr
            if pr > max_pr:
                max_pr = pr
                best_h, best_a = h, a

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
        return 1 / 3, 1 / 3, 1 / 3, 0.5, 0.5, "1 - 1", 1, 1, {(1, 1): 0.12}
    p_h, p_n, p_a = p_h / tot, p_n / tot, p_a / tot
    p_ov25_norm = min(max(p_over_25, 0.0), 1.0)
    return p_h, p_n, p_a, p_ov25_norm, 1.0 - p_ov25_norm, f"{best_h} - {best_a}", best_h, best_a, matrice_scores


def _score_compatible_avec_choix(h_sc, a_sc, choix, h_nom, a_nom):
    c_l = str(choix or "").strip().lower()
    h_l = _normaliser_nom_equipe(h_nom)
    a_l = _normaliser_nom_equipe(a_nom)

    if c_l == "match nul":
        return h_sc == a_sc

    if c_l.startswith("victoire "):
        cible = _normaliser_nom_equipe(c_l.replace("victoire ", "", 1))
        if not cible:
            return h_sc != a_sc
        if cible == h_l or (len(h_l) >= 3 and (cible in h_l or h_l in cible)):
            return h_sc > a_sc
        if cible == a_l or (len(a_l) >= 3 and (cible in a_l or a_l in cible)):
            return a_sc > h_sc
        return h_sc != a_sc

    if "+2.5" in c_l:
        return (h_sc + a_sc) >= 3

    if "-2.5" in c_l:
        return (h_sc + a_sc) <= 2

    return True


def _selectionner_score_exact_coherent(choix, h_nom, a_nom, best_h, best_a, matrice_scores, lam_h, lam_a):
    candidats = []

    if matrice_scores:
        for (h_sc, a_sc), pr in matrice_scores.items():
            if not _score_compatible_avec_choix(h_sc, a_sc, choix, h_nom, a_nom):
                continue
            penalite_volume = 0.0
            if (h_sc + a_sc) > 5:
                penalite_volume = 0.04 * ((h_sc + a_sc) - 5)
            score_pondere = float(pr) * (1.0 - penalite_volume)
            candidats.append((score_pondere, float(pr), int(h_sc), int(a_sc)))

    if candidats:
        candidats.sort(key=lambda x: (x[0], x[1], -(x[2] + x[3])), reverse=True)
        _, pr_exact, h_sel, a_sel = candidats[0]
        proba_exact_pct = round(max(0.5, min(95.0, pr_exact * 100.0)), 1)
        return f"{h_sel} - {a_sel}", h_sel, a_sel, proba_exact_pct

    h_sc, a_sc = int(best_h), int(best_a)
    c_l = str(choix or "").strip().lower()
    h_l = _normaliser_nom_equipe(h_nom)
    a_l = _normaliser_nom_equipe(a_nom)

    if c_l == "match nul":
        h_sc = a_sc = 1 if (lam_h + lam_a) >= 2.2 else 0
    elif c_l.startswith("victoire "):
        cible = _normaliser_nom_equipe(c_l.replace("victoire ", "", 1))
        if cible == a_l or (len(a_l) >= 3 and (cible in a_l or a_l in cible)):
            a_sc = max(1, int(round(lam_a)) or 1)
            h_sc = max(0, min(a_sc - 1, int(round(lam_h * 0.6))))
        else:
            h_sc = max(1, int(round(lam_h)) or 1)
            a_sc = max(0, min(h_sc - 1, int(round(lam_a * 0.6))))
    elif "+2.5" in c_l:
        if lam_h >= lam_a:
            h_sc, a_sc = 2, 1
        else:
            h_sc, a_sc = 1, 2
    elif "-2.5" in c_l:
        if lam_h >= lam_a:
            h_sc, a_sc = (1, 0) if lam_h >= 1.1 else (1, 1)
        else:
            h_sc, a_sc = (0, 1) if lam_a >= 1.1 else (1, 1)

    pr_secours = poisson_prob(h_sc, lam_h) * poisson_prob(a_sc, lam_a)
    proba_secours_pct = round(max(0.5, min(95.0, pr_secours * 100.0)), 1)
    return f"{h_sc} - {a_sc}", h_sc, a_sc, proba_secours_pct


def moyenne_ponderee_shrinkage(valeurs, prior_moyenne, prior_poids=1.5):
    if not valeurs:
        return prior_moyenne
    n = len(valeurs)
    poids = [1.0 + (i * 0.25) for i in range(n)]
    somme_poids = sum(poids)
    moy_obs = sum(v * p for v, p in zip(valeurs, poids)) / somme_poids
    return ((moy_obs * somme_poids) + (prior_moyenne * prior_poids)) / (somme_poids + prior_poids)


def _calculer_indice_confiance(proba_pct, fiable, gravite_alerte=0.0, est_nul=False):
    score = proba_pct
    if not fiable:
        score -= 10.0
    if gravite_alerte > 0.0:
        score -= min(gravite_alerte * 6.0, 14.0)
    if est_nul:
        score -= 8.0
    score = round(max(15.0, min(95.0, score)), 1)
    if fiable and gravite_alerte < 1.0 and not est_nul and score >= 65.0 and proba_pct >= 62.0:
        niveau = "HAUTE"
    elif score >= 48.0:
        niveau = "MOYENNE"
    else:
        niveau = "FAIBLE"
    return score, niveau


def _calculer_cote(proba_pct, marge=0.04):
    p = max(0.05, min(0.95, float(proba_pct or 50.0) / 100.0))
    fair_odds = 1.0 / p
    cote_broker = fair_odds * (1.0 - marge)
    return max(1.05, round(cote_broker, 2))


def _calculer_cote_min_et_mise(proba_pct, conf_score, fiable, est_nul=False, cote_reelle=None, est_cote_reelle=False):
    p = max(0.05, min(0.95, proba_pct / 100.0))
    marge_facteur = 1.0 + (CFG.marge_value_pct / 100.0)
    cote_min = round((1.0 / p) * marge_facteur, 2)

    est_value_bet = False
    ecart_value_pct = 0.0
    if est_cote_reelle and isinstance(cote_reelle, (int, float)) and cote_reelle > 1.0:
        ecart_value_pct = round(((float(cote_reelle) - cote_min) / cote_min) * 100.0, 1)
        est_value_bet = float(cote_reelle) >= cote_min

    if CFG.mise_unite_defaut <= 0:
        return cote_min, 0.0, est_value_bet, ecart_value_pct

    if not fiable or proba_pct < 50.0:
        mise = round(CFG.mise_unite_defaut * 0.25, 2)
    else:
        facteur_conf = max(0.35, min(1.35, conf_score / 60.0))
        facteur_nul = 0.65 if est_nul else 1.0
        facteur_value = 1.15 if est_value_bet else (0.75 if (est_cote_reelle and ecart_value_pct < -8.0) else 1.0)
        mise = round(
            min(CFG.mise_max_unites, max(0.25, CFG.mise_unite_defaut * facteur_conf * facteur_nul * facteur_value)),
            2,
        )
    return cote_min, mise, est_value_bet, ecart_value_pct


def charger_cotes_moyennes_foot(competitions):
    if not CFG.odds_api_key:
        return {}

    cotes_map = {}
    for comp in competitions:
        slug = comp.get("slug")
        label = comp.get("label")
        sport_key = MAP_COMP_ODDS_API.get(slug)
        if not sport_key:
            continue

        cached = _CACHE_COTES_MARCHE.get(sport_key, CFG.odds_cache_ttl_sec)
        if cached is None:
            try:
                resp = _HTTP_SESSION.get(
                    f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/",
                    params={
                        "apiKey": CFG.odds_api_key,
                        "regions": "eu,fr,uk",
                        "markets": "h2h",
                        "oddsFormat": "decimal",
                    },
                    timeout=CFG.timeout_http,
                )
                if resp.status_code != 200:
                    logger.debug("Odds API %s -> HTTP %s", sport_key, resp.status_code)
                    _CACHE_COTES_MARCHE.set(sport_key, [])
                    continue
                payload = resp.json()
                cached = payload if isinstance(payload, list) else []
                _CACHE_COTES_MARCHE.set(sport_key, cached)
            except Exception as e:
                logger.debug("Erreur Odds API (%s): %s", sport_key, e)
                continue

        for ev in cached:
            home_api = ev.get("home_team") or ""
            away_api = ev.get("away_team") or ""
            cotes_home = []
            cotes_draw = []
            cotes_away = []

            for bk in ev.get("bookmakers") or []:
                bk_key = str(bk.get("key") or "").lower()
                for mkt in bk.get("markets") or []:
                    if mkt.get("key") != "h2h":
                        continue
                    for oc in mkt.get("outcomes") or []:
                        prix = oc.get("price")
                        if not isinstance(prix, (int, float)) or prix <= 1.01:
                            continue
                        oc_nom = oc.get("name") or ""
                        if oc_nom == "Draw" or _normaliser_nom_equipe(oc_nom) == "draw":
                            cotes_draw.append((float(prix), bk_key in BOOKMAKERS_FR_PRIORITAIRES))
                        elif _memes_equipes(oc_nom, home_api):
                            cotes_home.append((float(prix), bk_key in BOOKMAKERS_FR_PRIORITAIRES))
                        elif _memes_equipes(oc_nom, away_api):
                            cotes_away.append((float(prix), bk_key in BOOKMAKERS_FR_PRIORITAIRES))

            def _moyenne_liste(liste_tuples):
                if not liste_tuples:
                    return None, 0
                fr_only = [p for p, is_fr in liste_tuples if is_fr]
                base = fr_only if fr_only else [p for p, _ in liste_tuples]
                return round(sum(base) / len(base), 2), len(base)

            moy_h, n_h = _moyenne_liste(cotes_home)
            moy_n, n_n = _moyenne_liste(cotes_draw)
            moy_a, n_a = _moyenne_liste(cotes_away)

            cle_match = (label, _normaliser_nom_equipe(home_api), _normaliser_nom_equipe(away_api))
            cotes_map[cle_match] = {
                "home": (moy_h, n_h),
                "draw": (moy_n, n_n),
                "away": (moy_a, n_a),
            }

    return cotes_map


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


def _formater_score_live(ev, sport="FOOT"):
    scores = _extraire_scores_evenement(ev)
    if len(scores) >= 2:
        s0 = scores[0]["score"] if scores[0]["score"] is not None else 0
        s1 = scores[1]["score"] if scores[1]["score"] is not None else 0
        if sport == "FOOT":
            return f"{s0} - {s1}"
        return f"{scores[0]['name']} {s0} - {s1} {scores[1]['name']}"
    return "En cours"


def _formater_chrono_live(ev):
    for champ in ("display_clock", "clock", "minute", "period", "status_detail"):
        val = ev.get(champ)
        if val is not None and str(val).strip():
            return str(val).strip()
    return "LIVE"


def stats_foot_detail(team_id, team_name="", events_override=None, n_derniers=8):
    events = list(events_override or [])
    joues = [ev for ev in events if str(ev.get("status", "")).lower() in STATUTS_TERMINES]
    joues.sort(key=lambda ev: _timestamp_match(ev.get("start_time") or ev.get("date")))

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
    a_historique = nb_matchs >= 2
    dynamique = max(-0.12, min(0.12, ((v - d) / nb_matchs) * 0.12)) if nb_matchs > 0 else 0.0

    return {
        "forme": f"{v}V-{n}N-{d}D" if nb_matchs > 0 else "Forme récente",
        "a_historique": a_historique,
        "nb_matchs": nb_matchs,
        "v": v,
        "n": n,
        "d": d,
        "dynamique": dynamique,
        "bp": moyenne_ponderee_shrinkage(bp_all, 1.35, prior_poids=1.5),
        "bc": moyenne_ponderee_shrinkage(bc_all, 1.20, prior_poids=1.5),
        "bp_home": moyenne_ponderee_shrinkage(bp_home, 1.50, prior_poids=1.2),
        "bc_home": moyenne_ponderee_shrinkage(bc_home, 1.10, prior_poids=1.2),
        "bp_away": moyenne_ponderee_shrinkage(bp_away, 1.20, prior_poids=1.2),
        "bc_away": moyenne_ponderee_shrinkage(bc_away, 1.30, prior_poids=1.2),
    }


def _titre_est_pertinent_pour_cible(titre, cible):
    t_str = str(titre or "").strip()
    if not t_str:
        return False
    t_norm = unicodedata.normalize("NFKD", t_str).encode("ascii", "ignore").decode("ascii").lower()
    c_norm = _normaliser_nom_equipe(cible)

    if any(b in t_norm for b in MOTS_BRUIT_RSS):
        return False

    a_mot_alerte = any(m in t_norm for m in MOTS_ALERTE_FORT) or any(m in t_norm for m in MOTS_ALERTE_FAIBLE)
    if not a_mot_alerte:
        return False

    if c_norm and len(c_norm) >= 3:
        mots_cible = [m for m in c_norm.split() if len(m) >= 3]
        if re.search(rf"\b{re.escape(c_norm)}\b", t_norm):
            return True
        if mots_cible and all(re.search(rf"\b{re.escape(m)}\b", t_norm) for m in mots_cible):
            return True
        return False

    return True


def _filtrer_titres_pertinents(titres, cible):
    retenus = []
    for t in titres or []:
        if _titre_est_pertinent_pour_cible(t, cible):
            retenus.append(str(t).strip())
    return retenus


def _evaluer_gravite_titres(titres, cible=""):
    pertinents = _filtrer_titres_pertinents(titres, cible) if cible else list(titres or [])
    gravite = 0.0
    for t in pertinents:
        tl = unicodedata.normalize("NFKD", str(t)).encode("ascii", "ignore").decode("ascii").lower()
        if any(m in tl for m in MOTS_ALERTE_FORT):
            gravite += 0.75
        elif any(m in tl for m in MOTS_ALERTE_FAIBLE):
            gravite += 0.25
    return min(gravite, 2.0)


def _requete_rss(requete_brute, max_titres=2, cible="", max_age_heures=None):
    q = urllib.parse.quote(requete_brute)
    url = f"https://news.google.com/rss/search?q={q}&hl=fr&gl=FR&ceid=FR:fr"
    age_limite_h = max_age_heures if max_age_heures is not None else CFG.max_age_rss_heures
    seuil_dt = datetime.now(timezone.utc) - timedelta(hours=max(1, int(age_limite_h)))
    titres = []
    try:
        r = _HTTP_SESSION.get(url, timeout=CFG.timeout_http)
        if r.status_code == 200 and r.text:
            root = ET.fromstring(r.text)
            for item in root.findall(".//item")[:10]:
                t = (item.findtext("title") or "").strip()
                if not t:
                    continue

                pub_raw = (item.findtext("pubDate") or item.findtext("dc:date") or "").strip()
                pub_dt = _parse_pubdate_utc(pub_raw)
                if pub_dt is not None and pub_dt < seuil_dt:
                    logger.debug("Titre RSS ignoré car trop ancien (%s): %s", pub_dt.isoformat(), t)
                    continue

                if cible and not _titre_est_pertinent_pour_cible(t, cible):
                    continue
                titres.append(t)
                if len(titres) >= max_titres:
                    break
    except Exception as e:
        logger.debug("Erreur RSS (%s): %s", requete_brute, e)
    return titres[:max_titres]


def scanner_sante_cible(cible, max_titres=2):
    cle = (str(cible).strip().lower(), max_titres)
    cached = _CACHE_SANTE.get(cle, CFG.cache_ttl_sec)
    if cached is not None:
        return _filtrer_titres_pertinents(cached, cible)[:max_titres]

    requete = f'"{cible}" (blessure OR forfait OR incertain OR suspendu) (match OR équipe OR foot OR tennis OR ATP OR WTA) when:2d'
    res = _requete_rss(requete, max_titres, cible=cible, max_age_heures=CFG.max_age_rss_heures)
    _CACHE_SANTE.set(cle, res)
    return list(res)


def _construire_entree_foot(ev, label_comp, events_comp):
    ev_id = str(ev.get("id") or "")
    start_str = str(ev.get("start_time") or ev.get("date") or "")
    ts_match = _timestamp_match(start_str)
    date_loc = _formater_date_locale(start_str)
    h_info, a_info = _normaliser_home_away(ev)
    h_nom = h_info.get("short_name") or h_info.get("name") or "Home"
    a_nom = a_info.get("short_name") or a_info.get("name") or "Away"
    match_lbl = f"{h_nom} vs {a_nom}"

    st_h = stats_foot_detail(h_info.get("id", ""), h_nom, events_comp, CFG.n_derniers_matchs)
    st_a = stats_foot_detail(a_info.get("id", ""), a_nom, events_comp, CFG.n_derniers_matchs)
    fiable = st_h["a_historique"] and st_a["a_historique"]

    coeffs = COEFFS_COMP_FOOT.get(label_comp, COEFF_COMP_DEFAUT)
    base_buts = max(0.8, float(coeffs.get("base_buts", 1.32)))
    mult_home = float(coeffs.get("mult_home", 1.12))
    mult_away = float(coeffs.get("mult_away", 0.90))

    h_att = ((0.55 * st_h["bp"]) + (0.45 * st_h["bp_home"])) * (1.0 + st_h["dynamique"])
    h_def = ((0.55 * st_h["bc"]) + (0.45 * st_h["bc_home"])) * (1.0 - (0.5 * st_h["dynamique"]))
    a_att = ((0.55 * st_a["bp"]) + (0.45 * st_a["bp_away"])) * (1.0 + st_a["dynamique"])
    a_def = ((0.55 * st_a["bc"]) + (0.45 * st_a["bc_away"])) * (1.0 - (0.5 * st_a["dynamique"]))

    lam_h = max(((h_att * a_def) / base_buts) * mult_home, 0.35)
    lam_a = max(((a_att * h_def) / base_buts) * mult_away, 0.35)
    p_h, p_n, p_a, p_ov25, p_un25, _, best_h, best_a, matrice_scores = simuler_poisson_foot(lam_h, lam_a)

    meilleure_victoire, prob_victoire = max(
        [(f"Victoire {h_nom}", p_h), (f"Victoire {a_nom}", p_a)],
        key=lambda x: x[1],
    )
    ecart_victoires = abs(p_h - p_a)
    buts_label = "+2.5 buts" if p_ov25 >= p_un25 else "-2.5 buts"
    buts_proba = round(min(max(p_ov25, p_un25), 0.88) * 100, 1)

    est_nul = False
    if (best_h == best_a and p_n >= 0.25 and ecart_victoires < 0.12) or (p_n >= 0.27 and (prob_victoire - p_n) <= 0.06):
        choix, prob = "Match nul", p_n
        est_nul = True
    elif ecart_victoires < 0.055 and max(p_ov25, p_un25) > prob_victoire + 0.06:
        choix, prob = buts_label, max(p_ov25, p_un25)
    else:
        choix, prob = meilleure_victoire, prob_victoire

    plafond = 0.86 if (fiable and not est_nul) else (0.55 if est_nul else 0.72)
    prob_cal = min(max(prob, 0.05), plafond)
    proba_pct = round(prob_cal * 100, 1)
    score_exact, _, _, score_exact_proba = _selectionner_score_exact_coherent(
        choix, h_nom, a_nom, best_h, best_a, matrice_scores, lam_h, lam_a
    )

    dt_match = _parse_date_utc(start_str)
    est_imminent = False
    if dt_match:
        delta_sec = (dt_match - datetime.now(timezone.utc)).total_seconds()
        if 0 <= delta_sec <= 7200:
            est_imminent = True

    cote_estimee = _calculer_cote(proba_pct)
    prudence = "ELEVE" if (proba_pct >= 70 and fiable and not est_nul) else ("MODERE" if proba_pct >= 55 else "PRUDENT")
    conf_score, conf_niveau = _calculer_indice_confiance(proba_pct, fiable, 0.0, est_nul=est_nul)
    cote_min, mise_u, est_value_bet, ecart_value_pct = _calculer_cote_min_et_mise(
        proba_pct, conf_score, fiable, est_nul=est_nul, cote_reelle=cote_estimee, est_cote_reelle=False
    )
    nul_note = " | Nul probable" if est_nul else ""
    cle_fav = _construire_cle_favori("FOOT", ev_id, match_lbl, date_loc)

    return {
        "sport": "FOOT",
        "competition": label_comp,
        "fiable": fiable,
        "event_id": ev_id,
        "cle_favori": cle_fav,
        "match": match_lbl,
        "date_match": date_loc,
        "ts_match": ts_match,
        "detail": (
            f"[{label_comp} | λ {lam_h:.2f}-{lam_a:.2f}] {h_nom} [{st_h['forme']}] vs {a_nom} [{st_a['forme']}] "
            f"| Buts: {buts_label} ({buts_proba:.0f}%){nul_note}"
        ),
        "choix": choix,
        "est_nul": est_nul,
        "proba": proba_pct,
        "cote": cote_estimee,
        "cote_source": "Estimée",
        "cote_min": cote_min,
        "est_value_bet": est_value_bet,
        "ecart_value_pct": ecart_value_pct,
        "score_exact": score_exact,
        "score_exact_proba": score_exact_proba,
        "est_imminent": est_imminent,
        "prudence": prudence,
        "confiance": conf_niveau,
        "confiance_score": conf_score,
        "mise_u": mise_u,
        "equipes": [h_nom, a_nom],
    }


def analyser_foot_competitions(competitions, heures_max=48):
    resultats_a_venir = []
    live_foot = []
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

        raw_comp_events = events_upcoming_par_slug[slug] + events_hist_par_slug[slug]
        dedup_par_id = {}
        sans_id = []

        for idx, ev in enumerate(raw_comp_events):
            eid = str(ev.get("id") or "").strip()
            if eid:
                if eid in dedup_par_id:
                    dedup_par_id[eid] = _fusionner_evenements(dedup_par_id[eid], ev)
                else:
                    dedup_par_id[eid] = ev
            else:
                sans_id.append((idx, ev))

        dedup_comp = dict(dedup_par_id)
        for idx, ev in sans_id:
            cle_sec = _cle_dedup_evenement(ev, idx)
            if cle_sec not in dedup_comp:
                dedup_comp[cle_sec] = ev
            else:
                dedup_comp[cle_sec] = _fusionner_evenements(dedup_comp[cle_sec], ev)

        events_comp = list(dedup_comp.values())
        a_venir_48h = [ev for ev in events_comp if _est_match_a_venir_dans_fenetre(ev, heures_max)]
        a_venir_48h.sort(key=lambda e: _timestamp_match(e.get("start_time") or e.get("date")))

        en_cours_evs = [ev for ev in events_comp if _est_match_en_cours(ev)]
        live_comp = []
        for ev in en_cours_evs:
            entree = _construire_entree_foot(ev, label_comp, events_comp)
            entree["en_cours"] = True
            entree["etat_match"] = "EN_COURS"
            entree["score_actuel"] = _formater_score_live(ev, "FOOT")
            entree["chrono_live"] = _formater_chrono_live(ev)
            live_comp.append(entree)

        clos_comp = {}
        for ev in events_comp:
            if str(ev.get("status") or "").strip().lower() in STATUTS_TERMINES and not _est_match_en_cours(ev):
                ev_enrichi = dict(ev)
                ev_enrichi["_sport"] = "FOOT"
                ev_enrichi["_competition"] = label_comp
                eid = str(ev.get("id") or "").strip()
                cle_ev = _construire_cle_favori(
                    "FOOT",
                    eid,
                    f"{_normaliser_home_away(ev)[0].get('short_name') or _normaliser_home_away(ev)[0].get('name') or 'Home'} vs {_normaliser_home_away(ev)[1].get('short_name') or _normaliser_home_away(ev)[1].get('name') or 'Away'}",
                    _formater_date_locale(ev.get("start_time") or ev.get("date")),
                )
                clos_comp[cle_ev] = ev_enrichi
                if eid:
                    clos_comp[eid] = ev_enrichi

        res_comp = []
        apercu_comp = []

        for ev in a_venir_48h:
            entree = _construire_entree_foot(ev, label_comp, events_comp)
            entree["etat_match"] = "A_VENIR"
            res_comp.append(entree)

            if len(apercu_comp) < 2:
                h, a = _normaliser_home_away(ev)
                apercu_comp.append(
                    f"{slug} | {_formater_date_locale(ev.get('start_time') or ev.get('date'))} | "
                    f"{h.get('short_name') or h.get('name') or '?'} vs {a.get('short_name') or a.get('name') or '?'}"
                )

        return label_comp, res_comp, live_comp, {}, len(clos_comp), len(events_comp), apercu_comp, clos_comp

    with ThreadPoolExecutor(max_workers=6) as ex:
        res_comps = list(ex.map(analyser_comp, competitions))

    for label_comp, res_comp, live_comp, alertes_comp, nb_clos, total_events, apercu_comp, clos_comp in res_comps:
        resultats_a_venir.extend(res_comp)
        live_foot.extend(live_comp)
        alertes_globales.update(alertes_comp)
        apercu_lignes.extend(apercu_comp)
        evenements_termines.update(clos_comp)
        stats_par_comp[label_comp] = (
            f"Live: {len(live_comp)} | À venir: {len(res_comp)} | Terminés: {nb_clos} (bruts: {total_events})"
        )

    logger.info("Couverture Foot : %s", " • ".join(f"{k}: {v}" for k, v in stats_par_comp.items()))
    return resultats_a_venir, live_foot, alertes_globales, stats_par_comp, "\n".join(apercu_lignes[:12]), evenements_termines


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


def _selectionner_classement_tennis(tournoi_nom, draw_nom, atp_rk, wta_rk):
    texte = f"{tournoi_nom or ''} {draw_nom or ''}".lower()
    if any(tok in texte for tok in ("wta", "women", "ladies", "femmes", "dames")):
        return wta_rk
    if any(tok in texte for tok in ("atp", "men", "hommes", "messieurs")):
        return atp_rk
    return atp_rk


def _detecter_surface_tennis(tournoi_nom, draw_nom, match_obj=None):
    parties = [str(tournoi_nom or ""), str(draw_nom or "")]
    if isinstance(match_obj, dict):
        for champ in ("surface", "court", "venue_surface"):
            if match_obj.get(champ):
                parties.append(str(match_obj[champ]))
        venue = match_obj.get("venue")
        if isinstance(venue, dict) and venue.get("surface"):
            parties.append(str(venue["surface"]))

    texte = unicodedata.normalize("NFKD", " ".join(parties)).encode("ascii", "ignore").decode("ascii").lower()
    if any(tok in texte for tok in ("terre battue", "clay", "roland garros", "rome", "madrid", "monte carlo", "barcelona", "buenos aires", "rio")):
        return "Terre battue", 1.06
    if any(tok in texte for tok in ("gazon", "grass", "wimbledon", "halle", "queen", "s-hertogenbosch", "mallorca")):
        return "Gazon", 1.08
    if any(tok in texte for tok in ("indoor", "salle", "paris masters", "bercy", "atp finals", "wta finals")):
        return "Indoor", 1.04
    return "Dur", 1.00


def _collecter_resultats_tennis(sb_data):
    historique_joueurs = {}
    for t in sb_data.get("tournaments", []):
        for d in t.get("draws", []):
            if "Doubles" in d.get("name", ""):
                continue
            for m in d.get("matches", []):
                if str(m.get("status") or "").strip().lower() not in STATUTS_TERMINES:
                    continue
                comps = m.get("competitors", [])
                if len(comps) != 2:
                    continue
                j1, j2 = str(comps[0].get("name") or "").strip(), str(comps[1].get("name") or "").strip()
                if not j1 or not j2 or "TBD" in (j1, j2):
                    continue

                gagnant = None
                if comps[0].get("winner") is True:
                    gagnant = j1
                elif comps[1].get("winner") is True:
                    gagnant = j2
                else:
                    try:
                        s0 = float(comps[0].get("score"))
                        s1 = float(comps[1].get("score"))
                        if s0 != s1:
                            gagnant = j1 if s0 > s1 else j2
                    except Exception:
                        gagnant = None

                if not gagnant:
                    continue
                perdant = j2 if gagnant == j1 else j1
                ts = _timestamp_match(str(m.get("date") or m.get("start_time") or ""))
                historique_joueurs.setdefault(" ".join(gagnant.lower().split()), []).append((ts, 1))
                historique_joueurs.setdefault(" ".join(perdant.lower().split()), []).append((ts, 0))

    for cle in historique_joueurs:
        historique_joueurs[cle].sort(key=lambda x: x[0])
    return historique_joueurs


def _ratio_forme_tennis(nom_joueur, hist_map, max_matchs=6):
    if not nom_joueur or not hist_map:
        return None
    cle = " ".join(nom_joueur.lower().strip().split())
    series = hist_map.get(cle)
    if not series:
        for k, v in hist_map.items():
            if len(cle) >= 5 and (cle in k or k in cle):
                series = v
                break
    if not series:
        return None
    recents = [res for _, res in series[-max_matchs:]]
    if not recents:
        return None
    return sum(recents) / len(recents)


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
        return [], [], {}, {}

    sb_data = sb.get("data", {}) if isinstance(sb, dict) else {}
    hist_tennis = _collecter_resultats_tennis(sb_data)

    simples = []
    live_tennis = []
    alertes_tennis = {}
    termines_tennis = {}

    for t in sb_data.get("tournaments", []):
        t_name = t.get("name")
        for d in t.get("draws", []):
            if "Doubles" in d.get("name", ""):
                continue
            rk_map = _selectionner_classement_tennis(t_name, d.get("name", ""), atp_rk, wta_rk)
            for m in d.get("matches", []):
                mid = str(m.get("id") or "").strip()
                statut = str(m.get("status") or "").strip().lower()
                date_raw = str(m.get("date") or m.get("start_time") or "")
                date_loc = _formater_date_locale(date_raw)

                comps = m.get("competitors", [])
                j1 = comps[0].get("name", "") if len(comps) >= 1 else ""
                j2 = comps[1].get("name", "") if len(comps) >= 2 else ""
                r1 = trouver_rang(j1, rk_map) if j1 else None
                r2 = trouver_rang(j2, rk_map) if j2 else None
                match_lbl = f"{j1} (#{r1 or 'NC'}) vs {j2} (#{r2 or 'NC'})" if (j1 and j2) else str(m.get("name") or "Match Tennis")

                if statut in STATUTS_TERMINES:
                    m_enrichi = dict(m)
                    m_enrichi["_sport"] = "TENNIS"
                    m_enrichi["_competition"] = t_name or "Tennis"
                    m_enrichi["_match_label"] = match_lbl
                    m_enrichi["_date_match"] = date_loc
                    cle_fav = _construire_cle_favori("TENNIS", mid, match_lbl, date_loc)
                    termines_tennis[cle_fav] = m_enrichi
                    if mid:
                        termines_tennis[mid] = m_enrichi

                est_live = _est_match_en_cours(m)
                if not est_live and not _est_match_a_venir_dans_fenetre(m, heures_max):
                    continue

                if len(comps) != 2 or "TBD" in (j1, j2):
                    continue
                complet = r1 is not None and r2 is not None

                surface_lbl, mult_surface = _detecter_surface_tennis(t_name, d.get("name", ""), m)
                forme1 = _ratio_forme_tennis(j1, hist_tennis)
                forme2 = _ratio_forme_tennis(j2, hist_tennis)
                ajust_forme = 0.0
                if forme1 is not None and forme2 is not None:
                    ajust_forme = (forme1 - forme2) * 0.45
                elif forme1 is not None:
                    ajust_forme = (forme1 - 0.5) * 0.25
                elif forme2 is not None:
                    ajust_forme = (0.5 - forme2) * 0.25

                diff_rang = (math.log((r2 or 180) + 1) - math.log((r1 or 180) + 1)) * (0.68 if complet else 0.25)
                diff = (diff_rang * mult_surface) + ajust_forme
                p1 = 1 / (1 + math.exp(-diff))
                favori, prob = (j1, p1) if p1 >= 0.5 else (j2, 1 - p1)
                plafond = 0.84 if complet else 0.65
                prob = min(max(prob, 0.50), plafond)

                proba_pct = round(max(0.05, prob) * 100, 1)

                ts_match = _timestamp_match(date_raw)
                dt_match = _parse_date_utc(date_raw)
                est_imminent = False
                if dt_match:
                    delta_sec = (dt_match - datetime.now(timezone.utc)).total_seconds()
                    if 0 <= delta_sec <= 7200:
                        est_imminent = True

                cote_estimee = _calculer_cote(proba_pct)
                conf_score, conf_niveau = _calculer_indice_confiance(proba_pct, complet, 0.0)
                cote_min, mise_u, est_value_bet, ecart_value_pct = _calculer_cote_min_et_mise(
                    proba_pct, conf_score, complet, est_nul=False, cote_reelle=cote_estimee, est_cote_reelle=False
                )

                f1_str = f" ({round(forme1 * 100)}%)" if forme1 is not None else ""
                f2_str = f" ({round(forme2 * 100)}%)" if forme2 is not None else ""
                match_affiche = f"{j1} (#{r1 or 'NC'}{f1_str}) vs {j2} (#{r2 or 'NC'}{f2_str})"
                cle_fav = _construire_cle_favori("TENNIS", mid, match_affiche, date_loc)

                entree = {
                    "sport": "TENNIS",
                    "competition": t_name or "Tennis",
                    "fiable": complet,
                    "event_id": mid,
                    "cle_favori": cle_fav,
                    "match": match_affiche,
                    "date_match": date_loc,
                    "ts_match": ts_match,
                    "detail": f"{t_name} • Surface : {surface_lbl}",
                    "choix": f"Victoire {favori}",
                    "est_nul": False,
                    "proba": proba_pct,
                    "cote": cote_estimee,
                    "cote_source": "Estimée",
                    "cote_min": cote_min,
                    "est_value_bet": est_value_bet,
                    "ecart_value_pct": ecart_value_pct,
                    "score_exact": None,
                    "score_exact_proba": None,
                    "est_imminent": est_imminent,
                    "prudence": "MODERE" if (proba_pct >= 58 and complet) else "PRUDENT",
                    "confiance": conf_niveau,
                    "confiance_score": conf_score,
                    "mise_u": mise_u,
                    "equipes": [favori],
                }
                if est_live:
                    entree["en_cours"] = True
                    entree["etat_match"] = "EN_COURS"
                    entree["score_actuel"] = _formater_score_live(m, "TENNIS")
                    entree["chrono_live"] = _formater_chrono_live(m)
                    live_tennis.append(entree)
                else:
                    entree["etat_match"] = "A_VENIR"
                    simples.append(entree)

    return simples, live_tennis, alertes_tennis, termines_tennis


def _charger_historique_lignes():
    with _CSV_LOCK:
        if not os.path.exists(FICHIER_HISTORIQUE):
            return []
        try:
            with open(FICHIER_HISTORIQUE, "r", newline="", encoding="utf-8") as f:
                return [
                    {k: (row.get(k) or "").strip() for k in CHAMPS_HISTORIQUE}
                    for row in csv.DictReader(f)
                    if any((v or "").strip() for v in row.values())
                ]
        except Exception as e:
            logger.warning("Erreur lecture historique CSV pour matchs live: %s", e)
            return []


def _enrichir_matchs_live_avec_pronostics(live_items, candidats_precalcul):
    lignes_hist = _charger_historique_lignes()
    par_event = {}
    for r in lignes_hist:
        cle_ev = f"{r.get('sport', '').upper()}_{r.get('event_id', '').strip()}"
        if r.get("event_id"):
            par_event.setdefault(cle_ev, []).append(r)

    precalcul_par_event = {
        f"{str(it.get('sport') or '').upper()}_{str(it.get('event_id') or '').strip()}": it
        for it in (candidats_precalcul or [])
        if it.get("event_id")
    }

    enrichis = []
    for m in live_items:
        copie = dict(m)
        sport_u = str(copie.get("sport") or "").upper()
        eid = str(copie.get("event_id") or "").strip()
        cle_ev = f"{sport_u}_{eid}"

        base = precalcul_par_event.get(cle_ev)
        if not base and " vs " in copie.get("match", ""):
            eq_h, eq_a = [x.strip() for x in copie["match"].split(" vs ", 1)]
            for it in (candidats_precalcul or []):
                if str(it.get("sport") or "").upper() == sport_u and " vs " in it.get("match", ""):
                    rh, ra = [x.strip() for x in it["match"].split(" vs ", 1)]
                    if _memes_equipes(eq_h, rh) and _memes_equipes(eq_a, ra):
                        base = it
                        break

        if base and not copie.get("score_exact"):
            copie["score_exact"] = base.get("score_exact")
            copie["score_exact_proba"] = base.get("score_exact_proba")

        hist_match = par_event.get(cle_ev) or []
        if not hist_match and " vs " in copie.get("match", ""):
            eq_h, eq_a = [x.strip() for x in copie["match"].split(" vs ", 1)]
            for r in lignes_hist:
                if str(r.get("sport") or "").upper() == sport_u and " vs " in r.get("match", ""):
                    rh, ra = [x.strip() for x in r["match"].split(" vs ", 1)]
                    if _memes_equipes(eq_h, rh) and _memes_equipes(eq_a, ra):
                        hist_match = [r]
                        break

        if hist_match:
            dernier = hist_match[-1]
            copie["pronostic_predi"] = dernier.get("choix") or copie.get("choix")
            copie["pronostic_proba"] = dernier.get("proba") or copie.get("proba")
            copie["pronostic_cote"] = dernier.get("cote") or copie.get("cote")
            copie["pronostic_statut"] = dernier.get("statut") or "EN_ATTENTE"
            copie["pronostic_source"] = "Enregistré"
        else:
            ref = base or copie
            copie["pronostic_predi"] = ref.get("choix")
            copie["pronostic_proba"] = ref.get("proba")
            copie["pronostic_cote"] = ref.get("cote")
            copie["pronostic_statut"] = "EN_ATTENTE"
            copie["pronostic_source"] = "Calculé"

        enrichis.append(copie)

    return enrichis


def _construire_favoris_suivis(tous_items_a_venir, live_enrichis, clos_globaux, ancien_rapport=None):
    with _FAV_LOCK:
        fav_map = {k: dict(v) for k, v in _FAVORIS_ACTIFS.items()}

    if not fav_map:
        return []

    index_actifs = {}
    for it in (tous_items_a_venir or []) + (live_enrichis or []):
        cle = str(it.get("cle_favori") or "").strip() or _construire_cle_favori(
            it.get("sport"), it.get("event_id"), it.get("match"), it.get("date_match")
        )
        index_actifs[cle] = it

    anciens_suivis_map = {}
    for prev in (ancien_rapport or {}).get("favoris_suivis") or []:
        cle_p = str(prev.get("cle_favori") or "").strip() or _construire_cle_favori(
            prev.get("sport"), prev.get("event_id"), prev.get("match"), prev.get("date_match")
        )
        anciens_suivis_map[cle_p] = dict(prev)

    suivis = []
    for cle, meta in fav_map.items():
        if cle in index_actifs:
            courant = dict(index_actifs[cle])
            courant["cle_favori"] = cle
            courant["est_favori"] = True
            courant.setdefault("etat_match", "EN_COURS" if courant.get("en_cours") else "A_VENIR")
            courant["ajoute_le"] = meta.get("ajoute_le") or ""
            suivis.append(courant)
            continue

        ev_clos = clos_globaux.get(cle) or clos_globaux.get(str(meta.get("event_id") or "").strip())
        if ev_clos:
            sport_u = str(ev_clos.get("_sport") or meta.get("sport") or "FOOT").upper()
            scores = _extraire_scores_evenement(ev_clos)
            h_info, a_info = _normaliser_home_away(ev_clos)
            h_nom = h_info.get("short_name") or h_info.get("name") or ""
            a_nom = a_info.get("short_name") or a_info.get("name") or ""

            if len(scores) >= 2:
                s0 = scores[0]["score"] if scores[0]["score"] is not None else 0
                s1 = scores[1]["score"] if scores[1]["score"] is not None else 0
                if sport_u == "FOOT":
                    score_final = f"{s0} - {s1}"
                else:
                    n0 = scores[0]["name"] or h_nom or "Joueur 1"
                    n1 = scores[1]["name"] or a_nom or "Joueur 2"
                    score_final = f"{n0} {s0} - {s1} {n1}"
            else:
                score_final = "Terminé"

            match_nom = meta.get("match") or (f"{h_nom} vs {a_nom}" if h_nom and a_nom else str(ev_clos.get("_match_label") or "Match terminé"))
            date_m = meta.get("date_match") or _formater_date_locale(ev_clos.get("start_time") or ev_clos.get("date"))
            ts_m = float(meta.get("ts_match") or _timestamp_match(ev_clos.get("start_time") or ev_clos.get("date")))

            ligne_eval = {
                "sport": sport_u,
                "choix": meta.get("choix") or "",
            }
            statut_prono = _resoudre_statut_depuis_evenement(ligne_eval, ev_clos) or "TERMINE"

            suivis.append({
                "cle_favori": cle,
                "est_favori": True,
                "sport": sport_u,
                "event_id": str(meta.get("event_id") or ev_clos.get("id") or ""),
                "competition": ev_clos.get("_competition") or meta.get("competition") or sport_u,
                "match": match_nom,
                "date_match": date_m,
                "ts_match": ts_m,
                "choix": meta.get("choix") or "Non défini",
                "proba": meta.get("proba"),
                "cote": meta.get("cote"),
                "mise_u": meta.get("mise_u"),
                "confiance": meta.get("confiance") or "MOYENNE",
                "confiance_score": meta.get("confiance_score") or 50.0,
                "etat_match": "TERMINE",
                "en_cours": False,
                "score_final": score_final,
                "statut_prono": statut_prono,
                "detail": meta.get("detail") or f"Match terminé ({sport_u})",
                "ajoute_le": meta.get("ajoute_le") or "",
            })
            continue

        if cle in anciens_suivis_map:
            prev_item = dict(anciens_suivis_map[cle])
            prev_item["cle_favori"] = cle
            prev_item["est_favori"] = True
            prev_item.setdefault("ajoute_le", meta.get("ajoute_le") or "")
            suivis.append(prev_item)
        else:
            fallback_item = dict(meta)
            fallback_item["cle_favori"] = cle
            fallback_item["est_favori"] = True
            fallback_item.setdefault("etat_match", "A_VENIR")
            fallback_item.setdefault("ts_match", 9_999_999_999.0)
            fallback_item.setdefault("confiance", "MOYENNE")
            fallback_item.setdefault("confiance_score", 50.0)
            suivis.append(fallback_item)

    ordre_etat = {"EN_COURS": 0, "A_VENIR": 1, "TERMINE": 2}
    suivis.sort(key=lambda x: (
        ordre_etat.get(str(x.get("etat_match") or "A_VENIR").upper(), 1),
        _cle_cle_chronologique(x),
    ))
    return suivis


def _annoter_favoris_dans_rapport(rapport):
    cles_fav = _obtenir_cles_favoris()
    r = dict(rapport)
    for liste_key in ("top_sur", "foot", "tennis", "en_cours_matchs"):
        nouvelle_liste = []
        for it in r.get(liste_key) or []:
            copie = dict(it)
            cle = str(copie.get("cle_favori") or "").strip() or _construire_cle_favori(
                copie.get("sport"), copie.get("event_id"), copie.get("match"), copie.get("date_match")
            )
            copie["cle_favori"] = cle
            copie["est_favori"] = cle in cles_fav
            nouvelle_liste.append(copie)
        r[liste_key] = nouvelle_liste

    r["favoris_suivis"] = [
        dict(it, est_favori=True)
        for it in (r.get("favoris_suivis") or [])
        if (it.get("cle_favori") or _construire_cle_favori(it.get("sport"), it.get("event_id"), it.get("match"), it.get("date_match"))) in cles_fav
    ]
    return r


def _cible_concernee_par_choix(it, cible):
    c_norm = _normaliser_nom_equipe(cible)
    if not c_norm:
        return True
    choix_l = _normaliser_nom_equipe(it.get("choix") or "")
    if "match nul" in choix_l or "+2 5" in choix_l or "-2 5" in choix_l:
        return True
    if choix_l.startswith("victoire "):
        cible_choix = choix_l.replace("victoire ", "", 1).strip()
        return _memes_equipes(cible_choix, cible)
    equipes = it.get("equipes") or []
    return any(_memes_equipes(eq, cible) for eq in equipes)


def _enrichir_sante_items(items_cibles, tous_les_items=None):
    if not items_cibles:
        return {}

    equipes_a_scanner = set()
    for it in items_cibles[:10]:
        for nom in it.get("equipes", []):
            if nom:
                equipes_a_scanner.add(nom)

    alertes_globales = {}
    gravites_par_cible = {}

    def scanner(nom):
        try:
            return nom, scanner_sante_cible(nom, 2)
        except Exception:
            return nom, []

    with ThreadPoolExecutor(max_workers=4) as ex:
        for nom, titres in ex.map(scanner, equipes_a_scanner):
            pertinents = _filtrer_titres_pertinents(titres, nom)
            if pertinents:
                alertes_globales[nom] = pertinents
                gravites_par_cible[nom] = _evaluer_gravite_titres(pertinents, cible=nom)

    liste_a_maj = list(items_cibles)
    if tous_les_items:
        ids_vus = {id(x) for x in liste_a_maj}
        for x in tous_les_items:
            if id(x) not in ids_vus:
                liste_a_maj.append(x)
                ids_vus.add(id(x))

    for it in liste_a_maj:
        grav = 0.0
        for n in it.get("equipes", []):
            g_cible = gravites_par_cible.get(n, 0.0)
            if g_cible > 0.0 and _cible_concernee_par_choix(it, n):
                grav += g_cible
        grav = min(grav, 2.0)

        if grav >= 0.75:
            penalite_proba = min(grav * 0.015, 0.04)
            it["proba"] = round(max(0.05, it["proba"] / 100 - penalite_proba) * 100, 1)
            it["prudence"] = "MODERE" if it["proba"] >= 62 else "PRUDENT (alerte)"
        est_nul = bool(it.get("est_nul")) or str(it.get("choix") or "").strip().lower() == "match nul"
        est_cote_reelle = str(it.get("cote_source") or "Estimée") != "Estimée"
        if not est_cote_reelle:
            it["cote"] = _calculer_cote(it["proba"])

        conf_score, conf_niveau = _calculer_indice_confiance(
            it.get("proba", 50.0),
            bool(it.get("fiable", True)),
            grav,
            est_nul=est_nul,
        )
        cote_min, mise_u, est_value_bet, ecart_value_pct = _calculer_cote_min_et_mise(
            it.get("proba", 50.0),
            conf_score,
            bool(it.get("fiable", True)),
            est_nul=est_nul,
            cote_reelle=it.get("cote"),
            est_cote_reelle=est_cote_reelle,
        )
        it["cote_min"] = cote_min
        it["mise_u"] = mise_u
        it["est_value_bet"] = est_value_bet
        it["ecart_value_pct"] = ecart_value_pct
        it["confiance"] = conf_niveau
        it["confiance_score"] = conf_score

    return alertes_globales


CHAMPS_HISTORIQUE = [
    "date_analyse", "sport", "event_id", "match", "date_match",
    "choix", "proba", "cote", "prudence", "mise_u", "statut",
]


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
            cote_val = None
            raw_cote = str(r.get("cote") or "").strip().replace(",", ".")
            if raw_cote:
                try:
                    c_num = float(raw_cote)
                    if c_num > 1.0:
                        cote_val = c_num
                except Exception:
                    cote_val = None
            if cote_val is None:
                try:
                    p_pct = float(str(r.get("proba") or "").strip().replace(",", "."))
                    if p_pct > 0:
                        cote_val = _calculer_cote(p_pct)
                except Exception:
                    cote_val = None
            gain_net = m * (cote_val - 1.0) if cote_val is not None else m
            pnl += gain_net
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
                h_nm = _normaliser_nom_equipe(item["name"])
            elif item["qualifier"] == "away":
                a_sc = item["score"]
                a_nm = _normaliser_nom_equipe(item["name"])
        if len(scores) >= 2 and (h_sc is None or a_sc is None):
            h_sc = scores[0]["score"]
            h_nm = _normaliser_nom_equipe(scores[0]["name"])
            a_sc = scores[1]["score"]
            a_nm = _normaliser_nom_equipe(scores[1]["name"])
        if h_sc is None or a_sc is None:
            return None
        if "+2.5" in choix_l:
            return "GAGNE" if (h_sc + a_sc) > 2 else "PERDU"
        if "-2.5" in choix_l:
            return "GAGNE" if (h_sc + a_sc) < 3 else "PERDU"
        if choix_l == "match nul":
            return "GAGNE" if h_sc == a_sc else "PERDU"
        if choix_l.startswith("victoire "):
            cible = _normaliser_nom_equipe(choix_l.replace("victoire ", "", 1))
            if cible and h_nm and (cible == h_nm or cible in h_nm or h_nm in cible):
                return "GAGNE" if h_sc > a_sc else "PERDU"
            if cible and a_nm and (cible == a_nm or cible in a_nm or a_nm in cible):
                return "GAGNE" if a_sc > h_sc else "PERDU"
        return None

    if sport == "TENNIS" and choix_l.startswith("victoire "):
        cible = choix_l.replace("victoire ", "", 1).strip().lower()
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
    champs = CHAMPS_HISTORIQUE
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
                eid = str(r.get("event_id") or "").strip()
                cle_fav = _construire_cle_favori(r.get("sport"), eid, r.get("match"), r.get("date_match"))
                ev_clos = clos_map.get(cle_fav) or clos_map.get(eid)
                nouveau_statut = _resoudre_statut_depuis_evenement(r, ev_clos)
                if nouveau_statut in ("GAGNE", "PERDU"):
                    r["statut"] = nouveau_statut

        now_str = datetime.now(TZ_PARIS).strftime("%Y-%m-%d %H:%M")
        for p in items:
            cle = f"{p['sport']}_{p['event_id']}_{p['choix']}"
            if cle not in cles:
                cote_p = p.get("cote")
                lignes.append({
                    "date_analyse": now_str,
                    "sport": str(p["sport"]),
                    "event_id": str(p["event_id"]),
                    "match": str(p["match"]),
                    "date_match": str(p["date_match"]),
                    "choix": str(p["choix"]),
                    "proba": str(p["proba"]),
                    "cote": str(cote_p) if cote_p is not None else "",
                    "prudence": str(p["prudence"]),
                    "mise_u": str(p.get("mise_u", CFG.mise_unite_defaut)),
                    "statut": "EN_ATTENTE",
                })
                cles.add(cle)

        tmp_csv = f"{FICHIER_HISTORIQUE}.tmp"
        try:
            os.makedirs(os.path.dirname(FICHIER_HISTORIQUE) or ".", exist_ok=True)
            with open(tmp_csv, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=champs)
                writer.writeheader()
                for r in lignes:
                    writer.writerow({k: r.get(k, "") for k in champs})
            os.replace(tmp_csv, FICHIER_HISTORIQUE)
        except Exception as e:
            logger.warning("Erreur écriture historique CSV: %s", e)
            try:
                if os.path.exists(tmp_csv):
                    os.remove(tmp_csv)
            except Exception:
                pass

        return _calculer_bilan(lignes)


def definir_statut_historique(sport, event_id, choix, statut):
    statut_norm = str(statut or "").strip().upper()
    if statut_norm not in ("GAGNE", "PERDU", "EN_ATTENTE"):
        return None
    champs = CHAMPS_HISTORIQUE
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

        tmp_csv = f"{FICHIER_HISTORIQUE}.tmp"
        try:
            with open(tmp_csv, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=champs)
                writer.writeheader()
                for r in lignes:
                    writer.writerow({k: r.get(k, "") for k in champs})
            os.replace(tmp_csv, FICHIER_HISTORIQUE)
        except Exception as e:
            logger.warning("Erreur sauvegarde statut historique: %s", e)
            try:
                if os.path.exists(tmp_csv):
                    os.remove(tmp_csv)
            except Exception:
                pass
            return None

        return _calculer_bilan(lignes)


def executer_analyse():
    global _DERNIER_RAPPORT, _ANALYSE_EN_COURS
    with _STATE_LOCK:
        if _ANALYSE_EN_COURS:
            return _DERNIER_RAPPORT
        _ANALYSE_EN_COURS = True
        ancien_rapport = dict(_DERNIER_RAPPORT)

    try:
        logger.info("Analyse en cours (fenetre: %sh)...", CFG.fenetre_heures)
        clos_globaux = {}
        live_globaux = []

        try:
            foot_res, live_f, alertes_f, stats_comp, apercu_bruts, clos_foot = analyser_foot_competitions(
                COMPETITIONS_CIBLES, CFG.fenetre_heures
            )
            clos_globaux.update(clos_foot)
            live_globaux.extend(live_f)
        except Exception as e:
            logger.error("Erreur Foot: %s", e)
            foot_res, alertes_f, stats_comp, apercu_bruts = [], {}, {}, f"Erreur Foot: {e}"

        try:
            tennis_res, live_t, alertes_t, clos_tennis = analyser_tennis(CFG.fenetre_heures)
            clos_globaux.update(clos_tennis)
            live_globaux.extend(live_t)
        except Exception as e:
            logger.error("Erreur Tennis: %s", e)
            tennis_res, alertes_t = [], {}

        cotes_reel_map = {}
        try:
            cotes_reel_map = charger_cotes_moyennes_foot(COMPETITIONS_CIBLES)
        except Exception as e:
            logger.warning("Erreur récupération cotes moyennes: %s", e)

        for it in foot_res:
            cote_trouvee = None
            nb_books = 0
            if cotes_reel_map and " vs " in it.get("match", ""):
                eq_h, eq_a = [x.strip() for x in it["match"].split(" vs ", 1)]
                for (lbl, nh, na), pack in cotes_reel_map.items():
                    if lbl == it.get("competition") and _memes_equipes(eq_h, nh) and _memes_equipes(eq_a, na):
                        choix_l = str(it.get("choix") or "").strip().lower()
                        if choix_l == "match nul":
                            cote_trouvee, nb_books = pack["draw"]
                        elif choix_l.startswith("victoire "):
                            cible = choix_l.replace("victoire ", "", 1).strip()
                            if _memes_equipes(cible, eq_h):
                                cote_trouvee, nb_books = pack["home"]
                            elif _memes_equipes(cible, eq_a):
                                cote_trouvee, nb_books = pack["away"]
                        break

            est_cote_reelle = bool(cote_trouvee and nb_books > 0)
            if est_cote_reelle:
                it["cote"] = cote_trouvee
                it["cote_source"] = f"Moy. {nb_books} bookmakers"
            else:
                it["cote"] = _calculer_cote(it["proba"])
                it["cote_source"] = "Estimée"

            est_nul = bool(it.get("est_nul")) or str(it.get("choix") or "").strip().lower() == "match nul"
            cote_min, mise_u, est_value_bet, ecart_value_pct = _calculer_cote_min_et_mise(
                it.get("proba", 50.0),
                it.get("confiance_score", it.get("proba", 50.0)),
                bool(it.get("fiable", True)),
                est_nul=est_nul,
                cote_reelle=it.get("cote"),
                est_cote_reelle=est_cote_reelle,
            )
            it["cote_min"] = cote_min
            it["mise_u"] = mise_u
            it["est_value_bet"] = est_value_bet
            it["ecart_value_pct"] = ecart_value_pct

        for it in tennis_res:
            it["cote"] = _calculer_cote(it["proba"])
            it["cote_source"] = "Estimée"
            cote_min, mise_u, est_value_bet, ecart_value_pct = _calculer_cote_min_et_mise(
                it.get("proba", 50.0),
                it.get("confiance_score", it.get("proba", 50.0)),
                bool(it.get("fiable", True)),
                est_nul=False,
                cote_reelle=it.get("cote"),
                est_cote_reelle=False,
            )
            it["cote_min"] = cote_min
            it["mise_u"] = mise_u
            it["est_value_bet"] = est_value_bet
            it["ecart_value_pct"] = ecart_value_pct

        tous_items = sorted(foot_res + tennis_res, key=_cle_cle_chronologique)

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

        foot_sorted = sorted(foot_res, key=_cle_cle_chronologique)
        tennis_sorted = sorted(tennis_res, key=_cle_cle_chronologique)

        top_candidats = sorted(
            foot_sorted + tennis_sorted,
            key=lambda x: (
                1 if x.get("est_value_bet") else 0,
                x.get("confiance_score", x["proba"]),
                x["proba"],
            ),
            reverse=True,
        )[:10]
        top_sur = sorted(top_candidats, key=_cle_cle_chronologique)

        try:
            bilan = mettre_a_jour_historique(
                [x for x in top_candidats if x.get("fiable", True)],
                evenements_termines=clos_globaux,
            )
        except Exception as e:
            logger.error("Erreur Historique: %s", e)
            bilan = dict(ancien_rapport.get("bilan") or {})

        live_enrichis = sorted(
            _enrichir_matchs_live_avec_pronostics(live_globaux, tous_items),
            key=_cle_cle_chronologique,
        )

        favoris_suivis = _construire_favoris_suivis(
            tous_items, live_enrichis, clos_globaux, ancien_rapport=ancien_rapport
        )

        synthese = "\n".join(
            f"• [{x['sport']} | Confiance {x.get('confiance', 'MOYENNE')} | "
            f"Cote {x.get('cote', '-')} ({x.get('cote_source', 'Estimée')})"
            f"{' | VALUE +' + str(x.get('ecart_value_pct')) + '%' if x.get('est_value_bet') else ''}] "
            f"{x['match']} ({x['date_match']}) -> {x['choix']} ({x['proba']}%)"
            for x in top_sur[:8]
        ) or f"Aucun match à venir dans les prochaines {CFG.fenetre_heures}h."

        nouveau_rapport = _annoter_favoris_dans_rapport({
            "mis_a_jour": datetime.now(TZ_PARIS).strftime("%Y-%m-%d %H:%M %Z"),
            "stats_comp": stats_comp,
            "apercu_bruts": apercu_bruts,
            "en_cours_matchs": live_enrichis,
            "top_sur": top_sur,
            "foot": foot_sorted,
            "tennis": tennis_sorted,
            "favoris_suivis": favoris_suivis,
            "alertes": alertes_combines,
            "bilan": bilan,
            "synthese": synthese,
        })

        with _STATE_LOCK:
            _DERNIER_RAPPORT = nouveau_rapport

        _sauvegarder_rapport_disque(nouveau_rapport)
        logger.info("Analyse terminée et rapport mis à jour.")
        return nouveau_rapport
    finally:
        with _STATE_LOCK:
            _ANALYSE_EN_COURS = False


app = Flask(__name__, static_folder="statique", static_url_path="/statique")


@app.route("/icon.svg")
def icon_svg():
    resp = Response(ICONE_SVG_MARKUP, mimetype="image/svg+xml")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/icon-192.png")
def icon_192():
    resp = Response(_PNG_192, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/icon-512.png")
def icon_512():
    resp = Response(_PNG_512, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/manifest.json")
def web_manifest():
    manifest = {
        "name": "BetPulse Pro",
        "short_name": "BetPulse Pro",
        "description": "Pronostics sportifs Football & Tennis, cotes et suivi en direct",
        "start_url": "/app",
        "scope": "/",
        "display": "standalone",
        "background_color": "#0b1324",
        "theme_color": "#0b1324",
        "orientation": "portrait-primary",
        "icons": [
            {
                "src": "/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": "/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable",
            },
            {
                "src": "/icon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any",
            },
        ],
    }
    resp = make_response(jsonify(manifest), 200)
    resp.headers["Content-Type"] = "application/manifest+json; charset=utf-8"
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


_HTML_HOME = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BetPulse Pro — Accueil</title>""" + FAVICON_HEAD_TAGS + """
<style>
  :root {
    --bg: #0f172a;
    --text: #e2e8f0;
    --muted: #94a3b8;
    --accent: #38bdf8;
    --green: #10b981;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0; min-height: 100vh; overflow-x: hidden;
    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
    background: radial-gradient(circle at 50% 30%, #1e293b 0%, var(--bg) 70%);
    color: var(--text);
  }
  .field {
    position: fixed; inset: 0; overflow: hidden; pointer-events: none;
    background-image:
      linear-gradient(rgba(56,189,248,.04) 1px, transparent 1px),
      linear-gradient(90deg, rgba(56,189,248,.04) 1px, transparent 1px);
    background-size: 60px 60px;
  }
  .ball {
    position: absolute; top: 0; left: 0;
    will-change: transform;
    filter: drop-shadow(0 6px 14px rgba(0,0,0,.45));
  }
  .football {
    width: 64px; height: 64px; border-radius: 50%;
    background:
      radial-gradient(circle at 50% 50%, #111 17%, transparent 19%),
      radial-gradient(circle at 22% 22%, #111 11%, transparent 13%),
      radial-gradient(circle at 78% 22%, #111 11%, transparent 13%),
      radial-gradient(circle at 15% 55%, #111 11%, transparent 13%),
      radial-gradient(circle at 85% 55%, #111 11%, transparent 13%),
      radial-gradient(circle at 30% 85%, #111 11%, transparent 13%),
      radial-gradient(circle at 70% 85%, #111 11%, transparent 13%),
      radial-gradient(circle at 40% 40%, #fff 0%, #eee 55%, #b0b0b0 100%);
    box-shadow: inset -6px -6px 14px rgba(0,0,0,.28), inset 4px 4px 8px rgba(255,255,255,.4);
  }
  .tennis {
    width: 54px; height: 54px;
  }
  .tennis svg {
    width: 100%; height: 100%;
  }
  .page-wrap {
    position: relative; z-index: 10;
    min-height: 100vh;
    display: flex; align-items: center; justify-content: center;
    padding: 20px;
  }
  .center-card {
    text-align: center;
    padding: 28px 32px; border-radius: 20px;
    background: rgba(30, 41, 59, 0.86);
    border: 1px solid rgba(56,189,248,.25);
    backdrop-filter: blur(10px);
    -webkit-backdrop-filter: blur(10px);
    box-shadow: 0 20px 50px rgba(0,0,0,.5);
    max-width: 520px; width: 100%;
    animation: fadeIn .5s ease-out;
  }
  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(10px); }
    to   { opacity: 1; transform: translateY(0); }
  }
  .logo-badge {
    display: inline-flex; align-items: center; justify-content: center;
    width: 74px; height: 74px; border-radius: 20px;
    overflow: hidden;
    margin-bottom: 14px;
    border: 1px solid rgba(56, 189, 248, 0.45);
    animation: pulse 2.2s cubic-bezier(.4,0,.6,1) infinite;
  }
  .logo-badge img {
    width: 100%; height: 100%; object-fit: cover; display: block;
  }
  @keyframes pulse {
    0%   { box-shadow: 0 0 0 0 rgba(56,189,248,.55); }
    70%  { box-shadow: 0 0 0 20px rgba(56,189,248,0); }
    100% { box-shadow: 0 0 0 0 rgba(56,189,248,0); }
  }
  .title { font-size: 1.85rem; font-weight: 800; margin: 0 0 6px; }
  .title span { color: var(--accent); }
  .tagline { color: var(--muted); font-size: .9rem; margin-bottom: 16px; }
  .explain-box {
    text-align: left;
    background: rgba(15, 23, 42, 0.72);
    border: 1px solid rgba(148, 163, 184, 0.2);
    border-radius: 12px;
    padding: 13px 15px;
    margin-bottom: 18px;
    font-size: .84rem;
    line-height: 1.48;
    color: #cbd5e1;
  }
  .explain-title {
    font-weight: 700;
    color: var(--accent);
    margin-bottom: 6px;
    font-size: .86rem;
  }
  .comp-list {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 8px;
  }
  .comp-chip {
    background: rgba(56, 189, 248, 0.12);
    border: 1px solid rgba(56, 189, 248, 0.28);
    color: #e2e8f0;
    padding: 3px 9px;
    border-radius: 999px;
    font-size: .76rem;
    font-weight: 600;
  }
  .comp-chip.tennis-chip {
    background: rgba(16, 185, 129, 0.14);
    border-color: rgba(16, 185, 129, 0.35);
    color: #a7f3d0;
  }
  .progress {
    height: 6px; border-radius: 999px; overflow: hidden;
    background: rgba(148,163,184,.2); margin-bottom: 14px;
  }
  .progress-fill {
    height: 100%; width: 35%; border-radius: 999px;
    background: linear-gradient(90deg, var(--accent), var(--green));
    animation: slide 1.8s ease-in-out infinite;
    box-shadow: 0 0 12px rgba(56,189,248,.85);
  }
  .progress-fill.ready {
    width: 100%;
    animation: none;
    background: linear-gradient(90deg, var(--green), var(--accent));
  }
  @keyframes slide {
    0%   { transform: translateX(-110%); }
    100% { transform: translateX(380%); }
  }
  .enter-wrap {
    margin: 4px 0 14px;
  }
  .btn-enter {
    display: inline-flex; align-items: center; justify-content: center; gap: 8px;
    width: 100%;
    padding: 13px 22px;
    border-radius: 12px;
    background: linear-gradient(135deg, #38bdf8 0%, #10b981 100%);
    color: #0b1220;
    font-weight: 900;
    font-size: 1rem;
    letter-spacing: 0.04em;
    text-decoration: none;
    box-shadow: 0 10px 24px rgba(16, 185, 129, 0.28);
    transition: transform 0.14s ease, box-shadow 0.14s ease, opacity 0.15s ease;
  }
  .btn-enter:hover {
    transform: translateY(-2px);
    box-shadow: 0 14px 28px rgba(56, 189, 248, 0.38);
  }
  .btn-enter.waiting {
    background: rgba(56, 189, 248, 0.16);
    color: var(--accent);
    border: 1px solid rgba(56, 189, 248, 0.35);
    box-shadow: none;
  }
  .status { font-size: .85rem; color: var(--muted); }
  .status.dots::after {
    content: ''; display: inline-block; width: 1.3em;
    animation: dots 1.5s steps(4,end) infinite;
  }
  @keyframes dots {
    0% { content: ''; }
    25% { content: '.'; }
    50% { content: '..'; }
    75%, 100% { content: '...'; }
  }
  .meta { margin-top: 10px; font-size: .75rem; color: #64748b; }

  @keyframes fly1 {
    0%   { transform: translate(8vw, 18vh) rotate(0deg); }
    20%  { transform: translate(25vw, 72vh) rotate(180deg); }
    40%  { transform: translate(60vw, 15vh) rotate(360deg); }
    60%  { transform: translate(85vw, 70vh) rotate(540deg); }
    80%  { transform: translate(45vw, 85vh) rotate(720deg); }
    100% { transform: translate(8vw, 18vh) rotate(900deg); }
  }
  @keyframes fly2 {
    0%   { transform: translate(88vw, 12vh) rotate(0deg); }
    25%  { transform: translate(60vw, 78vh) rotate(180deg); }
    50%  { transform: translate(20vw, 35vh) rotate(360deg); }
    75%  { transform: translate(70vw, 60vh) rotate(540deg); }
    100% { transform: translate(88vw, 12vh) rotate(720deg); }
  }
  @keyframes fly3 {
    0%   { transform: translate(50vw, 88vh) rotate(0deg); }
    20%  { transform: translate(80vw, 30vh) rotate(144deg); }
    40%  { transform: translate(30vw, 10vh) rotate(288deg); }
    60%  { transform: translate(10vw, 60vh) rotate(432deg); }
    80%  { transform: translate(70vw, 80vh) rotate(576deg); }
    100% { transform: translate(50vw, 88vh) rotate(720deg); }
  }
  @keyframes fly4 {
    0%   { transform: translate(15vw, 55vh) rotate(0deg); }
    25%  { transform: translate(50vw, 20vh) rotate(180deg); }
    50%  { transform: translate(90vw, 65vh) rotate(360deg); }
    75%  { transform: translate(40vw, 80vh) rotate(540deg); }
    100% { transform: translate(15vw, 55vh) rotate(720deg); }
  }
  @keyframes fly5 {
    0%   { transform: translate(70vw, 15vh) rotate(0deg); }
    20%  { transform: translate(20vw, 45vh) rotate(180deg); }
    40%  { transform: translate(60vw, 80vh) rotate(360deg); }
    60%  { transform: translate(90vw, 35vh) rotate(540deg); }
    80%  { transform: translate(30vw, 70vh) rotate(720deg); }
    100% { transform: translate(70vw, 15vh) rotate(900deg); }
  }

  @media (max-width: 480px) {
    .football { width: 48px; height: 48px; }
    .tennis { width: 42px; height: 42px; }
    .center-card { padding: 22px 18px; }
    .title { font-size: 1.6rem; }
  }
</style>
</head>
<body>
<div class="field">
  <div class="ball football" style="animation: fly1 11s linear infinite; animation-delay: 0s;"></div>
  <div class="ball football" style="animation: fly3 13s ease-in-out infinite; animation-delay: -4s;"></div>
  <div class="ball football" style="animation: fly5 15s linear infinite; animation-delay: -8s;"></div>

  <div class="ball tennis" style="animation: fly2 10s ease-in-out infinite; animation-delay: -2s;">
    <svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <radialGradient id="tg1" cx="35%" cy="35%">
          <stop offset="0%" stop-color="#f0f56a"/>
          <stop offset="100%" stop-color="#b8c92b"/>
        </radialGradient>
      </defs>
      <circle cx="50" cy="50" r="48" fill="url(#tg1)"/>
      <path d="M 12,14 Q 50,50 12,86" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/>
      <path d="M 88,14 Q 50,50 88,86" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/>
    </svg>
  </div>
  <div class="ball tennis" style="animation: fly4 12s linear infinite; animation-delay: -6s;">
    <svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <radialGradient id="tg2" cx="35%" cy="35%">
          <stop offset="0%" stop-color="#f0f56a"/>
          <stop offset="100%" stop-color="#b8c92b"/>
        </radialGradient>
      </defs>
      <circle cx="50" cy="50" r="48" fill="url(#tg2)"/>
      <path d="M 12,14 Q 50,50 12,86" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/>
      <path d="M 88,14 Q 50,50 88,86" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/>
    </svg>
  </div>
</div>

<div class="page-wrap">
  <div class="center-card">
    <div class="logo-badge"><img src="/icon.svg" alt="BetPulse Pro"></div>
    <h1 class="title">BetPulse <span>Pro</span></h1>
    <div class="tagline">Préparation des pronostics &amp; suivi en direct</div>

    <div class="explain-box">
      <div class="explain-title">Comment fonctionne BetPulse Pro ?</div>
      <div>
        L'outil analyse automatiquement les matchs en cours et à venir sur une fenêtre de {{ fenetre_heures }}h :
        forme récente, estimation statistique des buts et des scores exacts en football, classements et surfaces en tennis,
        ainsi que les alertes blessures ou forfaits récents.
      </div>
      <div class="comp-list">
        <span class="comp-chip">⚽ Ligue 1</span>
        <span class="comp-chip">⚽ Liga</span>
        <span class="comp-chip">⚽ Liga Portugaise</span>
        <span class="comp-chip">⚽ Bundesliga</span>
        <span class="comp-chip">⚽ Serie A</span>
        <span class="comp-chip">⚽ Premier League</span>
        <span class="comp-chip">⚽ Ligue des Champions</span>
        <span class="comp-chip">⚽ Europa League</span>
        <span class="comp-chip tennis-chip">🎾 ATP / WTA</span>
      </div>
    </div>

    <div class="progress"><div class="progress-fill {% if pret %}ready{% endif %}" id="progress-fill"></div></div>

    <div class="enter-wrap">
      <a href="/app" class="btn-enter {% if not pret %}waiting{% endif %}" id="btn-entrer">
        ENTRER →
      </a>
    </div>

    <div class="status {% if not pret %}dots{% endif %}" id="status">
      {% if pret %}Données prêtes — cliquez sur ENTRER pour accéder à l'application{% elif en_cours %}Analyse en cours{% else %}Initialisation des données{% endif %}
    </div>
    <div class="meta" id="meta">{% if mis_a_jour %}Dernière analyse : {{ mis_a_jour }}{% endif %}</div>
  </div>
</div>

<script>
(function(){
  var statusEl   = document.getElementById('status');
  var metaEl     = document.getElementById('meta');
  var btnEntrer  = document.getElementById('btn-entrer');
  var progFill   = document.getElementById('progress-fill');
  var POLL_MS    = 1500;

  function marquerPret(maj) {
    if (btnEntrer) {
      btnEntrer.classList.remove('waiting');
    }
    if (progFill) {
      progFill.classList.add('ready');
    }
    if (statusEl) {
      statusEl.classList.remove('dots');
      statusEl.textContent = "Données prêtes — cliquez sur ENTRER pour accéder à l'application";
    }
    if (metaEl && maj) {
      metaEl.textContent = "Dernière analyse : " + maj;
    }
  }

  async function check() {
    try {
      var r = await fetch("/api/status", { cache: "no-store" });
      if (r.ok) {
        var d = await r.json();
        if (d.mis_a_jour) {
          if (metaEl) metaEl.textContent = "Dernière analyse : " + d.mis_a_jour;
        }
        if (d.ready) {
          marquerPret(d.mis_a_jour);
          return;
        }
      }
    } catch (e) {}
    setTimeout(check, POLL_MS);
  }

  if (!({{ "true" if pret else "false" }})) {
    setTimeout(check, 400);
  }
})();
</script>
</body>
</html>
"""

_HTML_LOADING = """<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BetPulse Pro — Chargement…</title>""" + FAVICON_HEAD_TAGS + """
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
  <a href="/">← Retour à l'accueil</a>
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
  <title>BetPulse Pro — {% if page_favoris %}Matchs Favoris{% else %}Matchs &amp; Pronostics{% endif %}</title>""" + FAVICON_HEAD_TAGS + """
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
      --gold: #fbbf24;
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
    .header-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
    .brand-link { display: inline-flex; align-items: center; gap: 12px; text-decoration: none; color: var(--text); }
    .brand-badge {
      width: 42px; height: 42px; border-radius: 12px; overflow: hidden;
      border: 1px solid rgba(56, 189, 248, 0.45);
      display: inline-flex; align-items: center; justify-content: center;
      flex-shrink: 0;
    }
    .brand-badge img { width: 100%; height: 100%; object-fit: cover; display: block; }
    .meta { color: var(--muted); font-size: 0.88em; }
    button, .btn, select {
      background: var(--accent); color: #0b1220; border: none; padding: 9px 15px;
      border-radius: 9px; cursor: pointer; font-weight: 700; text-decoration: none;
      display: inline-flex; align-items: center; gap: 6px; font-size: 0.88em;
      transition: transform 0.12s ease, opacity 0.15s ease, background 0.15s ease, border-color 0.15s ease;
    }
    select { background: #0b1220; color: var(--text); border: 1px solid var(--border); font-weight: 600; }
    .btn-cat { background: var(--card); color: var(--text); border: 1px solid var(--border); font-weight: 600; }
    .btn-cat.active { background: var(--accent); color: #0b1220; border-color: var(--accent); font-weight: 700; }
    .btn-ghost {
      background: rgba(15, 23, 42, 0.85); color: var(--text); border: 1px solid var(--border);
      font-weight: 600; padding: 7px 12px; font-size: 0.82em;
    }
    .btn-ghost:hover { border-color: var(--accent); color: var(--accent); }
    .btn-danger-ghost {
      background: rgba(239, 68, 68, 0.1); color: #fca5a5; border: 1px solid rgba(239, 68, 68, 0.35);
      font-weight: 700; padding: 7px 12px; font-size: 0.82em;
    }
    .btn-danger-ghost:hover { background: rgba(239, 68, 68, 0.2); }
    button:hover, .btn:hover { opacity: 0.94; transform: translateY(-1px); }
    button:disabled { opacity: 0.55; cursor: not-allowed; transform: none; }
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

    .fav-summary-bar {
      display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px;
      background: linear-gradient(135deg, rgba(19, 29, 49, 0.95) 0%, rgba(24, 37, 63, 0.85) 100%);
      border: 1px solid rgba(251, 191, 36, 0.28);
      border-radius: 14px; padding: 14px 16px; margin-bottom: 18px;
    }
    .fav-kpis { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
    .fav-kpi {
      background: rgba(11, 18, 32, 0.72); border: 1px solid var(--border);
      padding: 6px 12px; border-radius: 999px; font-size: 0.82em; color: var(--muted);
    }
    .fav-kpi strong { color: var(--text); }
    .fav-kpi.gold { border-color: rgba(251, 191, 36, 0.45); color: #fde68a; }
    .fav-kpi.live { border-color: rgba(239, 68, 68, 0.45); color: #fca5a5; }
    .fav-section-header {
      display: flex; align-items: center; justify-content: space-between; gap: 10px;
      margin: 18px 0 10px 0; padding-bottom: 6px; border-bottom: 1px solid rgba(36, 51, 77, 0.75);
    }
    .fav-section-title {
      font-size: 0.95em; font-weight: 800; letter-spacing: 0.03em; text-transform: uppercase;
      display: flex; align-items: center; gap: 8px; color: #f8fafc;
    }
    .fav-count-pill {
      background: rgba(56, 189, 248, 0.14); color: var(--accent); border: 1px solid rgba(56, 189, 248, 0.3);
      padding: 2px 8px; border-radius: 999px; font-size: 0.78em; font-weight: 800;
    }
    .fav-slip-footer {
      display: flex; align-items: center; justify-content: space-between; gap: 8px;
      margin-top: 10px; padding-top: 9px; border-top: 1px dashed rgba(148, 163, 184, 0.22);
      font-size: 0.8em; color: var(--muted);
    }
    .odds-chip {
      background: rgba(251, 191, 36, 0.14); color: #fde68a; border: 1px solid rgba(251, 191, 36, 0.38);
      padding: 4px 9px; border-radius: 7px; font-weight: 800; font-size: 0.85em;
    }
    .fav-empty-box {
      background: var(--card); border: 1px dashed var(--border); border-radius: 14px;
      padding: 28px 20px; text-align: center; color: var(--muted); margin-bottom: 24px;
    }
    .fav-empty-box strong { color: var(--text); display: block; font-size: 1.05em; margin-bottom: 6px; }

    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 16px; margin-bottom: 24px; }
    .card {
      background: var(--card); padding: 16px; border-radius: 12px;
      border: 1px solid var(--border); border-left: 4px solid var(--accent);
      box-shadow: 0 6px 14px rgba(0,0,0,0.18);
      transition: transform 0.14s ease, background 0.14s ease, border-color 0.14s ease, opacity 0.18s ease;
    }
    .card:hover { background: var(--card-hover); transform: translateY(-2px); }
    .card.HAUTE { border-left-color: var(--green); }
    .card.MOYENNE { border-left-color: var(--orange); }
    .card.FAIBLE { border-left-color: var(--red); }
    .card.live-card { border-left-color: var(--red); border-color: rgba(239, 68, 68, 0.35); }
    .card.termine-card { border-left-color: #64748b; opacity: 0.92; }
    .card-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; margin-bottom: 8px; }
    .card-title { font-size: 1.02em; font-weight: 700; line-height: 1.3; display: flex; align-items: center; gap: 6px; min-width: 0; }
    .badges-right { display: flex; align-items: center; gap: 5px; flex-shrink: 0; flex-wrap: wrap; justify-content: flex-end; }
    .star-btn {
      background: rgba(15, 23, 42, 0.85); color: #94a3b8; border: 1px solid var(--border);
      padding: 4px 8px; border-radius: 8px; font-size: 0.95em; line-height: 1;
    }
    .star-btn:hover { color: var(--gold); border-color: rgba(251, 191, 36, 0.5); }
    .star-btn.active { background: rgba(251, 191, 36, 0.18); color: var(--gold); border-color: rgba(251, 191, 36, 0.55); }
    .badge { font-size: 0.73em; padding: 3px 7px; border-radius: 999px; font-weight: 800; white-space: nowrap; }
    .badge.proba { background: rgba(56, 189, 248, 0.16); color: #7dd3fc; border: 1px solid rgba(56, 189, 248, 0.35); }
    .badge.cote { background: rgba(148, 163, 184, 0.18); color: #e2e8f0; border: 1px solid rgba(148, 163, 184, 0.32); }
    .badge.value { background: rgba(16, 185, 129, 0.2); color: #6ee7b7; border: 1px solid rgba(16, 185, 129, 0.45); }
    .badge.live { background: rgba(239, 68, 68, 0.18); color: #fca5a5; border: 1px solid rgba(239, 68, 68, 0.45); display: inline-flex; align-items: center; gap: 5px; }
    .badge.termine { background: rgba(148, 163, 184, 0.18); color: #cbd5e1; border: 1px solid rgba(148, 163, 184, 0.35); }
    .badge.a-venir { background: rgba(56, 189, 248, 0.15); color: #7dd3fc; border: 1px solid rgba(56, 189, 248, 0.35); }
    .badge.gagne { background: rgba(16, 185, 129, 0.22); color: #6ee7b7; border: 1px solid rgba(16, 185, 129, 0.45); }
    .badge.perdu { background: rgba(239, 68, 68, 0.22); color: #fca5a5; border: 1px solid rgba(239, 68, 68, 0.45); }
    .badge.HAUTE { background: rgba(16, 185, 129, 0.2); color: #6ee7b7; border: 1px solid rgba(16, 185, 129, 0.4); }
    .badge.MOYENNE { background: rgba(245, 158, 11, 0.2); color: #fcd34d; border: 1px solid rgba(245, 158, 11, 0.4); }
    .badge.FAIBLE { background: rgba(239, 68, 68, 0.2); color: #fca5a5; border: 1px solid rgba(239, 68, 68, 0.4); }
    .badge.imminent { background: rgba(239, 68, 68, 0.15); color: #fca5a5; border: 1px solid rgba(239, 68, 68, 0.4); display: inline-flex; align-items: center; gap: 4px; }
    .dot-red { width: 8px; height: 8px; background-color: #ef4444; border-radius: 50%; animation: pulse-red 1.5s infinite; }
    @keyframes pulse-red { 0% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.7); } 70% { box-shadow: 0 0 0 6px rgba(239, 68, 68, 0); } 100% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0); } }
    .card-meta { font-size: 0.86em; color: var(--muted); margin-bottom: 10px; line-height: 1.45; }
    .card-meta strong { color: var(--text); }
    .score-box {
      margin-top: 6px; padding: 7px 9px; border-radius: 8px;
      background: rgba(15, 23, 42, 0.72); border: 1px solid rgba(56, 189, 248, 0.22);
    }
    .score-main { color: var(--accent); font-weight: 700; font-size: 0.92em; }
    .live-score-box {
      display: flex; justify-content: space-between; align-items: center;
      background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.3);
      border-radius: 8px; padding: 8px 11px; margin-bottom: 10px;
    }
    .final-score-box {
      display: flex; justify-content: space-between; align-items: center;
      background: rgba(148, 163, 184, 0.12); border: 1px solid rgba(148, 163, 184, 0.32);
      border-radius: 8px; padding: 8px 11px; margin-bottom: 10px;
    }
    .live-score-val { font-size: 1.08em; font-weight: 800; color: #f8fafc; }
    .pred-box {
      background: rgba(56, 189, 248, 0.1); border: 1px solid rgba(56, 189, 248, 0.32);
      border-radius: 8px; padding: 9px 11px; margin-top: 8px;
    }
    .pred-label { font-size: 0.73em; text-transform: uppercase; letter-spacing: 0.04em; color: #7dd3fc; font-weight: 800; margin-bottom: 3px; }
    .pred-choice { font-size: 0.96em; font-weight: 800; color: #f8fafc; }
    .pred-sub { font-size: 0.78em; color: var(--muted); margin-top: 2px; }
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
    .panel { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; margin-bottom: 24px; }
    .stats-bar { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 16px; }
    .stat-item { background: var(--card); border: 1px solid var(--border); padding: 8px 13px; border-radius: 999px; font-size: 0.84em; }
    pre { white-space: pre-wrap; background: #0b1220; border: 1px solid var(--border); padding: 14px; border-radius: 8px; overflow-x: auto; font-size: 0.85em; margin: 0; line-height: 1.5; }
    .notice { background: rgba(56, 189, 248, 0.12); border: 1px solid rgba(56, 189, 248, 0.35); color: #bae6fd; padding: 10px 14px; border-radius: 10px; margin-bottom: 18px; font-size: 0.9em; }
    .notice.warn { background: rgba(245, 158, 11, 0.14); border-color: rgba(245, 158, 11, 0.45); color: #fde68a; }
    .notice.ok { background: rgba(16, 185, 129, 0.14); border-color: rgba(16, 185, 129, 0.42); color: #a7f3d0; }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div>
        <a href="/" class="brand-link" title="Retour à l'accueil">
          <span class="brand-badge"><img src="/icon.svg" alt="BetPulse Pro"></span>
          <h1 style="margin: 0;">BetPulse Pro{% if page_favoris %} — Favoris{% endif %}</h1>
        </a><br>
        <span class="meta">Dernière mise à jour : {{ r.mis_a_jour or "En attente" }} • Fenêtre : {{ fenetre_heures }}h • <span id="compteur-cartes">Filtre actif</span></span>
      </div>
      <div class="header-actions">
        {% if page_favoris %}
        <a href="{{ url_for('app_view', cat='all') }}" class="btn btn-cat">← Tous les matchs</a>
        {% endif %}
        <form id="refresh-form" method="post" action="/refresh" style="margin: 0;" onsubmit="return soumettreRefresh(event);">
          <input type="hidden" name="admin_token" id="refresh-admin-token" value="">
          <button id="btn-refresh" type="submit">↻ Actualiser</button>
        </form>
      </div>
    </div>

    <div id="action-banner" class="notice warn" style="display: none;"></div>

    <div class="cat-bar">
      <a href="{{ url_for('app_view', cat='all') }}" class="btn btn-cat {% if not page_favoris and cat == 'all' %}active{% endif %}">Tout</a>
      <a href="{{ url_for('app_view', cat='favoris') }}" class="btn btn-cat {% if page_favoris or cat == 'favoris' %}active{% endif %}">★ Favoris (<span id="fav-count-tab">{{ r.favoris_suivis|length }}</span>)</a>
      <a href="{{ url_for('app_view', cat='live') }}" class="btn btn-cat {% if not page_favoris and cat == 'live' %}active{% endif %}">En cours ({{ r.en_cours_matchs|length }})</a>
      <a href="{{ url_for('app_view', cat='top') }}" class="btn btn-cat {% if not page_favoris and cat == 'top' %}active{% endif %}">Top Pronostics ({{ r.top_sur|length }})</a>
      <a href="{{ url_for('app_view', cat='foot') }}" class="btn btn-cat {% if not page_favoris and cat == 'foot' %}active{% endif %}">Football ({{ r.foot|length }})</a>
      <a href="{{ url_for('app_view', cat='tennis') }}" class="btn btn-cat {% if not page_favoris and cat == 'tennis' %}active{% endif %}">Tennis ({{ r.tennis|length }})</a>
      <a href="{{ url_for('app_view', cat='alertes') }}" class="btn btn-cat {% if not page_favoris and cat == 'alertes' %}active{% endif %}">Alertes ({{ r.get('alertes', {})|length }})</a>
      <a href="{{ url_for('app_view', cat='bilan') }}" class="btn btn-cat {% if not page_favoris and cat == 'bilan' %}active{% endif %}">Bilan &amp; Synthèse</a>
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
          <option value="date" selected>Tri : Date match</option>
          <option value="conf">Tri : Confiance</option>
          <option value="proba">Tri : Probabilité</option>
        </select>
      </div>
    </div>

    {% if en_cours %}
    <div class="notice">Mise à jour en arrière-plan en cours — les données affichées restent accessibles.</div>
    {% endif %}

    {% if page_favoris or cat == 'favoris' %}
    {% set fav_live = [] %}
    {% set fav_upcoming = [] %}
    {% set fav_done = [] %}
    {% for it in r.favoris_suivis %}
      {% set st_etat = (it.get('etat_match') or 'A_VENIR')|upper %}
      {% if st_etat == 'EN_COURS' %}
        {% set _ = fav_live.append(it) %}
      {% elif st_etat == 'TERMINE' %}
        {% set _ = fav_done.append(it) %}
      {% else %}
        {% set _ = fav_upcoming.append(it) %}
      {% endif %}
    {% endfor %}

    <div class="fav-summary-bar" id="fav-summary-bar">
      <div class="fav-kpis">
        <span class="fav-kpi gold">★ Suivis : <strong id="kpi-fav-total">{{ r.favoris_suivis|length }}</strong></span>
        <span class="fav-kpi live">● En cours : <strong id="kpi-fav-live">{{ fav_live|length }}</strong></span>
        <span class="fav-kpi">⏱ À venir : <strong id="kpi-fav-upcoming">{{ fav_upcoming|length }}</strong></span>
        <span class="fav-kpi">✓ Terminés : <strong id="kpi-fav-done">{{ fav_done|length }}</strong></span>
      </div>
      <div class="header-actions">
        <button type="button" class="btn-ghost" onclick="setFavGroupFilter('ALL', this)" id="fav-filter-all">Tous</button>
        <button type="button" class="btn-ghost" onclick="setFavGroupFilter('ACTIFS', this)" id="fav-filter-actifs">Actifs (Live + À venir)</button>
        <button type="button" class="btn-danger-ghost" id="btn-clear-done" onclick="nettoyerTermines()" {% if fav_done|length == 0 %}disabled{% endif %}>
          🗑 Effacer terminés (<span id="kpi-fav-done-btn">{{ fav_done|length }}</span>)
        </button>
      </div>
    </div>

    <div id="favoris-container">
      {% if r.favoris_suivis|length == 0 %}
      <div class="fav-empty-box" id="fav-empty-msg">
        <strong>Aucun match dans votre liste de favoris</strong>
        Cliquez sur ☆ sur n'importe quel match (En cours, Top Pronostics, Foot ou Tennis) pour l'épingler ici et suivre son résultat même après la fin.
      </div>
      {% else %}

      <div class="fav-group-section" data-fav-group="ACTIFS" id="fav-sec-live" {% if fav_live|length == 0 %}style="display:none;"{% endif %}>
        <div class="fav-section-header">
          <div class="fav-section-title"><span class="dot-red"></span> En direct <span class="fav-count-pill" id="cnt-fav-live">{{ fav_live|length }}</span></div>
        </div>
        <div class="grid" id="fav-grid-live">
          {% for it in fav_live %}
          {% set conf = it.get('confiance', 'MOYENNE') %}
          {% set conf_sc = it.get('confiance_score', 50.0) %}
          {% set ts_m = it.get('ts_match', 9999999999.0) %}
          <div class="card live-card {{ conf }}"
               data-fav-card="1"
               data-fav-state="EN_COURS"
               data-cle-favori="{{ it.get('cle_favori', '') }}"
               data-conf="{{ conf }}"
               data-proba="{{ it.proba or 0 }}"
               data-conf-score="{{ conf_sc }}"
               data-ts="{{ ts_m }}"
               data-search="{{ ((it.choix or '') ~ ' ' ~ (it.match or '') ~ ' ' ~ (it.competition or '') ~ ' ' ~ (it.score_actuel or '') ~ ' ' ~ (it.detail or ''))|lower }}">
            <div class="card-top">
              <div class="card-title">{% if it.sport == 'FOOT' %}⚽{% else %}🎾{% endif %} {{ it.match }}</div>
              <div class="badges-right">
                <span class="badge live"><span class="dot-red"></span> {{ it.chrono_live or 'LIVE' }}</span>
                <button type="button" class="star-btn active" title="Retirer des favoris" onclick="toggleFavori(this)"
                        data-cle-favori="{{ it.get('cle_favori', '') }}" data-sport="{{ it.sport or 'FOOT' }}"
                        data-event-id="{{ it.event_id or '' }}" data-match="{{ it.match or '' }}"
                        data-competition="{{ it.competition or '' }}" data-date-match="{{ it.date_match or '' }}"
                        data-ts-match="{{ ts_m }}" data-choix="{{ it.choix or '' }}"
                        data-proba="{{ it.proba if it.proba is not none else '' }}" data-cote="{{ it.cote if it.cote is not none else '' }}"
                        data-mise-u="{{ it.mise_u if it.mise_u is not none else '' }}" data-confiance="{{ conf }}"
                        data-confiance-score="{{ conf_sc }}" data-detail="{{ it.detail or '' }}" data-etat-match="EN_COURS">★</button>
              </div>
            </div>
            <div class="card-meta">{{ it.competition or it.sport }} • {{ it.date_match }}</div>
            <div class="live-score-box">
              <span class="meta">Score en direct</span>
              <span class="live-score-val">{{ it.score_actuel or 'En cours' }}</span>
            </div>
            <div class="pred-box">
              <div class="pred-label">Sélection suivie</div>
              <div class="pred-choice">{{ it.choix or 'Non défini' }}</div>
              <div class="pred-sub">
                {% if it.proba is not none %}Proba : <strong>{{ it.proba }}%</strong>{% endif %}
                {% if it.cote %} • Cote : <strong>{{ it.cote }}</strong>{% endif %}
              </div>
            </div>
            <div class="fav-slip-footer">
              <span>Mise suggérée : <strong>{{ it.mise_u if it.mise_u is not none else '-' }}u</strong></span>
              {% if it.cote %}<span class="odds-chip">@ {{ it.cote }}</span>{% endif %}
            </div>
          </div>
          {% endfor %}
        </div>
      </div>

      <div class="fav-group-section" data-fav-group="ACTIFS" id="fav-sec-upcoming" {% if fav_upcoming|length == 0 %}style="display:none;"{% endif %}>
        <div class="fav-section-header">
          <div class="fav-section-title">⏱ À venir <span class="fav-count-pill" id="cnt-fav-upcoming">{{ fav_upcoming|length }}</span></div>
        </div>
        <div class="grid" id="fav-grid-upcoming">
          {% for it in fav_upcoming %}
          {% set conf = it.get('confiance', 'MOYENNE') %}
          {% set conf_sc = it.get('confiance_score', 50.0) %}
          {% set ts_m = it.get('ts_match', 9999999999.0) %}
          <div class="card {{ conf }}"
               data-fav-card="1"
               data-fav-state="A_VENIR"
               data-cle-favori="{{ it.get('cle_favori', '') }}"
               data-conf="{{ conf }}"
               data-proba="{{ it.proba or 0 }}"
               data-conf-score="{{ conf_sc }}"
               data-ts="{{ ts_m }}"
               data-search="{{ ((it.choix or '') ~ ' ' ~ (it.match or '') ~ ' ' ~ (it.competition or '') ~ ' ' ~ (it.detail or ''))|lower }}">
            <div class="card-top">
              <div class="card-title">{% if it.sport == 'FOOT' %}⚽{% else %}🎾{% endif %} {{ it.match }}</div>
              <div class="badges-right">
                <span class="badge a-venir">À VENIR</span>
                <button type="button" class="star-btn active" title="Retirer des favoris" onclick="toggleFavori(this)"
                        data-cle-favori="{{ it.get('cle_favori', '') }}" data-sport="{{ it.sport or 'FOOT' }}"
                        data-event-id="{{ it.event_id or '' }}" data-match="{{ it.match or '' }}"
                        data-competition="{{ it.competition or '' }}" data-date-match="{{ it.date_match or '' }}"
                        data-ts-match="{{ ts_m }}" data-choix="{{ it.choix or '' }}"
                        data-proba="{{ it.proba if it.proba is not none else '' }}" data-cote="{{ it.cote if it.cote is not none else '' }}"
                        data-mise-u="{{ it.mise_u if it.mise_u is not none else '' }}" data-confiance="{{ conf }}"
                        data-confiance-score="{{ conf_sc }}" data-detail="{{ it.detail or '' }}" data-etat-match="A_VENIR">★</button>
              </div>
            </div>
            <div class="card-meta">{{ it.competition or it.sport }} • {{ it.date_match }}</div>
            <div class="pred-box">
              <div class="pred-label">Sélection suivie</div>
              <div class="pred-choice">{{ it.choix or 'Non défini' }}</div>
              <div class="pred-sub">
                {% if it.proba is not none %}Proba : <strong>{{ it.proba }}%</strong>{% endif %}
                • Confiance : <strong>{{ conf }}</strong>
              </div>
            </div>
            <div class="fav-slip-footer">
              <span>Mise : <strong>{{ it.mise_u if it.mise_u is not none else '-' }}u</strong></span>
              {% if it.cote %}<span class="odds-chip">@ {{ it.cote }}</span>{% endif %}
            </div>
          </div>
          {% endfor %}
        </div>
      </div>

      <div class="fav-group-section" data-fav-group="TERMINE" id="fav-sec-done" {% if fav_done|length == 0 %}style="display:none;"{% endif %}>
        <div class="fav-section-header">
          <div class="fav-section-title">✓ Terminés <span class="fav-count-pill" id="cnt-fav-done">{{ fav_done|length }}</span></div>
        </div>
        <div class="grid" id="fav-grid-done">
          {% for it in fav_done %}
          {% set conf = it.get('confiance', 'MOYENNE') %}
          {% set conf_sc = it.get('confiance_score', 50.0) %}
          {% set ts_m = it.get('ts_match', 9999999999.0) %}
          <div class="card termine-card {{ conf }}"
               data-fav-card="1"
               data-fav-state="TERMINE"
               data-cle-favori="{{ it.get('cle_favori', '') }}"
               data-conf="{{ conf }}"
               data-proba="{{ it.proba or 0 }}"
               data-conf-score="{{ conf_sc }}"
               data-ts="{{ ts_m }}"
               data-search="{{ ((it.choix or '') ~ ' ' ~ (it.match or '') ~ ' ' ~ (it.competition or '') ~ ' ' ~ (it.score_final or '') ~ ' ' ~ (it.detail or ''))|lower }}">
            <div class="card-top">
              <div class="card-title">{% if it.sport == 'FOOT' %}⚽{% else %}🎾{% endif %} {{ it.match }}</div>
              <div class="badges-right">
                <span class="badge termine">TERMINÉ</span>
                {% if it.get('statut_prono') == 'GAGNE' %}
                  <span class="badge gagne">GAGNÉ</span>
                {% elif it.get('statut_prono') == 'PERDU' %}
                  <span class="badge perdu">PERDU</span>
                {% endif %}
                <button type="button" class="star-btn active" title="Retirer des favoris" onclick="toggleFavori(this)"
                        data-cle-favori="{{ it.get('cle_favori', '') }}" data-sport="{{ it.sport or 'FOOT' }}"
                        data-event-id="{{ it.event_id or '' }}" data-match="{{ it.match or '' }}"
                        data-competition="{{ it.competition or '' }}" data-date-match="{{ it.date_match or '' }}"
                        data-ts-match="{{ ts_m }}" data-choix="{{ it.choix or '' }}"
                        data-proba="{{ it.proba if it.proba is not none else '' }}" data-cote="{{ it.cote if it.cote is not none else '' }}"
                        data-mise-u="{{ it.mise_u if it.mise_u is not none else '' }}" data-confiance="{{ conf }}"
                        data-confiance-score="{{ conf_sc }}" data-detail="{{ it.detail or '' }}" data-etat-match="TERMINE">★</button>
              </div>
            </div>
            <div class="card-meta">{{ it.competition or it.sport }} • {{ it.date_match }}</div>
            <div class="final-score-box">
              <span class="meta">Score final</span>
              <span class="live-score-val">{{ it.score_final or 'Terminé' }}</span>
            </div>
            <div class="pred-box">
              <div class="pred-label">Pronostic</div>
              <div class="pred-choice">{{ it.choix or 'Non défini' }}</div>
              <div class="pred-sub">
                {% if it.proba is not none %}Proba : <strong>{{ it.proba }}%</strong>{% endif %}
                {% if it.cote %} • Cote : <strong>{{ it.cote }}</strong>{% endif %}
              </div>
            </div>
          </div>
          {% endfor %}
        </div>
      </div>
      {% endif %}
    </div>
    {% endif %}

    {% if not page_favoris %}
      {% if cat in ('all', 'live') %}
      <h2><span class="dot-red"></span> Matchs en cours ({{ r.en_cours_matchs|length }})</h2>
      <div class="grid">
        {% for it in r.en_cours_matchs %}
        {% set conf = it.get('confiance', 'MOYENNE') %}
        {% set conf_sc = it.get('confiance_score', it.proba) %}
        {% set ts_m = it.get('ts_match', 9999999999.0) %}
        {% set is_fav = it.get('est_favori', False) %}
        {% set pct_score = it.get('score_exact_proba') if it.get('score_exact_proba') is not none else (it.pronostic_proba or it.proba) %}
        <div class="card live-card {{ conf }}"
             data-conf="{{ conf }}"
             data-proba="{{ it.pronostic_proba or it.proba }}"
             data-conf-score="{{ conf_sc }}"
             data-ts="{{ ts_m }}"
             data-search="{{ ((it.pronostic_predi or it.choix) ~ ' ' ~ it.match ~ ' ' ~ it.competition ~ ' ' ~ (it.score_actuel or '') ~ ' ' ~ (it.score_exact or ''))|lower }}">
          <div class="card-top">
            <div class="card-title">
              {% if it.sport == 'FOOT' %}⚽{% else %}🎾{% endif %} {{ it.match }}
            </div>
            <div class="badges-right">
              <span class="badge live"><span class="dot-red"></span> {{ it.chrono_live or 'LIVE' }}</span>
              <button type="button"
                      class="star-btn {% if is_fav %}active{% endif %}"
                      title="{% if is_fav %}Retirer des favoris{% else %}Ajouter aux favoris{% endif %}"
                      onclick="toggleFavori(this)"
                      data-cle-favori="{{ it.get('cle_favori', '') }}"
                      data-sport="{{ it.sport or 'FOOT' }}"
                      data-event-id="{{ it.event_id or '' }}"
                      data-match="{{ it.match or '' }}"
                      data-competition="{{ it.competition or '' }}"
                      data-date-match="{{ it.date_match or '' }}"
                      data-ts-match="{{ ts_m }}"
                      data-choix="{{ it.pronostic_predi or it.choix or '' }}"
                      data-proba="{{ it.pronostic_proba or it.proba or '' }}"
                      data-cote="{{ it.pronostic_cote or it.cote or '' }}"
                      data-mise-u="{{ it.mise_u if it.mise_u is not none else '' }}"
                      data-confiance="{{ conf }}"
                      data-confiance-score="{{ conf_sc }}"
                      data-detail="{{ it.detail or '' }}"
                      data-etat-match="EN_COURS"
                      data-score-actuel="{{ it.score_actuel or '' }}"
                      data-chrono-live="{{ it.chrono_live or 'LIVE' }}">{% if is_fav %}★{% else %}☆{% endif %}</button>
            </div>
          </div>
          <div class="card-meta">
            {{ it.competition }} • Début : {{ it.date_match }}
            {% if it.get('score_exact') %}
            <div class="score-box">
              <div class="score-main">Score exact cohérent : {{ it.score_exact }} ({{ pct_score }}%)</div>
            </div>
            {% endif %}
          </div>
          <div class="live-score-box">
            <span class="meta">Score actuel</span>
            <span class="live-score-val">{{ it.score_actuel or 'En cours' }}</span>
          </div>
          <div class="pred-box">
            <div class="pred-label">Pronostic prédit ({{ it.pronostic_source or 'Bot' }})</div>
            <div class="pred-choice">{{ it.pronostic_predi or it.choix }}</div>
            <div class="pred-sub">
              Proba : <strong>{{ it.pronostic_proba or it.proba }}%</strong>
              {% if it.pronostic_cote or it.cote %} • Cote : <strong>{{ it.pronostic_cote or it.cote }}</strong>{% endif %}
              • Statut : <strong>{{ it.pronostic_statut or 'EN_ATTENTE' }}</strong>
            </div>
          </div>
        </div>
        {% else %}
        <div class="card">Aucun match en cours pour le moment.</div>
        {% endfor %}
      </div>
      {% endif %}

      {% if cat in ('all', 'top') %}
      <h2>★ Top pronostics (Foot &amp; Tennis)</h2>
      <div class="grid">
        {% for it in r.top_sur %}
        {% set conf = it.get('confiance', 'MOYENNE') %}
        {% set conf_sc = it.get('confiance_score', it.proba) %}
        {% set pct_score = it.get('score_exact_proba') if it.get('score_exact_proba') is not none else it.proba %}
        {% set ts_m = it.get('ts_match', 9999999999.0) %}
        {% set is_fav = it.get('est_favori', False) %}
        <div class="card {{ conf }}"
             data-conf="{{ conf }}"
             data-proba="{{ it.proba }}"
             data-conf-score="{{ conf_sc }}"
             data-ts="{{ ts_m }}"
             data-search="{{ (it.choix ~ ' ' ~ it.match ~ ' ' ~ it.competition ~ ' ' ~ (it.score_exact or '') ~ ' ' ~ (it.detail or ''))|lower }}">
          <div class="card-top">
            <div class="card-title">
              {% if it.sport == 'FOOT' %}⚽{% else %}🎾{% endif %} {{ it.choix }}
            </div>
            <div class="badges-right">
              {% if it.get('est_value_bet') %}
              <span class="badge value" title="Cote réelle supérieure ou égale à la cote minimale">VALUE +{{ it.get('ecart_value_pct', 0) }}%</span>
              {% endif %}
              {% if it.get('cote') %}
              <span class="badge cote" title="{{ it.get('cote_source', 'Moyenne bookmakers') }}">Cote {{ it.cote }}</span>
              {% endif %}
              <span class="badge proba" title="Probabilité estimée">{{ pct_score }}%</span>
              {% if it.get('est_imminent') %}
              <span class="badge imminent"><span class="dot-red"></span> Imminent</span>
              {% endif %}
              <button type="button"
                      class="star-btn {% if is_fav %}active{% endif %}"
                      title="{% if is_fav %}Retirer des favoris{% else %}Ajouter aux favoris{% endif %}"
                      onclick="toggleFavori(this)"
                      data-cle-favori="{{ it.get('cle_favori', '') }}"
                      data-sport="{{ it.sport or 'FOOT' }}"
                      data-event-id="{{ it.event_id or '' }}"
                      data-match="{{ it.match or '' }}"
                      data-competition="{{ it.competition or '' }}"
                      data-date-match="{{ it.date_match or '' }}"
                      data-ts-match="{{ ts_m }}"
                      data-choix="{{ it.choix or '' }}"
                      data-proba="{{ it.proba if it.proba is not none else '' }}"
                      data-cote="{{ it.cote if it.cote is not none else '' }}"
                      data-mise-u="{{ it.mise_u if it.mise_u is not none else '' }}"
                      data-confiance="{{ conf }}"
                      data-confiance-score="{{ conf_sc }}"
                      data-detail="{{ it.detail or '' }}"
                      data-etat-match="A_VENIR">{% if is_fav %}★{% else %}☆{% endif %}</button>
            </div>
          </div>
          <div class="card-meta">
            <strong>{{ it.match }}</strong><br>
            {{ it.date_match }} — {{ it.competition }}
            {% if it.get('score_exact') %}
            <div class="score-box">
              <div class="score-main">Score exact cohérent : {{ it.score_exact }} ({{ pct_score }}%)</div>
            </div>
            {% endif %}
          </div>
          <div class="conf-meter">
            <div class="conf-label"><span>Indice de confiance ({{ conf }})</span><span>{{ conf_sc }}/100</span></div>
            <div class="meter-track"><div class="meter-fill" style="width: {{ conf_sc }}%;"></div></div>
          </div>
          {% if it.detail %}
          <div class="card-detail">{{ it.detail }}</div>
          {% endif %}
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
        {% set pct_score = it.get('score_exact_proba') if it.get('score_exact_proba') is not none else it.proba %}
        {% set ts_m = it.get('ts_match', 9999999999.0) %}
        {% set is_fav = it.get('est_favori', False) %}
        <div class="card {{ conf }}"
             data-conf="{{ conf }}"
             data-proba="{{ it.proba }}"
             data-conf-score="{{ conf_sc }}"
             data-ts="{{ ts_m }}"
             data-search="{{ (it.choix ~ ' ' ~ it.match ~ ' ' ~ it.competition ~ ' ' ~ (it.score_exact or '') ~ ' ' ~ (it.detail or ''))|lower }}">
          <div class="card-top">
            <div class="card-title">⚽ {{ it.choix }}</div>
            <div class="badges-right">
              {% if it.get('est_value_bet') %}
              <span class="badge value" title="Cote réelle supérieure ou égale à la cote minimale">VALUE +{{ it.get('ecart_value_pct', 0) }}%</span>
              {% endif %}
              {% if it.get('cote') %}
              <span class="badge cote" title="{{ it.get('cote_source', 'Moyenne bookmakers') }}">Cote {{ it.cote }}</span>
              {% endif %}
              <span class="badge proba" title="Probabilité estimée">{{ pct_score }}%</span>
              {% if it.get('est_imminent') %}
              <span class="badge imminent"><span class="dot-red"></span> Imminent</span>
              {% endif %}
              <button type="button"
                      class="star-btn {% if is_fav %}active{% endif %}"
                      title="{% if is_fav %}Retirer des favoris{% else %}Ajouter aux favoris{% endif %}"
                      onclick="toggleFavori(this)"
                      data-cle-favori="{{ it.get('cle_favori', '') }}"
                      data-sport="{{ it.sport or 'FOOT' }}"
                      data-event-id="{{ it.event_id or '' }}"
                      data-match="{{ it.match or '' }}"
                      data-competition="{{ it.competition or '' }}"
                      data-date-match="{{ it.date_match or '' }}"
                      data-ts-match="{{ ts_m }}"
                      data-choix="{{ it.choix or '' }}"
                      data-proba="{{ it.proba if it.proba is not none else '' }}"
                      data-cote="{{ it.cote if it.cote is not none else '' }}"
                      data-mise-u="{{ it.mise_u if it.mise_u is not none else '' }}"
                      data-confiance="{{ conf }}"
                      data-confiance-score="{{ conf_sc }}"
                      data-detail="{{ it.detail or '' }}"
                      data-etat-match="A_VENIR">{% if is_fav %}★{% else %}☆{% endif %}</button>
            </div>
          </div>
          <div class="card-meta">
            <strong>{{ it.match }}</strong><br>
            {{ it.date_match }}
            {% if it.get('score_exact') %}
            <div class="score-box">
              <div class="score-main">Score exact cohérent : {{ it.score_exact }} ({{ pct_score }}%)</div>
            </div>
            {% endif %}
          </div>
          <div class="conf-meter">
            <div class="conf-label"><span>Indice de confiance ({{ conf }})</span><span>{{ conf_sc }}/100</span></div>
            <div class="meter-track"><div class="meter-fill" style="width: {{ conf_sc }}%;"></div></div>
          </div>
          <div class="card-detail">{{ it.detail }}</div>
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
        {% set pct_score = it.get('score_exact_proba') if it.get('score_exact_proba') is not none else it.proba %}
        {% set ts_m = it.get('ts_match', 9999999999.0) %}
        {% set is_fav = it.get('est_favori', False) %}
        <div class="card {{ conf }}"
             data-conf="{{ conf }}"
             data-proba="{{ it.proba }}"
             data-conf-score="{{ conf_sc }}"
             data-ts="{{ ts_m }}"
             data-search="{{ (it.choix ~ ' ' ~ it.match ~ ' ' ~ it.competition ~ ' ' ~ (it.detail or ''))|lower }}">
          <div class="card-top">
            <div class="card-title">🎾 {{ it.choix }}</div>
            <div class="badges-right">
              {% if it.get('est_value_bet') %}
              <span class="badge value" title="Cote réelle supérieure ou égale à la cote minimale">VALUE +{{ it.get('ecart_value_pct', 0) }}%</span>
              {% endif %}
              {% if it.get('cote') %}
              <span class="badge cote" title="{{ it.get('cote_source', 'Moyenne bookmakers') }}">Cote {{ it.cote }}</span>
              {% endif %}
              <span class="badge proba" title="Probabilité estimée">{{ pct_score }}%</span>
              {% if it.get('est_imminent') %}
              <span class="badge imminent"><span class="dot-red"></span> Imminent</span>
              {% endif %}
              <button type="button"
                      class="star-btn {% if is_fav %}active{% endif %}"
                      title="{% if is_fav %}Retirer des favoris{% else %}Ajouter aux favoris{% endif %}"
                      onclick="toggleFavori(this)"
                      data-cle-favori="{{ it.get('cle_favori', '') }}"
                      data-sport="{{ it.sport or 'TENNIS' }}"
                      data-event-id="{{ it.event_id or '' }}"
                      data-match="{{ it.match or '' }}"
                      data-competition="{{ it.competition or '' }}"
                      data-date-match="{{ it.date_match or '' }}"
                      data-ts-match="{{ ts_m }}"
                      data-choix="{{ it.choix or '' }}"
                      data-proba="{{ it.proba if it.proba is not none else '' }}"
                      data-cote="{{ it.cote if it.cote is not none else '' }}"
                      data-mise-u="{{ it.mise_u if it.mise_u is not none else '' }}"
                      data-confiance="{{ conf }}"
                      data-confiance-score="{{ conf_sc }}"
                      data-detail="{{ it.detail or '' }}"
                      data-etat-match="A_VENIR">{% if is_fav %}★{% else %}☆{% endif %}</button>
            </div>
          </div>
          <div class="card-meta">
            <strong>{{ it.match }}</strong><br>
            {{ it.date_match }} — {{ it.competition }}
          </div>
          <div class="conf-meter">
            <div class="conf-label"><span>Indice de confiance ({{ conf }})</span><span>{{ conf_sc }}/100</span></div>
            <div class="meter-track"><div class="meter-fill" style="width: {{ conf_sc }}%;"></div></div>
          </div>
          {% if it.detail %}
          <div class="card-detail">{{ it.detail }}</div>
          {% endif %}
        </div>
        {% else %}
        <div class="card">Aucun match de tennis à venir dans cette fenêtre.</div>
        {% endfor %}
      </div>
      {% endif %}

      {% if cat in ('all', 'alertes') %}
      <h2>🩺 Alertes blessures / forfaits (2j)</h2>
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
    {% endif %}
  </div>

  <script>
    let confActif = "ALL";
    let favGroupFilter = "ALL";
    const PAGE_FAVORIS = {{ "true" if (page_favoris or cat == "favoris") else "false" }};
    const TOKEN_REQUIS = {{ "true" if token_requis else "false" }};

    function escapeHtml(str) {
      return String(str ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }

    function afficherBanniere(msg, type = "warn") {
      const b = document.getElementById("action-banner");
      if (!b) return;
      b.className = "notice " + type;
      b.textContent = msg;
      b.style.display = msg ? "" : "none";
    }

    function obtenirTokenAdmin() {
      let tok = "";
      try { tok = localStorage.getItem("betpulse_admin_token") || ""; } catch (e) {}
      if (!tok && TOKEN_REQUIS) {
        tok = (window.prompt("Token d'administration requis pour cette action :") || "").trim();
        if (tok) {
          try { localStorage.setItem("betpulse_admin_token", tok); } catch (e) {}
        }
      }
      return tok;
    }

    async function soumettreRefresh(ev) {
      if (ev) ev.preventDefault();
      const btn = document.getElementById("btn-refresh");
      const tok = obtenirTokenAdmin();
      const hiddenTok = document.getElementById("refresh-admin-token");
      if (hiddenTok) hiddenTok.value = tok;

      if (btn) {
        btn.disabled = true;
        btn.innerText = "Actualisation…";
      }
      afficherBanniere("");

      try {
        const headers = { "Accept": "application/json" };
        if (tok) headers["X-Admin-Token"] = tok;
        const body = new URLSearchParams();
        if (tok) body.set("admin_token", tok);

        const res = await fetch("/refresh", {
          method: "POST",
          headers: headers,
          body: body
        });

        if (res.status === 429) {
          const retryAfter = res.headers.get("Retry-After") || "quelques secondes";
          afficherBanniere(`Actualisation limitée (rate-limit) : réessayez dans ${retryAfter}s.`);
          if (btn) { btn.disabled = false; btn.innerText = "↻ Actualiser"; }
          return false;
        }
        if (res.status === 401 || res.status === 403) {
          try { localStorage.removeItem("betpulse_admin_token"); } catch (e) {}
          afficherBanniere("Accès refusé : token d'administration invalide.");
          if (btn) { btn.disabled = false; btn.innerText = "↻ Actualiser"; }
          return false;
        }
        window.location.replace(window.location.href);
      } catch (e) {
        if (btn) { btn.disabled = false; btn.innerText = "↻ Actualiser"; }
        afficherBanniere("Erreur réseau lors de l'actualisation.");
      }
      return false;
    }

    function setConf(val, btn) {
      confActif = val;
      document.querySelectorAll(".pill-btn").forEach(b => b.classList.remove("active"));
      if (btn) btn.classList.add("active");
      appliquerFiltres();
    }

    function setFavGroupFilter(mode, btn) {
      favGroupFilter = mode;
      document.querySelectorAll("#fav-filter-all, #fav-filter-actifs").forEach(b => {
        b.classList.toggle("active", b === btn);
      });
      appliquerFiltres();
    }

    function rafraichirCompteursFavoris() {
      const allFavCards = Array.from(document.querySelectorAll("#favoris-container .card[data-fav-card]"));
      const liveCards = allFavCards.filter(c => c.dataset.favState === "EN_COURS");
      const upCards = allFavCards.filter(c => c.dataset.favState === "A_VENIR");
      const doneCards = allFavCards.filter(c => c.dataset.favState === "TERMINE");

      const setTxt = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
      setTxt("kpi-fav-total", allFavCards.length);
      setTxt("kpi-fav-live", liveCards.length);
      setTxt("kpi-fav-upcoming", upCards.length);
      setTxt("kpi-fav-done", doneCards.length);
      setTxt("kpi-fav-done-btn", doneCards.length);
      setTxt("cnt-fav-live", liveCards.length);
      setTxt("cnt-fav-upcoming", upCards.length);
      setTxt("cnt-fav-done", doneCards.length);
      setTxt("fav-count-tab", allFavCards.length);

      const btnClear = document.getElementById("btn-clear-done");
      if (btnClear) btnClear.disabled = (doneCards.length === 0);

      const secLive = document.getElementById("fav-sec-live");
      const secUp = document.getElementById("fav-sec-upcoming");
      const secDone = document.getElementById("fav-sec-done");

      if (secLive) {
        secLive.style.display = (liveCards.length > 0 && favGroupFilter !== "TERMINE") ? "" : "none";
      }
      if (secUp) {
        secUp.style.display = (upCards.length > 0 && favGroupFilter !== "TERMINE") ? "" : "none";
      }
      if (secDone) {
        secDone.style.display = (doneCards.length > 0 && favGroupFilter !== "ACTIFS") ? "" : "none";
      }

      const container = document.getElementById("favoris-container");
      if (container && allFavCards.length === 0 && !document.getElementById("fav-empty-msg")) {
        const emptyDiv = document.createElement("div");
        emptyDiv.className = "fav-empty-box";
        emptyDiv.id = "fav-empty-msg";
        emptyDiv.innerHTML = "<strong>Aucun match dans votre liste de favoris</strong>Cliquez sur ☆ sur n'importe quel match pour l'épingler ici.";
        container.appendChild(emptyDiv);
      } else if (allFavCards.length > 0) {
        const emptyMsg = document.getElementById("fav-empty-msg");
        if (emptyMsg) emptyMsg.remove();
      }
    }

    function construireCarteFavoriElement(item) {
      const etat = String(item.etat_match || "A_VENIR").toUpperCase();
      const conf = String(item.confiance || "MOYENNE").toUpperCase();
      const confSc = Number(item.confiance_score ?? item.proba ?? 50) || 50;
      const tsM = Number(item.ts_match ?? 9999999999) || 9999999999;
      const sport = String(item.sport || "FOOT").toUpperCase();
      const icone = sport === "FOOT" ? "⚽" : "🎾";
      const cle = String(item.cle_favori || "");
      const match = String(item.match || "Match");
      const comp = String(item.competition || sport);
      const dateMatch = String(item.date_match || "");
      const choix = String(item.choix || "Non défini");
      const proba = item.proba ?? "";
      const cote = item.cote ?? "";
      const miseU = item.mise_u ?? "";
      const detail = String(item.detail || "");
      const scoreActuel = String(item.score_actuel || "En cours");
      const chronoLive = String(item.chrono_live || "LIVE");
      const scoreFinal = String(item.score_final || "Terminé");
      const statutProno = String(item.statut_prono || "").toUpperCase();

      const card = document.createElement("div");
      card.className = `card ${etat === "EN_COURS" ? "live-card " : etat === "TERMINE" ? "termine-card " : ""}${conf}`;
      card.dataset.favCard = "1";
      card.dataset.favState = etat;
      card.dataset.cleFavori = cle;
      card.dataset.conf = conf;
      card.dataset.proba = proba !== "" ? String(proba) : "0";
      card.dataset.confScore = String(confSc);
      card.dataset.ts = String(tsM);
      card.dataset.search = `${choix} ${match} ${comp} ${scoreActuel} ${scoreFinal} ${detail}`.toLowerCase();

      let topBadgeHtml = `<span class="badge a-venir">À VENIR</span>`;
      if (etat === "EN_COURS") {
        topBadgeHtml = `<span class="badge live"><span class="dot-red"></span> ${escapeHtml(chronoLive)}</span>`;
      } else if (etat === "TERMINE") {
        let pronoBadge = "";
        if (statutProno === "GAGNE") pronoBadge = `<span class="badge gagne">GAGNÉ</span>`;
        else if (statutProno === "PERDU") pronoBadge = `<span class="badge perdu">PERDU</span>`;
        topBadgeHtml = `<span class="badge termine">TERMINÉ</span>${pronoBadge}`;
      }

      let middleHtml = "";
      if (etat === "EN_COURS") {
        middleHtml = `
          <div class="live-score-box">
            <span class="meta">Score en direct</span>
            <span class="live-score-val">${escapeHtml(scoreActuel)}</span>
          </div>`;
      } else if (etat === "TERMINE") {
        middleHtml = `
          <div class="final-score-box">
            <span class="meta">Score final</span>
            <span class="live-score-val">${escapeHtml(scoreFinal)}</span>
          </div>`;
      }

      const subParts = [];
      if (proba !== "") subParts.push(`Proba : <strong>${escapeHtml(proba)}%</strong>`);
      if (etat === "EN_COURS" && cote !== "") subParts.push(`Cote : <strong>${escapeHtml(cote)}</strong>`);
      if (etat !== "EN_COURS") subParts.push(`Confiance : <strong>${escapeHtml(conf)}</strong>`);

      const footerHtml = etat !== "TERMINE" ? `
        <div class="fav-slip-footer">
          <span>${etat === "EN_COURS" ? "Mise suggérée" : "Mise"} : <strong>${miseU !== "" ? escapeHtml(miseU) + "u" : "-"}</strong></span>
          ${cote !== "" ? `<span class="odds-chip">@ ${escapeHtml(cote)}</span>` : ""}
        </div>` : "";

      card.innerHTML = `
        <div class="card-top">
          <div class="card-title">${icone} ${escapeHtml(match)}</div>
          <div class="badges-right">
            ${topBadgeHtml}
            <button type="button" class="star-btn active" title="Retirer des favoris" onclick="toggleFavori(this)"
                    data-cle-favori="${escapeHtml(cle)}"
                    data-sport="${escapeHtml(sport)}"
                    data-event-id="${escapeHtml(item.event_id || "")}"
                    data-match="${escapeHtml(match)}"
                    data-competition="${escapeHtml(comp)}"
                    data-date-match="${escapeHtml(dateMatch)}"
                    data-ts-match="${escapeHtml(tsM)}"
                    data-choix="${escapeHtml(choix)}"
                    data-proba="${escapeHtml(proba)}"
                    data-cote="${escapeHtml(cote)}"
                    data-mise-u="${escapeHtml(miseU)}"
                    data-confiance="${escapeHtml(conf)}"
                    data-confiance-score="${escapeHtml(confSc)}"
                    data-detail="${escapeHtml(detail)}"
                    data-etat-match="${escapeHtml(etat)}">★</button>
          </div>
        </div>
        <div class="card-meta">${escapeHtml(comp)} • ${escapeHtml(dateMatch)}</div>
        ${middleHtml}
        <div class="pred-box">
          <div class="pred-label">${etat === "TERMINE" ? "Pronostic" : "Sélection suivie"}</div>
          <div class="pred-choice">${escapeHtml(choix)}</div>
          <div class="pred-sub">${subParts.join(" • ")}</div>
        </div>
        ${footerHtml}
      `;
      return card;
    }

    function synchroniserCarteFavoriDom(item, estFav) {
      if (!PAGE_FAVORIS) return;
      const cle = String(item.cle_favori || "");
      if (!cle) return;

      const existante = document.querySelector(`#favoris-container .card[data-fav-card][data-cle-favori="${CSS.escape(cle)}"]`);
      if (!estFav) {
        if (existante) existante.remove();
        rafraichirCompteursFavoris();
        return;
      }

      const etat = String(item.etat_match || "A_VENIR").toUpperCase();
      const grilleId = etat === "EN_COURS" ? "fav-grid-live" : (etat === "TERMINE" ? "fav-grid-done" : "fav-grid-upcoming");
      const grille = document.getElementById(grilleId);
      if (!grille) return;

      const nouvelleCarte = construireCarteFavoriElement(item);
      if (existante) {
        existante.replaceWith(nouvelleCarte);
      } else {
        grille.appendChild(nouvelleCarte);
      }
      rafraichirCompteursFavoris();
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
        const favState = card.getAttribute("data-fav-state") || "";

        let okGroup = true;
        if (PAGE_FAVORIS && favState) {
          if (favGroupFilter === "ACTIFS") okGroup = (favState !== "TERMINE");
          if (favGroupFilter === "TERMINE") okGroup = (favState === "TERMINE");
        }

        const okConf = (confActif === "ALL") || (cConf === confActif);
        const okProba = cProba >= probaMin;
        const okText = !q || cText.includes(q);
        const show = okGroup && okConf && okProba && okText;
        card.style.display = show ? "" : "none";
        if (show) visibles += 1;
      });

      const cpt = document.getElementById("compteur-cartes");
      if (cpt && totalCartes > 0) {
        cpt.textContent = `${visibles}/${totalCartes} affichés`;
      }
    }

    function trierCartes() {
      const mode = document.getElementById("sort-mode")?.value || "date";
      document.querySelectorAll(".grid").forEach(grid => {
        const cards = Array.from(grid.querySelectorAll(".card[data-conf]"));
        if (cards.length < 2) return;
        cards.sort((a, b) => {
          if (mode === "proba") {
            return (parseFloat(b.dataset.proba) || 0) - (parseFloat(a.dataset.proba) || 0);
          }
          if (mode === "conf") {
            return (parseFloat(b.dataset.confScore) || 0) - (parseFloat(a.dataset.confScore) || 0);
          }
          const tsA = parseFloat(a.dataset.ts);
          const tsB = parseFloat(b.dataset.ts);
          const numA = Number.isFinite(tsA) ? tsA : Number.MAX_SAFE_INTEGER;
          const numB = Number.isFinite(tsB) ? tsB : Number.MAX_SAFE_INTEGER;
          return numA - numB;
        });
        cards.forEach(c => grid.appendChild(c));
      });
    }

    async function toggleFavori(btn) {
      if (!btn) return;
      const d = btn.dataset;
      const payload = {
        cle_favori: d.cleFavori || "",
        sport: d.sport || "FOOT",
        event_id: d.eventId || "",
        match: d.match || "",
        competition: d.competition || "",
        date_match: d.dateMatch || "",
        ts_match: d.tsMatch || "",
        choix: d.choix || "",
        proba: d.proba || null,
        cote: d.cote || null,
        mise_u: d.miseU || null,
        confiance: d.confiance || "MOYENNE",
        confiance_score: d.confianceScore || 50,
        detail: d.detail || "",
        etat_match: d.etatMatch || "A_VENIR",
        score_actuel: d.scoreActuel || "",
        chrono_live: d.chronoLive || ""
      };
      btn.disabled = true;
      try {
        const res = await fetch("/api/favoris/toggle", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        if (res.ok) {
          const data = await res.json();
          const estFav = Boolean(data.est_favori);
          const cle = data.cle_favori || payload.cle_favori;
          const itemSync = data.item || { ...payload, cle_favori: cle, est_favori: estFav };

          document.querySelectorAll(".star-btn").forEach(other => {
            if ((other.dataset.cleFavori || "") === cle) {
              other.classList.toggle("active", estFav);
              other.textContent = estFav ? "★" : "☆";
              other.title = estFav ? "Retirer des favoris" : "Ajouter aux favoris";
            }
          });

          if (typeof data.total_favoris === "number") {
            const tCpt = document.getElementById("fav-count-tab");
            if (tCpt) tCpt.textContent = data.total_favoris;
          }

          synchroniserCarteFavoriDom(itemSync, estFav);
          trierCartes();
          appliquerFiltres();
        }
      } catch (e) {
        afficherBanniere("Impossible de mettre à jour les favoris pour le moment.");
      } finally {
        btn.disabled = false;
      }
    }

    async function nettoyerTermines() {
      const btn = document.getElementById("btn-clear-done");
      if (btn) btn.disabled = true;
      try {
        const res = await fetch("/api/favoris/clear-done", { method: "POST" });
        if (res.ok) {
          const data = await res.json();
          document.querySelectorAll('#favoris-container .card[data-fav-state="TERMINE"]').forEach(c => c.remove());
          if (typeof data.total_favoris === "number") {
            const tCpt = document.getElementById("fav-count-tab");
            if (tCpt) tCpt.textContent = data.total_favoris;
          }
          rafraichirCompteursFavoris();
          trierCartes();
          appliquerFiltres();
          if ((data.supprimes || 0) > 0) {
            afficherBanniere(`${data.supprimes} match(s) terminé(s) retiré(s) des favoris.`, "ok");
          }
        }
      } catch (e) {
        afficherBanniere("Erreur lors du nettoyage des matchs terminés.");
      } finally {
        rafraichirCompteursFavoris();
      }
    }

    trierCartes();
    if (PAGE_FAVORIS) {
      const allBtn = document.getElementById("fav-filter-all");
      if (allBtn) allBtn.classList.add("active");
      rafraichirCompteursFavoris();
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
        rapport = _annoter_favoris_dans_rapport(_DERNIER_RAPPORT)
    if not rapport.get("mis_a_jour"):
        rapport = executer_analyse()
    return jsonify(rapport)


@app.route("/api/favoris/toggle", methods=["POST"])
def api_favoris_toggle():
    payload = request.get_json(silent=True) or request.form or {}
    force_raw = payload.get("favori")
    force_etat = None
    if isinstance(force_raw, bool):
        force_etat = force_raw
    elif isinstance(force_raw, str) and force_raw.strip().lower() in ("1", "true", "yes", "on"):
        force_etat = True
    elif isinstance(force_raw, str) and force_raw.strip().lower() in ("0", "false", "no", "off"):
        force_etat = False

    est_fav, cles_actuelles = basculer_favori(payload, force_etat=force_etat)
    cle = str(payload.get("cle_favori") or "").strip() or _construire_cle_favori(
        payload.get("sport"), payload.get("event_id"), payload.get("match"), payload.get("date_match")
    )

    item_enrichi = None
    with _STATE_LOCK:
        if _DERNIER_RAPPORT.get("mis_a_jour"):
            suivis_actuels = list(_DERNIER_RAPPORT.get("favoris_suivis") or [])
            index_ref = {}
            for liste_k in ("en_cours_matchs", "top_sur", "foot", "tennis"):
                for ref_it in (_DERNIER_RAPPORT.get(liste_k) or []):
                    ref_cle = str(ref_it.get("cle_favori") or "").strip() or _construire_cle_favori(
                        ref_it.get("sport"), ref_it.get("event_id"), ref_it.get("match"), ref_it.get("date_match")
                    )
                    if ref_cle:
                        index_ref[ref_cle] = ref_it

            if est_fav:
                with _FAV_LOCK:
                    meta = dict(_FAVORIS_ACTIFS.get(cle) or payload or {})
                source_it = dict(index_ref.get(cle) or meta)
                source_it.update({k: v for k, v in meta.items() if v not in (None, "")})
                source_it["cle_favori"] = cle
                source_it["est_favori"] = True
                source_it.setdefault("etat_match", "EN_COURS" if source_it.get("en_cours") else "A_VENIR")
                item_enrichi = dict(source_it)

                remplace = False
                for idx_s, existing_s in enumerate(suivis_actuels):
                    ex_cle = str(existing_s.get("cle_favori") or "").strip() or _construire_cle_favori(
                        existing_s.get("sport"), existing_s.get("event_id"), existing_s.get("match"), existing_s.get("date_match")
                    )
                    if ex_cle == cle:
                        merged_s = dict(existing_s)
                        merged_s.update(source_it)
                        suivis_actuels[idx_s] = merged_s
                        item_enrichi = dict(merged_s)
                        remplace = True
                        break
                if not remplace:
                    suivis_actuels.append(source_it)

                ordre_etat = {"EN_COURS": 0, "A_VENIR": 1, "TERMINE": 2}
                suivis_actuels.sort(key=lambda x: (
                    ordre_etat.get(str(x.get("etat_match") or "A_VENIR").upper(), 1),
                    _cle_cle_chronologique(x),
                ))
                _DERNIER_RAPPORT["favoris_suivis"] = suivis_actuels
            else:
                for ex_s in suivis_actuels:
                    ex_cle = str(ex_s.get("cle_favori") or "").strip() or _construire_cle_favori(
                        ex_s.get("sport"), ex_s.get("event_id"), ex_s.get("match"), ex_s.get("date_match")
                    )
                    if ex_cle == cle:
                        item_enrichi = dict(ex_s, est_favori=False)
                        break

            _DERNIER_RAPPORT = _annoter_favoris_dans_rapport(_DERNIER_RAPPORT)
            rapport_copie = dict(_DERNIER_RAPPORT)
        else:
            rapport_copie = None

    if rapport_copie and rapport_copie.get("mis_a_jour"):
        _sauvegarder_rapport_disque(rapport_copie)

    if item_enrichi is None:
        with _FAV_LOCK:
            meta_fallback = dict(_FAVORIS_ACTIFS.get(cle) or payload or {})
        meta_fallback["cle_favori"] = cle
        meta_fallback["est_favori"] = est_fav
        meta_fallback.setdefault("etat_match", "A_VENIR")
        item_enrichi = meta_fallback

    return jsonify({
        "ok": True,
        "cle_favori": cle,
        "est_favori": est_fav,
        "total_favoris": len(cles_actuelles),
        "item": item_enrichi,
    }), 200


@app.route("/api/favoris/clear-done", methods=["POST"])
def api_favoris_clear_done():
    supprimes, total_restants = nettoyer_favoris_termines()
    with _STATE_LOCK:
        if _DERNIER_RAPPORT.get("mis_a_jour"):
            _DERNIER_RAPPORT = _annoter_favoris_dans_rapport(_DERNIER_RAPPORT)
            rapport_copie = dict(_DERNIER_RAPPORT)
        else:
            rapport_copie = None

    if rapport_copie and rapport_copie.get("mis_a_jour"):
        _sauvegarder_rapport_disque(rapport_copie)

    return jsonify({
        "ok": True,
        "supprimes": supprimes,
        "total_favoris": total_restants,
    }), 200


@app.route("/api/historique/statut", methods=["POST"])
def api_historique_statut():
    payload = request.get_json(silent=True) or request.form or {}
    refus = _verifier_acces_admin("/api/historique/statut", CFG.statut_rate_limit_sec, payload=payload, json_mode=True)
    if refus is not None:
        return refus

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
    payload = request.get_json(silent=True) or request.form or {}
    wants_json = request.is_json or ("application/json" in (request.headers.get("Accept") or ""))
    refus = _verifier_acces_admin("/refresh", CFG.refresh_rate_limit_sec, payload=payload, json_mode=wants_json)
    if refus is not None:
        return refus

    _demarrer_analyse_si_besoin(0.0)
    if wants_json:
        return jsonify({"ok": True, "message": "Analyse lancée"}), 202
    return redirect(url_for("app_view"))


@app.route("/")
def index():
    with _STATE_LOCK:
        rapport = dict(_DERNIER_RAPPORT)
        en_cours = _ANALYSE_EN_COURS

    maj = rapport.get("mis_a_jour")
    pret = bool(maj)

    if not pret and not en_cours:
        _demarrer_analyse_si_besoin(0.2)

    resp = make_response(render_template_string(
        _HTML_HOME,
        pret=pret,
        mis_a_jour=maj,
        en_cours=en_cours,
        fenetre_heures=CFG.fenetre_heures,
    ), 200)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/favoris")
def favoris_view():
    with _STATE_LOCK:
        rapport = _annoter_favoris_dans_rapport(_DERNIER_RAPPORT)
        en_cours = _ANALYSE_EN_COURS

    if not rapport.get("mis_a_jour"):
        if not en_cours:
            _demarrer_analyse_si_besoin(0.0)
        resp = make_response(render_template_string(_HTML_LOADING), 200)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return resp

    resp = make_response(render_template_string(
        _HTML_PAGE,
        r=rapport,
        cat="favoris",
        page_favoris=True,
        en_cours=en_cours,
        fenetre_heures=CFG.fenetre_heures,
        token_requis=bool(CFG.admin_token),
    ), 200)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/app")
def app_view():
    cat = request.args.get("cat", "all")
    if cat not in ("all", "favoris", "live", "top", "foot", "tennis", "alertes", "bilan"):
        cat = "all"

    with _STATE_LOCK:
        rapport = _annoter_favoris_dans_rapport(_DERNIER_RAPPORT)
        en_cours = _ANALYSE_EN_COURS

    if not rapport.get("mis_a_jour"):
        if not en_cours:
            _demarrer_analyse_si_besoin(0.0)
        resp = make_response(render_template_string(_HTML_LOADING), 200)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return resp

    resp = make_response(render_template_string(
        _HTML_PAGE,
        r=rapport,
        cat=cat,
        page_favoris=(cat == "favoris"),
        en_cours=en_cours,
        fenetre_heures=CFG.fenetre_heures,
        token_requis=bool(CFG.admin_token),
    ), 200)
    resp.headers["Cache-Control"] = "no-store"
    return resp


_nettoyer_fichiers_tmp_orphelins()
_charger_favoris_disque()
_charger_rapport_disque()
_demarrer_scheduler_si_actif()

if not _DERNIER_RAPPORT.get("mis_a_jour"):
    _demarrer_analyse_si_besoin(0.5)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
