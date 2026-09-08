"""eBay-OAuth-Helfer: Authorisierungs-URL bauen + Code -> Refresh-Token tauschen.

Nutzt CLIENT_ID/SECRET aus der .env (app.config). Die RuName (eBay Redirect URL
name) wird als redirect_uri verwendet.

Ablauf:
  1) URL erzeugen und im Browser oeffnen, einloggen, zustimmen:
       .venv\\Scripts\\python.exe -m scripts.ebay_oauth authurl <RUNAME>
  2) eBay zeigt auf der Standard-Accept-Seite einen "authorization code".
     Diesen kopieren und tauschen:
       .venv\\Scripts\\python.exe -m scripts.ebay_oauth exchange <RUNAME> "<CODE>"
     -> gibt den Refresh-Token aus (kommt in EBAY_REFRESH_TOKEN).
"""
from __future__ import annotations

import base64
import sys
import urllib.parse

import httpx

from app.config import get_settings

_SCOPES = (
    "https://api.ebay.com/oauth/api_scope/sell.inventory "
    "https://api.ebay.com/oauth/api_scope/sell.analytics.readonly "
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment "
    "https://api.ebay.com/oauth/api_scope/sell.account "
    "https://api.ebay.com/oauth/api_scope/sell.finances "
    "https://api.ebay.com/oauth/api_scope/sell.marketing"
)


def _auth_host(sandbox: bool) -> str:
    return "https://auth.sandbox.ebay.com" if sandbox else "https://auth.ebay.com"


def _api_host(sandbox: bool) -> str:
    return "https://api.sandbox.ebay.com" if sandbox else "https://api.ebay.com"


def build_authurl(runame: str) -> str:
    s = get_settings()
    params = {
        "client_id": s.ebay_client_id,
        "response_type": "code",
        "redirect_uri": runame,
        "scope": _SCOPES,
        "prompt": "login",
    }
    return f"{_auth_host(s.ebay_use_sandbox)}/oauth2/authorize?" + urllib.parse.urlencode(params)


def exchange(runame: str, code: str) -> int:
    s = get_settings()
    code = urllib.parse.unquote(code).strip()  # aus der URL ist der Code oft encodiert
    basic = base64.b64encode(f"{s.ebay_client_id}:{s.ebay_client_secret}".encode()).decode()
    with httpx.Client(timeout=20.0) as c:
        r = c.post(
            f"{_api_host(s.ebay_use_sandbox)}/identity/v1/oauth2/token",
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": runame},
        )
    body = r.json() if r.content else {}
    if r.status_code != 200 or "refresh_token" not in body:
        print("FEHLER beim Token-Tausch:")
        print("  HTTP:", r.status_code)
        print("  Antwort:", body)
        if body.get("error") == "invalid_grant":
            print("  Hinweis: Code abgelaufen (nur ~5 min gueltig) oder schon benutzt – "
                  "Schritt 1 (authurl) neu machen und frischen Code holen.")
        return 2
    print("=" * 60)
    print(" ERFOLG – Refresh-Token erhalten")
    print("=" * 60)
    print("REFRESH-TOKEN (in .env als EBAY_REFRESH_TOKEN eintragen):")
    print()
    print(body["refresh_token"])
    print()
    print(f"  gueltig ca. {body.get('refresh_token_expires_in', '?')} s "
          f"(~{int(body.get('refresh_token_expires_in', 0)) // 86400} Tage)")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "authurl":
        print(build_authurl(argv[1]))
        return 0
    if len(argv) >= 3 and argv[0] == "exchange":
        return exchange(argv[1], argv[2])
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
