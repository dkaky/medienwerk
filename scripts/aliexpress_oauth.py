"""AliExpress-OAuth-Helfer: Autorisierungs-URL bauen + Code -> access_token tauschen.

Die ds.*-Dropshipping-Methoden brauchen einen Konto-``access_token``. Ablauf:
  1) URL erzeugen, im Browser oeffnen, mit AliExpress-Konto autorisieren:
       .venv\\Scripts\\python.exe -m scripts.aliexpress_oauth authurl
     -> AliExpress leitet auf die Callback-URL um: ...?code=<CODE>
  2) Code tauschen:
       .venv\\Scripts\\python.exe -m scripts.aliexpress_oauth exchange "<CODE>"
     -> gibt access_token (+ refresh_token) aus -> in .env ALIEXPRESS_ACCESS_TOKEN.
"""
from __future__ import annotations

import asyncio
import sys

import httpx

from app.config import get_settings
from app.integrations import aliexpress_api as api


def _host_from_base(base: str) -> str:
    # "https://api-sg.aliexpress.com/sync" -> "https://api-sg.aliexpress.com"
    parts = base.split("/")
    return "/".join(parts[:3]) if len(parts) >= 3 else "https://api-sg.aliexpress.com"


def authurl() -> int:
    s = get_settings()
    if not s.aliexpress_callback_url:
        print("FEHLER: ALIEXPRESS_CALLBACK_URL fehlt in der .env.")
        return 1
    print(api.oauth_authorize_url(
        s.aliexpress_app_key, s.aliexpress_callback_url, host=_host_from_base(s.aliexpress_api_base)
    ))
    return 0


async def _exchange(code: str) -> int:
    s = get_settings()
    async with httpx.AsyncClient(timeout=20.0) as c:
        try:
            data = await api.call(
                c, base=s.aliexpress_api_base, method="/auth/token/create",
                business={"code": code}, app_key=s.aliexpress_app_key,
                app_secret=s.aliexpress_app_secret, sign_method=s.aliexpress_sign_method,
            )
        except api.AliExpressApiError as exc:
            print(f"FEHLER beim Token-Tausch: {exc}")
            if "code" in str(exc).lower():
                print("  Hinweis: Code abgelaufen/bereits benutzt – authurl neu aufrufen.")
            return 2
    tok = api.parse_token(data)
    if not tok["access_token"]:
        print("FEHLER: kein access_token in der Antwort:")
        print("  ", str(data)[:400])
        return 3
    api.save_token_store(s.aliexpress_token_file, access_token=tok["access_token"],
                         refresh_token=tok["refresh_token"], expires_in=tok["expires_in"])
    print("=" * 60)
    print(" ERFOLG – AliExpress access_token erhalten")
    print("=" * 60)
    print(f"Persistiert in: {s.aliexpress_token_file} (Auto-Refresh nutzt das automatisch)")
    print("\nALIEXPRESS_ACCESS_TOKEN (zusaetzlich in .env als Seed):")
    print("\n  " + str(tok["access_token"]) + "\n")
    if tok["refresh_token"]:
        print("ALIEXPRESS_REFRESH_TOKEN:")
        print("\n  " + str(tok["refresh_token"]) + "\n")
    if tok["expires_in"]:
        print(f"  gueltig: {tok['expires_in']} s")
    return 0


async def _refresh() -> int:
    """Manueller Refresh ueber den gespeicherten/Settings-refresh_token (Test/Notfall)."""
    s = get_settings()
    store = api.load_token_store(s.aliexpress_token_file) or {}
    rt = store.get("refresh_token") or s.aliexpress_refresh_token
    if not rt:
        print("FEHLER: kein refresh_token vorhanden.")
        return 1
    async with httpx.AsyncClient(timeout=20.0) as c:
        try:
            data = await api.call(c, base=s.aliexpress_api_base, method="/auth/token/refresh",
                                  business={"refresh_token": rt}, app_key=s.aliexpress_app_key,
                                  app_secret=s.aliexpress_app_secret,
                                  sign_method=s.aliexpress_sign_method)
        except api.AliExpressApiError as exc:
            print(f"FEHLER beim Refresh: {exc}")
            return 2
    tok = api.parse_token(data)
    if not tok["access_token"]:
        print("FEHLER: kein access_token:", str(data)[:300])
        return 3
    api.save_token_store(s.aliexpress_token_file, access_token=tok["access_token"],
                         refresh_token=tok["refresh_token"] or rt, expires_in=tok["expires_in"])
    print(f"OK: Token erneuert (gueltig {tok['expires_in']} s), gespeichert in {s.aliexpress_token_file}.")
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "authurl":
        return authurl()
    if len(argv) >= 2 and argv[0] == "exchange":
        return asyncio.run(_exchange(argv[1]))
    if argv and argv[0] == "refresh":
        return asyncio.run(_refresh())
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
