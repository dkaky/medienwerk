"""Der bewusste Klick: eine Tuer im Probebetrieb, die nur von Hand aufgeht.

Ausgangslage: ``MOCK_EBAY=true`` heisst "es geht nichts an das eBay-Konto raus".
Die Sperre sitzt im HTTP-Weg des echten Clients (``_NurLesenClient`` in
``app/integrations/ebay.py``), weil sie an den Aufrufstellen siebenmal fehlte -
darunter zwei Zeitplaner-Jobs, die ohne jeden Klick laufen.

Nutzerwunsch vom 01.09.2026, woertlich: **"mock ebay kann ja gerne auf true
bleiben, wennich ein entwurf live schalten will dann soll ich das doch
duerfen."** Also: Automatik bleibt gesperrt, der Mensch kommt durch.

Warum das nicht mit einem einfachen Schalter geht
-------------------------------------------------
Die Publish-Warteschlange wird von ZWEI Seiten gefuettert: vom Klick im
Dashboard und vom Selbstheilungs-Job ``retry_failed_publishes`` (alle 20
Minuten, ohne Klick, ruft ebenfalls ``publish_queue.enqueue``). Ein Schalter
"die Warteschlange darf schreiben" wuerde deshalb genau den Automatik-Job
mitoeffnen, den die Sperre aufhalten soll.

Darum ist die Freigabe **pro Listing und einmalig**:

1. Der Router erteilt sie beim bestaetigten Klick: ``erteile(listing_id)``.
2. Der Worker verbraucht sie, kurz bevor er genau dieses Listing anfasst:
   ``with beim_veroeffentlichen(listing_id):``.
3. Nur solange dieser Block laeuft, sagt ``schreiben_erlaubt()`` ja.

Ein Listing, das der Automatik-Job einreiht, hat keine Freigabe - der Worker
laeuft dafuer mit geschlossener Tuer, und der ``_NurLesenClient`` haelt ihn an,
genau wie vorher.

Das Tor ist eine ``ContextVar``, kein Modul-Flag. Was das genau heisst, ist
wichtig genug fuer eine praezise Formulierung - ein Modul-Flag waere waehrend
der Veroeffentlichung fuer JEDEN im Prozess sichtbar:

* Sichtbar ist das offene Tor in der Aufgabe, die es geoeffnet hat, und in
  Aufgaben, die aus ihr heraus waehrenddessen entstehen (der Publish-Vorgang
  selbst - genau das soll er duerfen).
* NICHT sichtbar ist es fuer Aufgaben, die davor schon liefen oder aus einem
  anderen Zusammenhang gestartet werden. Dazu gehoeren die Jobs des
  Zeitplaners: APScheduler erzeugt sie aus seinem eigenen Zusammenhang, nicht
  aus dem Publish-Vorgang. Ein zeitgleich laufender Automatik-Job schluepft
  also nicht durch die offene Tuer.

Der Test ``test_fremde_aufgabe_sieht_das_tor_nicht`` haelt diese Grenze fest.

Nichts hiervon wird gespeichert. Ein Serverneustart loescht alle Freigaben; was
danach in der Warteschlange liegt, gilt wieder als Automatik.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar

logger = logging.getLogger("app.services.freigabe")

#: Listings, fuer die ein Mensch bewusst geklickt hat. Einmalverbrauch.
_erteilt: set[int] = set()

#: Steht nur waehrend EINER freigegebenen Veroeffentlichung auf True - und nur
#: in der Aufgabe, die sie durchfuehrt.
_tor: ContextVar[bool] = ContextVar("ebay_schreibtor", default=False)


def erteile(listing_id: int) -> None:
    """Ein Mensch hat bewusst geklickt: dieses eine Listing darf einmal raus."""
    _erteilt.add(int(listing_id))
    logger.info("Schreibfreigabe erteilt fuer Listing %s (bewusster Klick)", listing_id)


def hat_freigabe(listing_id: int) -> bool:
    """Liegt fuer dieses Listing eine unverbrauchte Freigabe vor?"""
    return int(listing_id) in _erteilt


def widerrufe(listing_id: int) -> None:
    """Freigabe zuruecknehmen, ohne sie zu verbrauchen."""
    _erteilt.discard(int(listing_id))


def alle_widerrufen() -> None:
    """Alles zuruecksetzen - fuer Tests und den Serverstart."""
    _erteilt.clear()


def schreiben_erlaubt() -> bool:
    """Darf JETZT, in DIESER Aufgabe, an eBay geschrieben werden?

    Die einzige Frage, die ``_NurLesenClient`` stellt. Ausserhalb einer
    freigegebenen Veroeffentlichung ist die Antwort immer nein.
    """
    return bool(_tor.get())


@contextmanager
def beim_veroeffentlichen(listing_id: int):
    """Tor oeffnen, falls fuer dieses Listing ein Klick vorliegt. Verbraucht ihn.

    Liefert ``True``, wenn geoeffnet wurde, sonst ``False`` - der Aufrufer muss
    nichts pruefen, er kann einfach weitermachen: ohne Freigabe greift die
    normale Sperre wie bisher.

    Die Freigabe wird beim BETRETEN verbraucht, nicht beim Verlassen. Ein
    gescheiterter Versuch wird also nicht stillschweigend wiederholt - der
    automatische Retry braucht dann einen neuen Klick. Das ist Absicht: sonst
    haette ein einziger Klick eine bis zu fuenfmal wiederholte Wirkung, und die
    Warteschlange wiederholt hartnaeckig.
    """
    lid = int(listing_id)
    offen = lid in _erteilt
    _erteilt.discard(lid)          # Einmalverbrauch, auch wenn es gleich scheitert
    marke = _tor.set(True) if offen else None
    if offen:
        logger.info("Schreibtor offen fuer Listing %s", lid)
    try:
        yield offen
    finally:
        if marke is not None:
            _tor.reset(marke)
