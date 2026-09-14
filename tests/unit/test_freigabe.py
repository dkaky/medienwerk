"""Der bewusste Klick kommt durch, die Automatik nicht.

Die gefaehrlichste Verwechslung dieser Aenderung waere ein Schalter, der die
Publish-Warteschlange pauschal oeffnet. Die Warteschlange wird naemlich von
ZWEI Seiten gefuettert: vom Klick im Dashboard und vom Selbstheilungs-Job
``retry_failed_publishes`` (alle 20 Minuten, ohne Zutun). Diese Tests halten
fest, dass genau diese Verwechslung nicht passiert ist.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import freigabe


@pytest.fixture(autouse=True)
def _sauber():
    """Freigaben sind Prozess-Zustand - zwischen Tests immer leeren."""
    freigabe.alle_widerrufen()
    yield
    freigabe.alle_widerrufen()


# --- Grundverhalten ---------------------------------------------------------

def test_ohne_klick_ist_schreiben_verboten():
    assert freigabe.schreiben_erlaubt() is False


def test_erteilte_freigabe_oeffnet_das_tor():
    freigabe.erteile(7)
    with freigabe.beim_veroeffentlichen(7) as offen:
        assert offen is True
        assert freigabe.schreiben_erlaubt() is True


def test_tor_schliesst_sich_wieder():
    freigabe.erteile(7)
    with freigabe.beim_veroeffentlichen(7):
        pass
    assert freigabe.schreiben_erlaubt() is False


def test_tor_schliesst_auch_wenn_es_kracht():
    """Ein Fehler mitten im Veroeffentlichen darf das Tor nicht offen lassen."""
    freigabe.erteile(7)
    with pytest.raises(RuntimeError):
        with freigabe.beim_veroeffentlichen(7):
            raise RuntimeError("eBay antwortet nicht")
    assert freigabe.schreiben_erlaubt() is False


# --- Die eigentliche Absicherung -------------------------------------------

def test_freigabe_gilt_nur_fuer_das_geklickte_listing():
    """Ein Klick auf Listing 7 darf Listing 8 nicht mit rausschleusen."""
    freigabe.erteile(7)
    with freigabe.beim_veroeffentlichen(8) as offen:
        assert offen is False
        assert freigabe.schreiben_erlaubt() is False


def test_freigabe_ist_einmalig():
    """Der Automatik-Job reiht dasselbe Listing spaeter erneut ein.

    Waere die Freigabe dauerhaft, wuerde ein einziger Klick jeden spaeteren
    automatischen Versuch mit durchlassen - unbemerkt und ohne Zutun.
    """
    freigabe.erteile(7)
    with freigabe.beim_veroeffentlichen(7) as offen:
        assert offen is True
    with freigabe.beim_veroeffentlichen(7) as offen:
        assert offen is False


def test_freigabe_wird_auch_bei_fehler_verbraucht():
    """Sonst haette ein Klick eine mehrfach wiederholte Wirkung."""
    freigabe.erteile(7)
    with pytest.raises(RuntimeError):
        with freigabe.beim_veroeffentlichen(7):
            raise RuntimeError("kaputt")
    assert freigabe.hat_freigabe(7) is False


def test_widerruf_nimmt_die_freigabe_zurueck():
    freigabe.erteile(7)
    freigabe.widerrufe(7)
    with freigabe.beim_veroeffentlichen(7) as offen:
        assert offen is False


def test_fremde_aufgabe_sieht_das_tor_nicht():
    """Eine Aufgabe aus einem ANDEREN Zusammenhang erbt das offene Tor nicht.

    Das ist die Grenze, auf die es ankommt: die Jobs des Zeitplaners werden von
    APScheduler aus dessen eigenem Zusammenhang gestartet, nicht aus dem
    laufenden Publish-Vorgang. Sie duerfen die offene Tuer nicht sehen.

    Nachgestellt wird das mit einer Aufgabe, die BEREITS LAEUFT, bevor das Tor
    aufgeht - dieselbe Ausgangslage wie beim Zeitplaner. Ein Modul-Flag wuerde
    hier durchfallen, weil es prozessweit sichtbar waere.

    Abgegrenzt davon: eine Aufgabe, die INNERHALB des offenen Blocks entsteht,
    erbt das Tor sehr wohl - das ist der Publish-Vorgang selbst, und der soll
    es duerfen. Siehe test_eigene_unteraufgabe_darf_schreiben.
    """
    gesehen = {}

    async def lauf():
        los = asyncio.Event()

        async def fremde_aufgabe():
            # Existiert VOR dem Oeffnen; ihr Zusammenhang ist damit festgelegt.
            await los.wait()
            gesehen["fremd"] = freigabe.schreiben_erlaubt()

        t = asyncio.create_task(fremde_aufgabe())
        await asyncio.sleep(0)                 # Aufgabe wirklich starten lassen

        freigabe.erteile(7)
        with freigabe.beim_veroeffentlichen(7):
            gesehen["eigen"] = freigabe.schreiben_erlaubt()
            los.set()
            await t

    asyncio.run(lauf())
    assert gesehen["eigen"] is True
    assert gesehen["fremd"] is False, "Fremde Aufgabe hat das offene Tor gesehen"


def test_eigene_unteraufgabe_darf_schreiben():
    """Der Publish-Vorgang selbst darf parallel arbeiten, ohne sich auszusperren."""
    gesehen = {}

    async def lauf():
        async def teil_des_publish():
            gesehen["teil"] = freigabe.schreiben_erlaubt()

        freigabe.erteile(7)
        with freigabe.beim_veroeffentlichen(7):
            await asyncio.create_task(teil_des_publish())

    asyncio.run(lauf())
    assert gesehen["teil"] is True


# --- Zusammenspiel mit der eBay-Schreibsperre -------------------------------

def test_schreibsperre_haelt_ohne_freigabe():
    """Ohne Freigabe schaltet der Client weiter den Nur-Lesen-Wachposten vor."""
    from app.integrations import ebay as ebay_modul

    from app.config import get_settings
    client = ebay_modul.RealEbayClient(get_settings())
    assert isinstance(client._http(), ebay_modul._NurLesenClient)


def test_schreibsperre_oeffnet_mit_freigabe():
    from app.integrations import ebay as ebay_modul

    from app.config import get_settings
    client = ebay_modul.RealEbayClient(get_settings())
    freigabe.erteile(7)
    with freigabe.beim_veroeffentlichen(7):
        assert not isinstance(client._http(), ebay_modul._NurLesenClient)
    # und danach wieder zu
    assert isinstance(client._http(), ebay_modul._NurLesenClient)


# --- Weg durch die Warteschlange -------------------------------------------

def test_sammellauf_gibt_ohne_bestaetigung_nichts_frei(db):
    """Der Trockenlauf darf keine einzige Freigabe erteilen."""
    from app.services import publish_queue

    asyncio.run(publish_queue.enqueue_all_drafts(db, dry_run=True))
    assert freigabe.hat_freigabe(1) is False


# --- Die vollstaendige Kette vom Klick bis zum Schreibzugriff ---------------
#
# Nutzerfrage vom 03.09.2026: "wieso kann ich die entwuerfe nicht live schalten?"
# Die Kette hat vier Glieder, und der Klick landet im Router, waehrend das
# Schreiben in einer ganz anderen Aufgabe passiert. Jedes Glied wird hier
# einzeln festgenagelt, damit die Antwort nicht wieder geraten werden muss.

def test_glied_3_der_worker_oeffnet_das_tor(monkeypatch):
    """Warteschlange: waehrend des Veroeffentlichens muss das Tor offen sein.

    Das ist das Glied, das am leichtesten unbemerkt reisst - es verbindet zwei
    verschiedene Aufgaben miteinander.
    """
    from app.services import publish_queue

    gesehen = {}

    async def _merke(listing_id):
        gesehen["tor_offen"] = freigabe.schreiben_erlaubt()
        return {"listing_id": listing_id, "ebay_item_id": "v1|1|0"}

    monkeypatch.setattr(publish_queue, "_publish_once", _merke)
    monkeypatch.setattr(publish_queue, "_finish", lambda *a, **k: None)

    freigabe.erteile(42)
    asyncio.run(publish_queue._process(42))
    assert gesehen["tor_offen"] is True, "Der Worker hat das Tor nicht geoeffnet"


def test_glied_3b_ohne_klick_bleibt_das_tor_zu(monkeypatch):
    """Derselbe Weg ohne Klick - so kommt der Selbstheilungs-Job daher."""
    from app.services import publish_queue

    gesehen = {}

    async def _merke(listing_id):
        gesehen["tor_offen"] = freigabe.schreiben_erlaubt()
        return {"listing_id": listing_id, "ebay_item_id": "v1|1|0"}

    monkeypatch.setattr(publish_queue, "_publish_once", _merke)
    monkeypatch.setattr(publish_queue, "_finish", lambda *a, **k: None)

    asyncio.run(publish_queue._process(42))          # keine Freigabe erteilt
    assert gesehen["tor_offen"] is False


def test_glied_4_das_tor_erreicht_den_ebay_client(monkeypatch):
    """Ganz durch: waehrend des Worker-Laufs darf der Client echt schreiben."""
    from app.config import get_settings
    from app.integrations import ebay as ebay_modul
    from app.services import publish_queue

    gesehen = {}

    async def _merke(listing_id):
        c = ebay_modul.RealEbayClient(get_settings())
        gesehen["gesperrt"] = isinstance(c._http(), ebay_modul._NurLesenClient)
        return {"listing_id": listing_id, "ebay_item_id": "v1|1|0"}

    monkeypatch.setattr(publish_queue, "_publish_once", _merke)
    monkeypatch.setattr(publish_queue, "_finish", lambda *a, **k: None)

    freigabe.erteile(42)
    asyncio.run(publish_queue._process(42))
    assert gesehen["gesperrt"] is False, (
        "Der Klick kam bis zum Worker, aber der eBay-Client schreibt trotzdem "
        "nicht - genau das Bild, das der Nutzer als 'geht nicht live' sieht."
    )
