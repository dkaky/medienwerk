"""Einen Browser mit dem dauerhaften Profil starten.

Stand bis 08.09.2026 in ``app/integrations/aliexpress_store.py`` und hatte dort
nie etwas zu suchen: Das Profil ist nicht das eines Lieferanten, sondern
schlicht der Browser dieses Rechners. Der Motiv-Radar brauchte es und zog sich
damit den kompletten AliExpress-Trakt herein - beim Ausraeumen des Handelsteils
waere er mitgerissen worden.

Wozu ein DAUERHAFTES Profil: An ihm haengen die Anmeldungen. eBay zeigt
Verkaufszahlen nur angemeldet; ohne Profil ist das Marktsignal des Radars nur
noch die Position in der Liste.
"""

from __future__ import annotations

from pathlib import Path

#: Reihenfolge der Browser, die probiert werden. Playwrights MITGELIEFERTER
#: Chromium (kein Kanal, daher None am Ende) startet auf diesem Windows zwar
#: unsichtbar, aber NICHT als Fenster - er scheitert mit "spawn UNKNOWN"
#: (gemessen 05.09.2026). Der installierte Chrome und Edge koennen beides.
#: Wichtig ist vor allem, dass Anmeldung und Leser DENSELBEN Kanal nehmen -
#: sonst liest der Leser ein Profil, das ein anderer Browser geschrieben hat.
BROWSER_KANAELE = ("chrome", "msedge", None)

#: Wo das dauerhafte Profil liegt. Eine Stelle, nicht drei - frueher stand der
#: Pfad an jedem Aufrufort noch einmal.
PROFIL_ORDNER = Path("data/browser_profile")

#: Startschalter, die ein automatisiert gesteuerter Browser braucht.
START_ARGUMENTE = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
]


async def starte_profil(p, profil: Path | str | None = None, *, sichtbar: bool = False, **kw):
    """Dauerhaftes Profil oeffnen - mit dem ersten Kanal, der laeuft.

    Gibt ``(ctx, kanal)`` zurueck. Wirft nur, wenn KEIN Kanal startet.
    """
    ordner = Path(profil) if profil is not None else PROFIL_ORDNER
    ordner.mkdir(parents=True, exist_ok=True)
    letzter = None
    for kanal in BROWSER_KANAELE:
        try:
            ctx = await p.chromium.launch_persistent_context(
                str(ordner), headless=not sichtbar,
                **({"channel": kanal} if kanal else {}), **kw)
            return ctx, kanal or "playwright-chromium"
        except Exception as exc:  # noqa: BLE001 - naechsten Kanal probieren
            letzter = exc
    raise letzter if letzter else RuntimeError("Kein Browser startbar")
