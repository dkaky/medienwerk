"""eBay-Zugang verknuepfen: Zustimmungs-Adresse bauen, Code gegen Refresh-Token tauschen.

Nutzt EBAY_CLIENT_ID / EBAY_CLIENT_SECRET / EBAY_RUNAME aus der .env.

Ablauf (aus dem Projektordner):
  1) Adresse erzeugen, im Browser oeffnen, bei eBay anmelden, zustimmen:
       .venv\\Scripts\\python.exe -m scripts.ebay_oauth authurl
  2) eBay zeigt danach eine Seite mit dem Code (in der Adresszeile: ``code=...``).
     Den Code in ANFUEHRUNGSZEICHEN uebergeben - er ist nur etwa 5 Minuten gueltig:
       .venv\\Scripts\\python.exe -m scripts.ebay_oauth exchange "<CODE>"
     -> der Refresh-Token wird DIREKT in die .env geschrieben (EBAY_REFRESH_TOKEN).
  3) Pruefen:
       .venv\\Scripts\\python.exe -m scripts.verify_ebay

Die RuName kann statt aus der .env auch als Argument kommen:
  ``authurl <RUNAME>`` bzw. ``exchange <RUNAME> "<CODE>"``.

**Warum der Token nicht mehr angezeigt wird (geaendert 12.09.2026):** Frueher stand
er nach dem Tausch im Fenster, mit der Bitte, ihn von Hand in die .env zu kopieren.
Damit lag ein Schluessel mit rund 18 Monaten Laufzeit im Verlauf des Terminals, und
beim Kopieren geht gern ein Zeichen verloren - eBay antwortet dann nur mit einem
wortkargen ``invalid_grant``. Jetzt schreibt das Skript ihn selbst, und auf dem
Bildschirm steht nur, DASS es geklappt hat.
"""
from __future__ import annotations

import base64
import sys
import urllib.parse
from pathlib import Path

import httpx

from app.config import Settings, get_settings

_SCOPES = (
    "https://api.ebay.com/oauth/api_scope/sell.inventory "
    "https://api.ebay.com/oauth/api_scope/sell.analytics.readonly "
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment "
    "https://api.ebay.com/oauth/api_scope/sell.account "
    "https://api.ebay.com/oauth/api_scope/sell.finances "
    "https://api.ebay.com/oauth/api_scope/sell.marketing"
)

#: Dieselbe Datei, die ``app.config`` liest - relativ zum Projektordner, aus dem
#: die Skripte gestartet werden.
ENV_DATEI = Path(".env")


def _auth_host(sandbox: bool) -> str:
    return "https://auth.sandbox.ebay.com" if sandbox else "https://auth.ebay.com"


def _api_host(sandbox: bool) -> str:
    return "https://api.sandbox.ebay.com" if sandbox else "https://api.ebay.com"


def fehlende_zugangsdaten(s: Settings) -> list[str]:
    """Welche der beiden App-Schluessel noch fehlen - als .env-Namen."""
    fehlt = []
    if not s.ebay_client_id:
        fehlt.append("EBAY_CLIENT_ID")
    if not s.ebay_client_secret:
        fehlt.append("EBAY_CLIENT_SECRET")
    return fehlt


def runame_aus(argument: str | None, s: Settings) -> str:
    """RuName aus dem Argument, sonst aus der .env. Leer, wenn beides fehlt."""
    return (argument or s.ebay_runame or "").strip()


def build_authurl(runame: str, s: Settings | None = None) -> str:
    s = s or get_settings()
    params = {
        "client_id": s.ebay_client_id,
        "response_type": "code",
        "redirect_uri": runame,
        "scope": _SCOPES,
        "prompt": "login",
    }
    return f"{_auth_host(s.ebay_use_sandbox)}/oauth2/authorize?" + urllib.parse.urlencode(params)


def speichere_refresh_token(token: str, env_datei: Path = ENV_DATEI) -> None:
    """Den Refresh-Token in die .env schreiben - vorhandene Zeile ersetzen, sonst anhaengen.

    ``set_key`` laesst alle anderen Zeilen und Kommentare stehen. Ohne Anfuehrungs-
    zeichen, damit die Zeile genauso aussieht wie alle anderen in der Datei.
    """
    from dotenv import set_key

    env_datei.touch(exist_ok=True)
    set_key(str(env_datei), "EBAY_REFRESH_TOKEN", token, quote_mode="never")


def exchange(runame: str, code: str, *, s: Settings | None = None,
             env_datei: Path = ENV_DATEI,
             transport: httpx.BaseTransport | None = None) -> int:
    s = s or get_settings()
    code = urllib.parse.unquote(code).strip()  # aus der Adresszeile ist der Code oft kodiert
    basic = base64.b64encode(f"{s.ebay_client_id}:{s.ebay_client_secret}".encode()).decode()
    with httpx.Client(timeout=20.0, transport=transport) as c:
        r = c.post(
            f"{_api_host(s.ebay_use_sandbox)}/identity/v1/oauth2/token",
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": runame},
        )
    body = r.json() if r.content else {}
    if r.status_code != 200 or "refresh_token" not in body:
        print("FEHLER beim Token-Tausch - an der .env wurde nichts geaendert.")
        print("  HTTP:", r.status_code)
        print("  eBay:", body.get("error"), "-", body.get("error_description", ""))
        if body.get("error") == "invalid_grant":
            print("  Hinweis: Code abgelaufen (nur ~5 min gueltig), schon benutzt oder "
                  "zu einer anderen RuName ausgestellt. Schritt 1 neu machen.")
        elif body.get("error") == "invalid_client":
            print("  Hinweis: Client-ID/Secret passen nicht - oder Sandbox- und "
                  "Produktions-Schluessel sind vertauscht.")
        return 2

    speichere_refresh_token(body["refresh_token"], env_datei)
    tage = int(body.get("refresh_token_expires_in", 0)) // 86400
    print("=" * 60)
    print(" ERFOLG - eBay-Zugang verknuepft")
    print("=" * 60)
    print(f"  Refresh-Token in {env_datei.resolve().name} gespeichert (EBAY_REFRESH_TOKEN).")
    print(f"  Gueltig etwa {tage} Tage - danach Schritt 1 und 2 wiederholen.")
    print("  Jetzt pruefen:  .venv\\Scripts\\python.exe -m scripts.verify_ebay")
    return 0


def main(argv: list[str]) -> int:
    s = get_settings()
    if not argv or argv[0] not in ("authurl", "exchange"):
        print(__doc__)
        return 1

    fehlt = fehlende_zugangsdaten(s)
    if fehlt:
        print(f"Es fehlt in der .env: {', '.join(fehlt)}.")
        print("  Zu finden auf developer.ebay.com -> Application Keys -> Production")
        print("  (App ID = EBAY_CLIENT_ID, Cert ID = EBAY_CLIENT_SECRET).")
        return 1

    if argv[0] == "authurl":
        runame = runame_aus(argv[1] if len(argv) >= 2 else None, s)
        if not runame:
            print("Es fehlt die RuName: EBAY_RUNAME in der .env eintragen oder als Argument "
                  "angeben. Zu finden auf developer.ebay.com -> User Tokens -> "
                  "'Get a Token from eBay via Your Application'.")
            return 1
        print(build_authurl(runame, s))
        return 0

    # exchange "<CODE>"  oder  exchange <RUNAME> "<CODE>"
    if len(argv) == 2:
        runame, code = runame_aus(None, s), argv[1]
    elif len(argv) >= 3:
        runame, code = runame_aus(argv[1], s), argv[2]
    else:
        print('Es fehlt der Code:  exchange "<CODE>"')
        return 1
    if not runame:
        print("Es fehlt die RuName (EBAY_RUNAME in der .env oder als Argument).")
        return 1
    return exchange(runame, code, s=s)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
