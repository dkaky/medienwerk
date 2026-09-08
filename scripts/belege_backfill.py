"""Belege-Sammler (lokal): echte AliExpress-Original-Belege -> POD Shop.

**Laeuft auf macOS UND Windows** – beide muessen die Belege nachpflegen koennen
(Urlaubsvertretung). Doppelklick-Starter daneben: ``BELEGE-HOLEN.command`` (Mac),
``BELEGE-HOLEN.bat`` (Windows).

WARUM LOKAL UND NICHT AUF DEM VPS: AliExpress hat fuer den Kaufbeleg keine
Schnittstelle – er entsteht erst im Browser einer angemeldeten Sitzung. Eine
Dauer-Anmeldung auf dem Server waere das Konto, ueber das echtes Geld laeuft;
AliExpress erkennt automatisierte Zugriffe. Deshalb bewusst auf euren Rechnern.

Nachfolger des im August entfernten Windows-Sammlers (Commit e61f480). Gleiche
Erkenntnisse, aber zwei Aenderungen:

* **Kein BACKFILL_TOKEN noetig.** Hochgeladen wird aus einer POD Shop-Seite im selben
  Browser – die Anmelde-Sitzung reicht (``app/auth.py`` laesst /originals auch per
  Login-Cookie durch). Der Token liegt nur auf dem VPS und ist hier nicht verfuegbar.
* Startet Chrome selbst mit eigenem Profil + Debug-Port (Chrome verweigert
  Remote-Debugging im Standardprofil). Euer normales Chrome bleibt unberuehrt.

Ablauf: Chrome starten -> DU meldest dich einmal bei AliExpress und beim Programm an
-> das Skript holt die To-do-Liste, laedt je Bestellung den Beleg und laedt ihn hoch.
Wiederholbar: erledigte Bestellungen fallen aus der To-do-Liste. Die Anmeldung bleibt
im eigenen Profil erhalten – spaetere Laeufe brauchen dich nicht mehr.

Die RECHNUNGEN daraus erzeugt der Server selbst (naechtlicher Job, `app/scheduler.py`)
– dafuer ist kein Rechner von euch noetig, egal wer die Belege eingesammelt hat.

AliExpress-Eigenheiten (aus der Live-Analyse des Vorgaengers uebernommen):
* Der Beleg-Dialog ist ein IFRAME (``iframe.invoice-iframe-container``) – der Klick auf
  „Herunterladen" MUSS darin passieren.
* Der erste Klick auf „Beleg" kommt oft zu frueh (die Seite baut sich noch auf) -> Retry.
* Der Download landet je nach Chrome im Downloads-Ordner als ``OrderSummary*<id>.png``.

    .venv/bin/python scripts/belege_backfill.py --limit 20
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFIL = ROOT / "chrome-belege-profil"          # gitignored
DL_DIR = ROOT / "data" / "backfill_downloads"
DOWNLOADS = Path.home() / "Downloads"
DEBUG_PORT = 9222
# Wohin das Skript die geholten Belege liefert. Hier stand die feste Adresse des
# GbR-Servers, von dem dieses System kopiert wurde - das Skript haette also die
# eigenen Belege bei einem fremden Betrieb abgeladen. Vorgabe ist jetzt der eigene
# Rechner auf Port 8030; wer woanders laeuft, setzt POD_SERVER.
SERVER = os.environ.get("POD_SERVER", "http://127.0.0.1:8030").rstrip("/")

AE_ORDERS = "https://www.aliexpress.com/p/order/index.html"
AE_DETAIL = "https://www.aliexpress.com/p/order/detail.html?orderId={ref}"


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


def chrome_pfad() -> str:
    """Chrome finden – macOS (Wajjahat) UND Windows (Kaky).

    Der Pfad war fest auf macOS verdrahtet; unter Windows scheiterte das Skript
    sofort. Beide muessen die Belege nachpflegen koennen (Urlaubsvertretung), also
    darf hier nichts rechnerspezifisch sein. CHROME_PFAD in der Umgebung schlaegt
    alles, falls Chrome woanders liegt.
    """
    if (eigen := os.environ.get("CHROME_PFAD")):
        if Path(eigen).exists():
            return eigen
        raise SystemExit(f"CHROME_PFAD zeigt auf nichts Vorhandenes: {eigen}")
    if sys.platform == "darwin":
        kandidaten = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    elif sys.platform.startswith("win"):
        kandidaten = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            str(Path(os.environ.get("LOCALAPPDATA", ""))
                / "Google" / "Chrome" / "Application" / "chrome.exe"),
        ]
    else:
        kandidaten = ["/usr/bin/google-chrome", "/usr/bin/chromium",
                      "/usr/bin/chromium-browser"]
    for k in kandidaten:
        if k and Path(k).exists():
            return k
    raise SystemExit(
        "Chrome nicht gefunden. Gesucht wurde:\n  " + "\n  ".join(kandidaten)
        + "\nLiegt Chrome woanders: CHROME_PFAD setzen, z.B.\n"
        + ('  set CHROME_PFAD="C:\\Pfad\\zu\\chrome.exe"' if sys.platform.startswith("win")
           else '  export CHROME_PFAD="/Pfad/zu/chrome"'))


# --------------------------------------------------------------- Chrome starten
def chrome_laeuft() -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=2)
        return True
    except Exception:  # noqa: BLE001
        return False


def chrome_starten() -> None:
    """Eigenes Chrome-Fenster mit Debug-Port. Eigenes Profil, weil Chrome das
    Standardprofil fuer Remote-Debugging blockiert – die Anmeldungen dort bleiben
    erhalten, der Login ist also nur beim ersten Mal noetig."""
    if chrome_laeuft():
        log("Chrome mit Debug-Port laeuft bereits.")
        return
    PROFIL.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [chrome_pfad(), f"--remote-debugging-port={DEBUG_PORT}", f"--user-data-dir={PROFIL}",
         "--no-first-run", "--no-default-browser-check", AE_ORDERS],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        if chrome_laeuft():
            log("Chrome gestartet.")
            return
        time.sleep(0.5)
    raise SystemExit("Chrome liess sich nicht mit Debug-Port starten.")


# ------------------------------------------------------------------ Anmeldungen
def seite(ctx, url: str):
    for p in ctx.pages:
        try:
            offen = p.url
        except Exception:  # noqa: BLE001 – geschlossene Tabs werfen beim Lesen
            continue
        if offen.startswith(url.split("?")[0][:40]):
            return p
    p = ctx.new_page()
    p.goto(url, timeout=60000)
    return p


def warte_auf_login(hole_seite, pruefung, name: str, minuten: int = 10):
    """Wartet, bis DU angemeldet bist. Passwoerter tippt das Skript nie selbst.

    Holt die Seite bei JEDEM Versuch neu. Beim Anmelden ersetzt AliExpress den Tab —
    ein einmal festgehaltener Verweis zeigt danach ins Leere, und der Lauf wartet die
    vollen 10 Minuten ab, obwohl laengst angemeldet ist. Genau so im Test passiert;
    ein Erstnutzer wuerde daraus schliessen, das Skript sei kaputt.

    Gibt die FRISCHE Seite zurueck – der Aufrufer darf nicht mit der alten weiterarbeiten.
    """
    ende = time.time() + minuten * 60
    gemeldet = False
    while time.time() < ende:
        try:
            page = hole_seite()
            if pruefung(page):
                log(f"{name}: angemeldet.")
                return page
        except Exception:  # noqa: BLE001
            pass
        if not gemeldet:
            log(f"→ Bitte im geoeffneten Chrome-Fenster bei {name} anmelden. Ich warte …")
            gemeldet = True
        time.sleep(3)
    raise SystemExit(f"{name}: keine Anmeldung erkannt – abgebrochen.")


def ae_angemeldet(page) -> bool:
    if "login" in (page.url or "").lower():
        return False
    txt = (page.inner_text("body", timeout=5000) or "").lower()
    return any(w in txt for w in ("bestellung", "order", "meine bestellungen"))


def server_angemeldet(page) -> bool:
    r = page.evaluate(
        "fetch('/api/v1/invoices/originals/todo').then(r=>r.status).catch(()=>0)")
    return int(r or 0) == 200


# ------------------------------------------------------- POD Shop (per Sitzung)
def todo_holen(adj_page, limit: int) -> list[dict]:
    roh = adj_page.evaluate(
        "fetch('/api/v1/invoices/originals/todo').then(r=>r.json())")
    liste = (roh or {}).get("aliexpress") or []
    return liste[:limit] if limit else liste


def hochladen(adj_page, pfad: Path, *, order_id: int, ref: str, betrag) -> dict:
    """Beleg ueber die angemeldete POD Shop-Seite hochladen (kein Token noetig)."""
    b64 = base64.b64encode(pfad.read_bytes()).decode("ascii")
    return adj_page.evaluate(
        """async ({b64, name, order_id, nummer, betrag}) => {
            const bin = atob(b64); const arr = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
            const fd = new FormData();
            fd.append('file', new Blob([arr], {type: 'image/png'}), name);
            fd.append('invoice_type', 'aliexpress_purchase');
            fd.append('order_id', String(order_id));
            fd.append('invoice_number', nummer);
            if (betrag !== null && betrag !== undefined) fd.append('amount', String(betrag));
            const r = await fetch('/api/v1/invoices/originals/attach',
                                  {method: 'POST', body: fd});
            return {status: r.status, text: (await r.text()).slice(0, 200)};
        }""",
        {"b64": b64, "name": pfad.name, "order_id": order_id,
         "nummer": f"AE_{ref}", "betrag": betrag})


# ------------------------------------------------------------------- Download
def _neue_datei(muster: list[str], vorher: set, timeout: float = 25.0):
    """Auf eine neue, vollstaendige Datei warten (kein halber .crdownload-Rest)."""
    ende = time.time() + timeout
    while time.time() < ende:
        for pat in muster:
            for p in DOWNLOADS.glob(pat):
                if str(p) in vorher or p.suffix == ".crdownload":
                    continue
                if (DOWNLOADS / (p.name + ".crdownload")).exists():
                    continue
                try:
                    groesse = p.stat().st_size
                    time.sleep(0.6)
                    if groesse > 0 and p.stat().st_size == groesse:
                        return p
                except OSError:
                    continue
        time.sleep(0.7)
    return None


def beleg_laden(page, frame, ref: str, ziel: Path):
    muster = [f"OrderSummary*{ref}.png", "OrderSummary*.png"]
    vorher = {str(p) for pat in muster for p in DOWNLOADS.glob(pat)}

    def klick():
        frame.get_by_text(re.compile(r"Herunter\s*laden|Download", re.I)).first.click(timeout=8000)

    try:
        with page.expect_download(timeout=20000) as dl:
            klick()
        dl.value.save_as(str(ziel))
        return ziel
    except Exception:  # noqa: BLE001 – Chrome legt den Download oft direkt im Ordner ab
        return _neue_datei(muster, vorher)


# ----------------------------------------------------------------------- Lauf
def sammle(limit: int) -> dict:
    """Ein kompletter Durchlauf. Gibt Zaehler zurueck, damit der Dienst-Modus
    das Ergebnis an das Programm zurueckmelden kann."""
    DL_DIR.mkdir(parents=True, exist_ok=True)
    chrome_starten()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
        ctx = browser.contexts[0]

        # Seite je Versuch neu holen – der Anmelde-Vorgang ersetzt den Tab.
        ae = warte_auf_login(lambda: seite(ctx, AE_ORDERS), ae_angemeldet, "AliExpress")
        adj = warte_auf_login(lambda: seite(ctx, SERVER + "/"),
                              server_angemeldet, "POD Shop")

        todo = todo_holen(adj, limit)
        if not todo:
            log("Keine offenen Belege.")
            return {"geholt": 0, "uebersprungen": 0, "fehler": 0}
        log(f"{len(todo)} Belege zu holen (neueste zuerst).")

        ok = fehler = uebersprungen = 0
        for i, t in enumerate(todo, 1):
            ref = str(t["aliexpress_order_id"])
            try:
                ae.goto(AE_DETAIL.format(ref=ref), timeout=45000)
                try:
                    ae.wait_for_load_state("networkidle", timeout=12000)
                except Exception:  # noqa: BLE001
                    pass
                ae.wait_for_timeout(2500)

                knopf = ae.get_by_role("button", name=re.compile(r"Beleg|Rechnung|Receipt", re.I))
                if knopf.count() == 0:
                    log(f"  [{i}/{len(todo)}] {ref}: kein Beleg-Knopf (alt/storniert?) – uebersprungen")
                    uebersprungen += 1
                    continue

                frame = None
                for _ in range(4):          # 1. Klick kommt oft zu frueh (Seitenaufbau)
                    try:
                        knopf.first.click(timeout=5000)
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        ae.wait_for_selector("iframe.invoice-iframe-container", timeout=4000)
                        frame = ae.frame_locator("iframe.invoice-iframe-container")
                        break
                    except Exception:  # noqa: BLE001
                        continue
                if frame is None:
                    log(f"  [{i}/{len(todo)}] {ref}: Beleg-Dialog oeffnete nicht – uebersprungen")
                    uebersprungen += 1
                    continue

                # Echten Zahlbetrag aus dem Beleg lesen (steht dort als „Insgesamt")
                betrag = None
                try:
                    txt = frame.locator("body").inner_text(timeout=3000)
                    m = re.search(r"Insgesamt:?\s*([\d.,]+)\s*€", txt) or \
                        re.search(r"Total:?\s*([\d.,]+)\s*€", txt)
                    if m:
                        betrag = float(m.group(1).replace(".", "").replace(",", "."))
                except Exception:  # noqa: BLE001
                    pass

                datei = beleg_laden(ae, frame, ref, DL_DIR / f"ae_{ref}.png")
                if not datei:
                    log(f"  [{i}/{len(todo)}] {ref}: Download kam nicht an – uebersprungen")
                    uebersprungen += 1
                    ae.keyboard.press("Escape")
                    continue

                antwort = hochladen(adj, Path(datei), order_id=t["order_id"], ref=ref, betrag=betrag)
                if int(antwort.get("status", 0)) not in (200, 201):
                    raise RuntimeError(f"Upload {antwort.get('status')}: {antwort.get('text')}")
                try:
                    Path(datei).unlink()
                except OSError:
                    pass
                ok += 1
                log(f"  [{i}/{len(todo)}] {ref}: hochgeladen"
                    + (f" ({betrag:.2f} €)" if betrag else ""))
                ae.keyboard.press("Escape")
                ae.wait_for_timeout(700)
            except Exception as exc:  # noqa: BLE001 – ein Beleg darf den Lauf nicht stoppen
                fehler += 1
                log(f"  [{i}/{len(todo)}] {ref}: FEHLER {str(exc)[:150]}")
                try:
                    ae.keyboard.press("Escape")
                except Exception:  # noqa: BLE001
                    pass
                ae.wait_for_timeout(800)

        log(f"Fertig: {ok} hochgeladen, {uebersprungen} uebersprungen, {fehler} Fehler.")
        return {"geholt": ok, "uebersprungen": uebersprungen, "fehler": fehler}


# ============================================================== Dienst-Modus
# Der Knopf im Programm laeuft auf dem SERVER und kommt an keinen Browser bei euch
# heran. Der Helfer muss also nachfragen — aber OHNE dafuer Chrome offen zu halten:
# genau das liess beim Anmelden ungefragt ein Fenster aufgehen und Belege holen
# (Wajjahat, 18.08.: "das soll so nicht sein"). Deshalb fragt er ueber den bewusst
# oeffentlichen, inhaltslosen Endpunkt /originals/abruf-signal (nur ein Ja/Nein) und
# startet Chrome erst, wenn wirklich jemand gedrueckt hat.


def _adj_api(pfad: str, method: str = "GET"):
    """POD Shop-API ueber die angemeldete Seite ansprechen (kein Token noetig).

    Wird NUR waehrend eines Laufs benutzt — da ist Chrome ohnehin offen.
    """
    from playwright.sync_api import sync_playwright

    chrome_starten()
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
        ctx = b.contexts[0]
        adj = seite(ctx, SERVER + "/")
        return adj.evaluate(
            """async ({p, m}) => {
                const r = await fetch(p, {method: m});
                const t = await r.text();
                try { return {status: r.status, data: JSON.parse(t)}; }
                catch { return {status: r.status, data: t.slice(0, 200)}; }
            }""", {"p": pfad, "m": method})


def _lauf() -> dict:
    """Ein kompletter Durchgang: anmelden -> Belege holen -> Rechnungen erzeugen.

    Meldet Anfang und Ende an das Programm, damit beide im Dashboard sehen, was
    laeuft und was dabei herauskam.
    """
    try:
        # Der Auftrag liegt schon vor (der Knopf hat ihn angelegt) – hier nur noch
        # uebernehmen, damit nicht beide Rechner denselben Lauf machen.
        a = _adj_api("/api/v1/invoices/originals/abruf-uebernehmen", "POST")
        if not (a.get("data") or {}).get("uebernommen"):
            log("Laeuft schon auf einem anderen Rechner – nichts zu tun.")
            return {"geholt": 0, "uebersprungen": 0, "fehler": 0}
    except Exception as exc:  # noqa: BLE001
        log(f"Konnte den Lauf nicht uebernehmen: {str(exc)[:120]}")

    try:
        ergebnis = sammle(0) or {}
    except SystemExit as exc:                 # z.B. Anmeldung abgelaufen
        ergebnis = {"geholt": 0, "uebersprungen": 0, "fehler": 1, "meldung": str(exc)[:200]}
    except Exception as exc:  # noqa: BLE001
        ergebnis = {"geholt": 0, "uebersprungen": 0, "fehler": 1, "meldung": str(exc)[:200]}

    # Kette zu Ende fuehren: aus den frischen Belegen gleich die Rechnungen erzeugen.
    if ergebnis.get("geholt"):
        log(f"Erzeuge Rechnungen aus {ergebnis['geholt']} neuen Belegen …")
        try:
            r = _adj_api("/api/v1/invoices/rechnungen/erzeugen"
                         f"?limit={int(ergebnis['geholt']) + 5}", "POST")
            gebaut = (r.get("data") or {}).get("erzeugt") if isinstance(r, dict) else None
            log(f"{gebaut} Rechnungen erzeugt.")
            ergebnis["rechnungen"] = int(gebaut or 0)
        except Exception as exc:  # noqa: BLE001
            # Belege sind sicher abgelegt – der Nachtlauf des Servers holt die
            # Rechnungen nach. Kein Grund, den Lauf als Fehler zu melden.
            log(f"Rechnungen kommen im Nachtlauf: {str(exc)[:100]}")

    try:
        from urllib.parse import quote
        q = (f"?geholt={ergebnis.get('geholt', 0)}"
             f"&uebersprungen={ergebnis.get('uebersprungen', 0)}"
             f"&fehler={ergebnis.get('fehler', 0)}"
             f"&rechnungen={ergebnis.get('rechnungen', 0)}")
        if ergebnis.get("meldung"):
            q += "&meldung=" + quote(str(ergebnis["meldung"])[:200])
        _adj_api("/api/v1/invoices/originals/abruf-fertig" + q, "POST")
    except Exception as exc:  # noqa: BLE001
        log(f"Rueckmeldung fehlgeschlagen: {str(exc)[:120]}")
    return ergebnis


def dienst(intervall: int = 15) -> None:
    """Auf den Knopf im Programm warten – OHNE dafuer Chrome offen zu halten.

    Erster Versuch war: die POD Shop-Seite stoesst den Helfer auf 127.0.0.1 an. Chrome
    blockiert das aber — eine oeffentliche HTTPS-Seite darf den eigenen Rechner nicht
    aufrufen (Private Network Access), und daran aendern auch Kopfzeilen nichts.

    Also doch nachfragen, aber ohne Browser: ``/originals/abruf-signal`` ist bewusst
    oeffentlich und liefert nur ein Ja/Nein. Chrome startet erst, wenn wirklich jemand
    gedrueckt hat — kein Fenster beim Anmelden (Vorfall 18.08.).

    ``intervall`` ist zugleich die Wartezeit, die man nach dem Knopfdruck sieht. 15 s
    auf Wajjahats Wunsch (vorher 45 s); eine Nachfrage ist ein winziger GET, das
    faellt weder beim Server noch im Netz ins Gewicht.
    """
    import json as _json

    signal = f"{SERVER}/api/v1/invoices/originals/abruf-signal"
    log(f"Helfer bereit. Er startet nichts von allein — nur auf den Knopf "
        f"„Belege abrufen\" im Programm. (Nachfrage alle {intervall}s, ohne Browser.)")
    ruhig = True                      # nur EINMAL meckern, wenn der Server weg ist
    while True:
        wartet = False
        try:
            with urllib.request.urlopen(signal, timeout=15) as r:
                wartet = bool((_json.loads(r.read().decode("utf-8")) or {}).get("wartet"))
            ruhig = True
        except Exception as exc:  # noqa: BLE001 – Netz weg? Beim naechsten Mal wieder.
            if ruhig:
                log(f"POD Shop nicht erreichbar: {str(exc)[:110]}")
                ruhig = False
        if wartet:
            log("Knopf gedrueckt – Belege holen.")
            try:
                e = _lauf()
                log(f"Fertig: {e.get('geholt', 0)} geholt, "
                    f"{e.get('rechnungen', 0)} Rechnungen.")
            except Exception as exc:  # noqa: BLE001
                log(f"Lauf abgebrochen: {str(exc)[:150]}")
        time.sleep(max(5, intervall))   # Untergrenze, damit niemand den Server flutet


def main() -> None:
    ap = argparse.ArgumentParser(description="AliExpress-Belege in das Programm holen")
    ap.add_argument("--limit", type=int, default=20, help="wie viele Belege (0 = alle)")
    ap.add_argument("--dienst", action="store_true",
                    help="mitlaufen und auf den Knopf im Programm warten")
    ap.add_argument("--intervall", type=int, default=15,
                    help="Sekunden zwischen zwei Nachfragen (nur mit --dienst)")
    args = ap.parse_args()
    if args.dienst:
        dienst(args.intervall)
    else:
        sammle(args.limit)


if __name__ == "__main__":
    main()
