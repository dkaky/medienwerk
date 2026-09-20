"""Dashboard-Login (Passwortschutz für Betrieb auf Server/VPS).

Aktivierung über `DASHBOARD_PASSWORD` in der .env. Ohne gesetztes Passwort ist
die Auth komplett aus (lokale Entwicklung + Tests laufen unverändert).

Öffentlich bleiben nur Pfade, die von außen erreichbar sein MÜSSEN:
* /health – Monitoring/Uptime-Checks
* /login – die Login-Seite selbst

Session = signiertes Cookie (HMAC-SHA256 über den Ablaufzeitpunkt). Der Schlüssel
wird aus dem Passwort abgeleitet -> Passwortwechsel in der .env invalidiert
automatisch alle bestehenden Sessions. Kein Session-Store nötig.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings

logger = logging.getLogger("app.auth")

COOKIE_NAME = "podshop_session"
SESSION_TTL_SECONDS = 30 * 24 * 3600  # 30 Tage angemeldet bleiben

# Exakte Pfad-Matches (Trailing-Slash wird vorher normalisiert).
PUBLIC_PATHS = {
    "/api/v1/version",
    "/logo-banner.jpg",
    "/logo.png",
    "/design.css",
    "/logo-hell.png",
    "/ebay/marketplace-account-deletion",
    "/api/v1/invoices/originals/abruf-signal",
    "/favicon.svg",
    "/favicon.ico",
    "/login",
    "/health",
}

# Brute-Force-Bremse: je IP nach _MAX_ATTEMPTS Fehlversuchen _LOCK_SECONDS sperren.
_MAX_ATTEMPTS = 5
_LOCK_SECONDS = 15 * 60
_failed: dict[str, tuple[int, float]] = {}  # ip -> (fehlversuche, gesperrt_bis)


def _secret() -> bytes:
    """Signierschlüssel, deterministisch aus dem Dashboard-Passwort abgeleitet."""
    cfg = get_settings()
    pw = cfg.dashboard_password
    basis = f"ma-session-v1:{cfg.dashboard_user}:{pw}" if cfg.dashboard_user else f"ma-session-v1:{pw}"
    return hashlib.sha256(basis.encode("utf-8")).digest()


def issue_token(now: float | None = None) -> str:
    exp = int((now if now is not None else time.time()) + SESSION_TTL_SECONDS)
    sig = hmac.new(_secret(), str(exp).encode("ascii"), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def verify_token(token: str) -> bool:
    # Cookie-Werte kommen latin-1-dekodiert an; Non-ASCII darf nie zum 500 fuehren.
    # Deshalb: Bytes-Vergleich + kompletter Pruefpfad im try (jeder Fehler = False).
    try:
        exp_str, sig = token.split(".", 1)
        exp = int(exp_str)
        expected = hmac.new(_secret(), exp_str.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig.encode("utf-8"), expected.encode("ascii")):
            return False
        return exp >= time.time()
    except (ValueError, TypeError, AttributeError):
        return False


class AuthMiddleware(BaseHTTPMiddleware):
    """Schützt alle Routen außer PUBLIC_PATHS, sobald ein Passwort gesetzt ist."""

    async def dispatch(self, request: Request, call_next):
        if not get_settings().dashboard_password:
            return await call_next(request)

        path = request.url.path.rstrip("/") or "/"
        if path in PUBLIC_PATHS:
            return await call_next(request)

        # Belege-Backfill-Client: NUR die /originals-Endpoints per Token-Header
        # (X-Backfill-Token) statt Login-Session – der lokale Playwright-Client hat
        # keinen Browser-Login. Streng auf diese Pfade begrenzt (kein Voll-API-Zugang
        # bei geleaktem Token).
        if path.startswith("/api/v1/invoices/originals"):
            bf = get_settings().backfill_token
            if bf and hmac.compare_digest(
                    request.headers.get("x-backfill-token", "").encode("utf-8"),
                    bf.encode("utf-8")):
                return await call_next(request)

        token = request.cookies.get(COOKIE_NAME, "")
        if token and verify_token(token):
            return await call_next(request)

        # Browser zur Login-Seite schicken, API-Aufrufer bekommen sauberes 401.
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"detail": "Nicht angemeldet"}, status_code=401)


router = APIRouter(include_in_schema=False)


@router.get("/login")
def login_page():
    if not get_settings().dashboard_password:
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(_render_login())


@router.post("/login")
async def login(request: Request, password: str = Form(""), benutzer: str = Form("")):
    s = get_settings()
    if not s.dashboard_password:
        return RedirectResponse("/", status_code=303)

    ip = request.client.host if request.client else "unknown"
    now = time.time()
    attempts, locked_until = _failed.get(ip, (0, 0.0))
    if locked_until > now:
        minutes = int((locked_until - now) / 60) + 1
        logger.warning("login blocked (rate limit)", extra={"ip": ip})
        return HTMLResponse(
            _render_login(f"Zu viele Fehlversuche – gesperrt für ca. {minutes} Minuten."),
            status_code=429,
        )

    passwort_ok = hmac.compare_digest(password.encode("utf-8"), s.dashboard_password.encode("utf-8"))
    # Beides wird IMMER verglichen, damit die Antwortzeit nicht verraet, welcher Teil stimmte.
    benutzer_ok = (not s.dashboard_user) or hmac.compare_digest(
        benutzer.strip().encode("utf-8"), s.dashboard_user.encode("utf-8"))
    if passwort_ok and benutzer_ok:
        _failed.pop(ip, None)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            issue_token(),
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            # Hinter HTTPS (VPS mit Proxy: uvicorn --proxy-headers) wird das
            # Cookie als "secure" markiert; lokal über http bleibt es nutzbar.
            secure=request.url.scheme == "https",
            path="/",
        )
        logger.info("login ok", extra={"ip": ip})
        return response

    attempts += 1
    locked = now + _LOCK_SECONDS if attempts >= _MAX_ATTEMPTS else 0.0
    _failed[ip] = (attempts, locked)
    logger.warning("login failed", extra={"ip": ip, "attempts": attempts})
    remaining = max(_MAX_ATTEMPTS - attempts, 0)
    was = "Benutzername oder Passwort falsch." if s.dashboard_user else "Falsches Passwort."
    msg = was
    if remaining:
        msg += f" Noch {remaining} Versuch{'e' if remaining != 1 else ''}."
    else:
        msg = f"{was} Zugang für 15 Minuten gesperrt."
    return HTMLResponse(_render_login(msg), status_code=401)


@router.get("/logout")
def logout(request: Request):
    response = RedirectResponse("/login", status_code=303)
    # Das Session-Cookie wurde mit secure/samesite/path gesetzt (auf HTTPS als "Secure").
    # delete_cookie OHNE diese Attribute loescht es auf dem HTTPS-VPS NICHT -> man blieb
    # eingeloggt. Darum mit EXAKT denselben Attributen als abgelaufen ueberschreiben.
    response.set_cookie(
        COOKIE_NAME, "", max_age=0, expires=0,
        httponly=True, samesite="lax",
        secure=request.url.scheme == "https", path="/",
    )
    return response


_LOGIN_HTML = """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Anmelden · Medienwerk</title>
  <link rel="icon" type="image/svg+xml" href="/favicon.svg?v=7">
  <link rel="icon" type="image/x-icon" href="/favicon.ico?v=7" sizes="any">
  <link rel="stylesheet" href="/design.css?v=1">
  <style>
    .anmeldung { min-height: 100vh; display: grid; place-items: center; padding: 24px; }
    .anmeldung .karte-login { width: min(100%, 360px); padding: 32px; background: var(--card);
      border: 1px solid var(--line); border-radius: var(--radius); }
    .anmeldung .logo-bild { margin: 0 auto 8px; max-width: 200px; }
    .anmeldung p.unter { text-align: center; color: var(--muted); margin-bottom: 24px; }
    .anmeldung .btn { width: 100%; margin-top: 12px; padding: 9px 14px; }
    .anmeldung .fehler { margin-bottom: 16px; padding: 10px 12px; border-left: 3px solid var(--bad);
      background: var(--bg-soft); color: var(--bad); border-radius: 0 6px 6px 0; }
  </style>
</head>
<body>
  <script>
    (function () {
      var w = null;
      try { w = localStorage.getItem("podshop-farbschema"); } catch (e) {}
      if (w !== "dunkel") document.body.classList.add("light");
    })();
  </script>
  <div class="anmeldung">
    <div class="karte-login">
      <img class="logo-bild logo-dunkelgrund" src="/logo-hell.png" alt="Medienwerk">
      <img class="logo-bild logo-hellgrund" src="/logo.png" alt="Medienwerk">
      <p class="unter">Interner Zugang</p>
      <!--ERROR-->
      <form method="post" action="/login">
        <!--USER-->
        <input type="password" name="password" placeholder="Passwort" autofocus autocomplete="current-password">
        <button class="btn primary" type="submit">Anmelden</button>
      </form>
    </div>
  </div>
</body>
</html>"""


def _render_login(error: str | None = None) -> str:
    error_html = f'<p class="fehler">{error}</p>' if error else ""
    benutzer_html = ('<input type="text" name="benutzer" placeholder="Benutzername" autocomplete="username" '
                     'style="margin-bottom:8px" autofocus>' if get_settings().dashboard_user else "")
    seite = _LOGIN_HTML.replace("<!--ERROR-->", error_html).replace("<!--USER-->", benutzer_html)
    if benutzer_html:
        seite = seite.replace('placeholder="Passwort" autofocus', 'placeholder="Passwort"')
    return seite
