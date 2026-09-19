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
    pw = get_settings().dashboard_password
    return hashlib.sha256(f"ma-session-v1:{pw}".encode("utf-8")).digest()


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
async def login(request: Request, password: str = Form("")):
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

    if hmac.compare_digest(password.encode("utf-8"), s.dashboard_password.encode("utf-8")):
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
    msg = "Falsches Passwort."
    if remaining:
        msg += f" Noch {remaining} Versuch{'e' if remaining != 1 else ''}."
    else:
        msg = "Falsches Passwort. Zugang für 15 Minuten gesperrt."
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
  <!-- Fehlte bisher. Die Anmeldeseite ist das Erste, was man sieht -
       und die Seite, die man sich als Lesezeichen anlegt. -->
  <link rel="icon" type="image/svg+xml" href="/favicon.svg?v=7">
  <link rel="icon" type="image/x-icon" href="/favicon.ico?v=7" sizes="any">
  <style>
    :root{
      /* Dieselbe Palette wie das Dashboard: kuehles Anthrazit, Cyan als
         Markenfarbe. Vorher stand hier Flaschengruen mit Messing - die
         eine fremde Bildwelt. Die Anmeldeseite ist das Erste, was man sieht;
         sie darf nicht nach einem anderen Betrieb aussehen als der Rest.
         --gold heisst weiter so, traegt aber das Cyan: der Name steckt in
         einem Dutzend Regeln hier drunter. */
      --bg:#12161d; --edge:#333c49; --hair:#2a323d;
      --ink:#e6e9ef; --muted:#8b93a3; --gold:#19a7bd; --gold-soft:#3dc4d8;
      --serif:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
      --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
    }
    *{ box-sizing:border-box; margin:0; }
    html,body{ height:100%; }
    body{ font-family:var(--sans); color:var(--ink); background:var(--bg); }
    .scene{ position:fixed; inset:0; display:grid; place-items:center; overflow:hidden; padding:24px; }
    /* Ruhiger Verlauf mit einem Anklang des Marken-Rots statt des frueheren Fotos
       (logo-banner.jpg zeigt startende Militaerraketen). Ein Verlauf laedt zudem
       ohne zusaetzliche Anfrage - die Anmeldeseite steht sofort. */
    .scene .bg{ position:absolute; inset:0;
      background:
        radial-gradient(90% 70% at 22% 18%, rgba(25,167,189,.18) 0%, transparent 58%),
        radial-gradient(80% 65% at 82% 88%, rgba(25,167,189,.10) 0%, transparent 60%),
        linear-gradient(160deg, #1c232d 0%, #171c24 46%, #12161d 100%); }
    .scene .veil{ position:absolute; inset:0;
      background:
        radial-gradient(130% 95% at 50% 26%, rgba(23,28,36,.15) 0%, rgba(18,22,29,.70) 62%, rgba(15,18,24,.92) 100%),
        linear-gradient(180deg, rgba(18,22,29,.52) 0%, rgba(18,22,29,.32) 45%, rgba(18,22,29,.78) 100%); }
    .scene .grain{ position:absolute; inset:0; opacity:.5; background:linear-gradient(180deg, transparent, rgba(0,0,0,.25)); }
    .gate{ position:relative; z-index:2; width:min(94vw, 400px);
      background:linear-gradient(180deg, rgba(31,37,48,.92) 0%, rgba(23,28,36,.94) 100%);
      border:1px solid var(--edge); border-radius:18px;
      box-shadow:0 40px 90px -40px rgba(0,0,0,.9), inset 0 1px 0 rgba(25,167,189,.12);
      padding:34px 34px 26px; text-align:center; backdrop-filter:blur(3px); }
    .gate .frame{ position:absolute; inset:9px; border:1px solid var(--gold-soft); opacity:.32; border-radius:12px; pointer-events:none; }
    .crest{ width:66px; height:66px; color:var(--gold); margin:0 auto 14px; display:block; filter:drop-shadow(0 3px 8px rgba(0,0,0,.5)); }
    .kicker{ font-size:10.5px; letter-spacing:3px; text-transform:uppercase; color:var(--gold); font-weight:700; margin-bottom:8px; }
    h1{ font-family:var(--serif); font-weight:600; font-size:30px; letter-spacing:.4px; color:var(--ink); line-height:1.05; }
    .motto{ font-family:var(--serif); font-style:italic; font-size:16.5px; color:var(--gold-soft); margin-top:16px; line-height:1.35; }
    .motto-sub{ font-size:11.5px; color:var(--muted); margin-top:5px; letter-spacing:.3px; font-style:italic; }
    .rule{ display:flex; align-items:center; gap:12px; margin:22px 2px 20px; color:var(--gold-soft); }
    .rule::before,.rule::after{ content:""; height:1px; flex:1; background:linear-gradient(90deg,transparent,var(--hair),transparent); }
    .rule .star{ font-size:11px; opacity:.8; }
    input[type=password]{ width:100%; padding:13px 14px; border-radius:11px; font:inherit; font-size:15px;
      background:rgba(15,18,24,.6); border:1px solid var(--edge); color:var(--ink); outline:none; letter-spacing:2px; }
    input[type=password]::placeholder{ letter-spacing:normal; color:var(--muted); }
    input[type=password]:focus{ border-color:var(--gold-soft); box-shadow:0 0 0 3px rgba(25,167,189,.18); }
    button{ width:100%; margin-top:16px; padding:13px; border:0; border-radius:11px; cursor:pointer;
      background:linear-gradient(135deg, var(--gold), var(--gold-soft)); color:#04171b;
      font:inherit; font-size:14px; font-weight:700; letter-spacing:1px; text-transform:uppercase;
      transition:filter .12s, transform .05s; }
    button:hover{ filter:brightness(1.06); } button:active{ transform:translateY(1px); }
    .foot{ margin-top:20px; font-size:11px; color:var(--muted); letter-spacing:.4px; }
    .foot .em{ color:var(--gold-soft); }
    .err{ background:rgba(255,92,108,.12); border:1px solid rgba(255,92,108,.4); color:#ff9aa4;
      padding:10px 12px; border-radius:10px; font-size:12.5px; margin-bottom:14px; text-align:left; }
    @media(max-width:420px){ h1{ font-size:25px; } .gate{ padding:28px 24px 22px; } }
  </style>
</head>
<body>
  <div class="scene">
    <div class="bg"></div>
    <div class="veil"></div>
    <div class="grain"></div>
    <div class="gate">
      <div class="frame"></div>
      <svg class="crest" viewBox="0 0 100 100" aria-hidden="true">
        <circle cx="50" cy="50" r="40" fill="none" stroke="currentColor" stroke-width="6"/>
        <path d="M27 68V36h9v4c5-7 15-7 20 0 8-9 20-5 20 7v21H66V49c0-9-10-9-10 0v19H46V49c0-9-9-9-9 0v19z" fill="currentColor"/>
      </svg>
      <div class="kicker">Medienwerk</div>
      <h1>Medienwerk</h1>
      <div class="motto">Print on Demand</div>
      <div class="motto-sub">Eigene Entwürfe und Grafiken an einem Ort</div>
      <div class="rule"><span class="star">✦</span></div>
      <!--ERROR-->
      <form method="post" action="/login">
        <input type="password" name="password" placeholder="Passwort" autofocus autocomplete="current-password">
        <button type="submit">Anmelden</button>
      </form>
      <div class="foot">Interner Zugang &mdash; <span class="em">Medienwerk</span></div>
    </div>
  </div>
</body>
</html>"""


def _render_login(error: str | None = None) -> str:
    error_html = f'<p class="err">{error}</p>' if error else ""
    return _LOGIN_HTML.replace("<!--ERROR-->", error_html)
