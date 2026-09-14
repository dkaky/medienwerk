"""Warum scheitert die eBay-Validierung? Den Deletion-Endpunkt von aussen pruefen.

eBay meldet nur "endpoint validation failed" - ohne Grund. Dieses Skript ruft den
Endpunkt so auf, wie eBay es tut, und sagt, WAS nicht stimmt:

  * Laeuft ueberhaupt der richtige Code (oder noch das "Hello World" von Cloudflare)?
  * Kommen die Worker-Variablen an? Fehlt eine, rechnet der Worker mit dem Text
    "undefined" - das laesst sich erkennen, OHNE den Token zu kennen.
  * Stimmt der Token? Dafuer fragt das Skript ihn VERDECKT ab (nichts wird angezeigt,
    nichts gespeichert). Leer lassen ueberspringt diesen Teil.

Aufruf (aus dem Projektordner):
    .venv\\Scripts\\python.exe -m scripts.pruefe_ebay_endpunkt https://ebay-deletion.<name>.workers.dev

Aendert nichts - weder bei Cloudflare noch bei eBay.
"""
from __future__ import annotations

import getpass
import hashlib
import sys

import httpx

PROBE_CODE = "medienwerk-probe-123"


def erwartet(code: str, token: str, url: str) -> str:
    """Genau die Rechnung, die eBay erwartet: SHA-256(code + token + url) als HEX."""
    return hashlib.sha256((code + token + url).encode()).hexdigest()


def url_hinweise(url: str) -> list[str]:
    """Formfehler an der URL, die eBay sofort scheitern lassen."""
    hinweise = []
    if not url.startswith("https://"):
        hinweise.append("Die URL muss mit https:// beginnen.")
    if url.endswith("/"):
        hinweise.append("Die URL endet mit '/'. Auf eBay UND im Worker (ENDPOINT_URL) ohne "
                        "Schraegstrich am Ende eintragen - der Hash rechnet mit dem exakten Text.")
    if "?" in url:
        hinweise.append("Die URL enthaelt '?'. Nur die reine Adresse eintragen.")
    return hinweise


def token_hinweise(token: str) -> list[str]:
    """eBay erlaubt 32-80 Zeichen aus Buchstaben, Ziffern, '_' und '-'."""
    hinweise = []
    if token != token.strip():
        hinweise.append("Der Token hat Leerzeichen am Anfang oder Ende.")
    if not 32 <= len(token.strip()) <= 80:
        hinweise.append(f"Der Token hat {len(token.strip())} Zeichen - erlaubt sind 32 bis 80.")
    erlaubt = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
    if any(z not in erlaubt for z in token.strip()):
        hinweise.append("Der Token enthaelt Zeichen ausser Buchstaben, Ziffern, '_' und '-'.")
    return hinweise


def deute_antwort(antwort_hash: str, code: str, url: str) -> str | None:
    """Erkennt fehlende Worker-Variablen am Hash - ohne den Token zu kennen.

    Fehlt eine Variable, setzt JavaScript an ihrer Stelle den Text "undefined" ein.
    Zwei der drei Faelle lassen sich deshalb nachrechnen.
    """
    if antwort_hash == erwartet(code, "undefined", "undefined"):
        return ("Im Worker fehlen BEIDE Variablen (VERIFICATION_TOKEN und ENDPOINT_URL) - "
                "oder sie wurden angelegt, aber nicht mit 'Deploy' gespeichert.")
    if antwort_hash == erwartet(code, "undefined", url):
        return "Im Worker fehlt VERIFICATION_TOKEN (oder wurde nicht deployt)."
    return None


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    url = argv[0].strip()
    print("=" * 64)
    print(" eBay-Deletion-Endpunkt pruefen")
    print("=" * 64)
    fehler = 0

    for h in url_hinweise(url):
        print(f"  ! {h}")
        fehler += 1
    ziel = url.rstrip("/")

    print("1) Laeuft der richtige Code? (Aufruf ohne challenge_code) ...")
    try:
        r = httpx.get(ziel, timeout=15.0)
    except httpx.HTTPError as exc:
        print(f"   FAIL: Endpunkt nicht erreichbar: {exc}")
        print("   Hinweis: URL vertippt oder Worker nicht deployt.")
        return 2
    if r.status_code == 400 and "missing challenge_code" in r.text:
        print("   OK - der medienwerk-Worker antwortet.")
    else:
        print(f"   FAIL: HTTP {r.status_code}, Antwort: {r.text[:120]!r}")
        print("   Hinweis: Dort laeuft NICHT der Code aus deploy/cloudflare-worker/worker.js - "
              "vermutlich noch das Cloudflare-Beispiel. 'Edit code', einfuegen, 'Deploy'.")
        return 3

    print("2) Challenge wie eBay senden ...")
    r = httpx.get(ziel, params={"challenge_code": PROBE_CODE}, timeout=15.0)
    typ = r.headers.get("content-type", "")
    try:
        antwort = r.json().get("challengeResponse", "")
    except ValueError:
        antwort = ""
    if r.status_code != 200 or "application/json" not in typ or len(antwort) != 64:
        print(f"   FAIL: HTTP {r.status_code}, Typ {typ!r}, Antwort {r.text[:120]!r}")
        return 4
    print("   OK - Antwort hat die richtige Form.")

    deutung = deute_antwort(antwort, PROBE_CODE, ziel)
    if deutung:
        print(f"   FAIL: {deutung}")
        return 5

    print("3) Token vergleichen (verdeckte Eingabe, Enter = ueberspringen)")
    token = getpass.getpass("   Verification Token: ")
    if not token:
        print("   uebersprungen.")
    else:
        for h in token_hinweise(token):
            print(f"  ! {h}")
            fehler += 1
        if antwort == erwartet(PROBE_CODE, token.strip(), ziel):
            print("   OK - Token und ENDPOINT_URL im Worker stimmen mit deiner Eingabe ueberein.")
        elif antwort == erwartet(PROBE_CODE, token.strip(), ziel + "/"):
            print("   FAIL: ENDPOINT_URL im Worker endet mit '/'. Ohne Schraegstrich eintragen, Deploy.")
            return 6
        else:
            print("   FAIL: Der Worker rechnet mit einem ANDEREN Token oder einer anderen "
                  "ENDPOINT_URL als eingegeben.")
            print("   Hinweis: VERIFICATION_TOKEN im Worker neu setzen (exakt derselbe Wert wie "
                  "auf eBay), ENDPOINT_URL = exakt diese URL ohne '/', dann 'Deploy'.")
            return 6

    print("-" * 64)
    if fehler:
        print("Endpunkt antwortet richtig, aber siehe die Hinweise oben.")
        return 7
    print("PASS: Der Endpunkt antwortet so, wie eBay es verlangt.")
    print("      Auf eBay exakt diese URL und denselben Token eintragen, dann 'Save'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
