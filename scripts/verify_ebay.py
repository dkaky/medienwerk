"""Verbindungstest gegen die ECHTE eBay-Sell-API.

Nutzt die Credentials aus der .env (EBAY_CLIENT_ID/SECRET/REFRESH_TOKEN), holt per
refresh_token-Grant einen User-Access-Token und macht einen leichten, lesenden
API-Call. Gibt klar PASS/FAIL mit Diagnose aus – ohne irgendetwas zu veraendern.

Aufruf (aus dem Projektordner):
    .venv\\Scripts\\python.exe -m scripts.verify_ebay
"""
from __future__ import annotations

import asyncio
import base64

import httpx

from app.config import get_settings

_SCOPES = (
    "https://api.ebay.com/oauth/api_scope/sell.inventory "
    "https://api.ebay.com/oauth/api_scope/sell.analytics.readonly "
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment"
)


def _host(sandbox: bool) -> str:
    return "https://api.sandbox.ebay.com" if sandbox else "https://api.ebay.com"


async def _get_token(s) -> tuple[str | None, dict]:
    host = _host(s.ebay_use_sandbox)
    basic = base64.b64encode(f"{s.ebay_client_id}:{s.ebay_client_secret}".encode()).decode()
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(
            f"{host}/identity/v1/oauth2/token",
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token",
                  "refresh_token": s.ebay_refresh_token,
                  "scope": _SCOPES},
        )
    body = r.json() if r.content else {}
    if r.status_code != 200 or "access_token" not in body:
        return None, {"status": r.status_code, **body}
    return body["access_token"], body


async def _test_call(s, token: str) -> dict:
    host = _host(s.ebay_use_sandbox)
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.get(
            f"{host}/sell/inventory/v1/inventory_item",
            params={"limit": 1},
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/json",
                     "X-EBAY-C-MARKETPLACE-ID": s.ebay_marketplace_id},
        )
    return {"status": r.status_code,
            "body": (r.json() if r.content else {})}


def _line(label: str, value: str) -> None:
    print(f"  {label:<22} {value}")


async def main() -> int:
    s = get_settings()
    print("=" * 60)
    print(" eBay-Verbindungstest")
    print("=" * 60)
    _line("Umgebung", "SANDBOX" if s.ebay_use_sandbox else "PRODUCTION")
    _line("Marketplace", s.ebay_marketplace_id)
    _line("Client-ID gesetzt", "ja" if s.ebay_client_id else "NEIN")
    _line("Client-Secret gesetzt", "ja" if s.ebay_client_secret else "NEIN")
    _line("Refresh-Token gesetzt", "ja" if s.ebay_refresh_token else "NEIN")
    print("-" * 60)

    if not (s.ebay_client_id and s.ebay_client_secret and s.ebay_refresh_token):
        print("FAIL: Es fehlen Credentials in der .env (siehe oben).")
        return 1

    print("1) Hole User-Access-Token (refresh_token-Grant) ...")
    token, info = await _get_token(s)
    if token is None:
        print("   FAIL: Token konnte nicht geholt werden.")
        _line("HTTP", str(info.get("status")))
        _line("Fehler", str(info.get("error") or info))
        hint = info.get("error")
        if hint == "invalid_grant":
            print("   Hinweis: Refresh-Token ungueltig/abgelaufen oder fuer falsche "
                  "Umgebung (Sandbox vs. Production).")
        elif hint == "invalid_client":
            print("   Hinweis: Client-ID/Secret passen nicht (oder Sandbox/Prod vertauscht).")
        return 2
    print(f"   OK: Token erhalten (gueltig {info.get('expires_in', '?')} s).")

    print("2) Test-Call: getInventoryItems (limit=1) ...")
    res = await _test_call(s, token)
    status = res["status"]
    if status == 200:
        total = res["body"].get("total", 0)
        print(f"   OK: HTTP 200 – Sell-Inventory erreichbar (vorhandene Items: {total}).")
        print("-" * 60)
        print("PASS: eBay-Production-Verbindung steht.")
        return 0

    print(f"   Unerwartet: HTTP {status}")
    body = res["body"]
    errs = body.get("errors") or []
    if errs:
        e = errs[0]
        _line("eBay-Fehler", f"{e.get('errorId')}: {e.get('message')}")
        if "scope" in str(e).lower() or e.get("errorId") in (1100, 1001):
            print("   Hinweis: Dem Token fehlen Sell-Scopes – Refresh-Token mit den "
                  "richtigen Scopes (sell.inventory/analytics/fulfillment) neu erzeugen.")
    else:
        _line("Body", str(body)[:300])
    print("-" * 60)
    print("FAIL: Token ok, aber API-Call nicht erfolgreich (siehe Hinweis).")
    return 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
