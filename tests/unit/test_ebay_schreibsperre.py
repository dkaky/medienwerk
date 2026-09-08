"""Im Probebetrieb geht nichts an das eBay-Konto raus (Pruefung 28.08.2026).

MOCK_EBAY=true sollte heissen: lesen ja, schreiben nein. Es hiess das nicht. Der
Riegel sass an zwei Router-Endpunkten, waehrend golive_service ausdruecklich einen
echten Client baut ("unabhaengig von MOCK_EBAY"), damit Lesen weiterlaeuft.

Daran vorbei fuehrten mindestens sieben Wege, zwei davon OHNE jeden Klick:
der Nachhol-Job alle 20 Minuten und die Publish-Queue bei jedem Serverstart.
Dazu /products/{id}/end, /products/shipping/free, /products/{id}/promote, der
naechtliche Mengenrabatt-Job und die Titel-Uebernahme.

Die Sperre sitzt deshalb an der HTTP-Engstelle, durch die alles muss - nicht an
den Aufrufstellen, wo der achte neue Aufrufer sie wieder vergessen wuerde.
"""
from __future__ import annotations

import asyncio

import pytest

from app.integrations.ebay import EbaySchreibsperre, RealEbayClient, _NurLesenClient
from app.retry import PersistentError


class _FakeHttp:
    """Merkt sich, was durchgekommen waere."""

    def __init__(self):
        self.durch = []
        self.headers = {"da": "ja"}

    async def get(self, url, *a, **kw):
        self.durch.append(("GET", url)); return "gelesen"

    async def post(self, url, *a, **kw):
        self.durch.append(("POST", url)); return "geschrieben"

    async def put(self, url, *a, **kw):
        self.durch.append(("PUT", url)); return "geschrieben"

    async def patch(self, url, *a, **kw):
        self.durch.append(("PATCH", url)); return "geschrieben"

    async def delete(self, url, *a, **kw):
        self.durch.append(("DELETE", url)); return "geschrieben"

    async def request(self, method, url, *a, **kw):
        self.durch.append((method, url)); return "egal"


ANGEBOT = "https://api.ebay.com/sell/inventory/v1/offer/123"
ANMELDUNG = "https://api.ebay.com/identity/v1/oauth2/token"
SCHLUESSEL = "https://apiz.ebay.com/developer/key_management/v1/signing_key"


@pytest.mark.parametrize("verb", ["post", "put", "patch", "delete"])
def test_schreiben_wird_angehalten(verb):
    echt = _FakeHttp()
    c = _NurLesenClient(echt)

    with pytest.raises(EbaySchreibsperre):
        asyncio.run(getattr(c, verb)(ANGEBOT))

    assert echt.durch == [], "es darf nichts beim echten Client angekommen sein"


def test_lesen_laeuft_echt_weiter():
    """Der Zweck des echten Clients im Probebetrieb: Bestellungen und Preise sehen."""
    echt = _FakeHttp()
    c = _NurLesenClient(echt)

    assert asyncio.run(c.get(ANGEBOT)) == "gelesen"
    assert echt.durch == [("GET", ANGEBOT)]


@pytest.mark.parametrize("url", [ANMELDUNG, SCHLUESSEL])
def test_anmeldung_bleibt_erlaubt(url):
    """Sie holt ein Zugriffsrecht und aendert nichts am Konto - ohne sie stirbt auch das Lesen."""
    echt = _FakeHttp()
    c = _NurLesenClient(echt)

    assert asyncio.run(c.post(url)) == "geschrieben"
    assert echt.durch == [("POST", url)]


def test_request_mit_fremdem_verb_wird_auch_angehalten():
    """Nicht nur die bequemen Kurzformen - auch der allgemeine Weg."""
    echt = _FakeHttp()
    c = _NurLesenClient(echt)

    with pytest.raises(EbaySchreibsperre):
        asyncio.run(c.request("POST", ANGEBOT))

    assert echt.durch == []


def test_request_mit_get_geht_durch():
    echt = _FakeHttp()
    c = _NurLesenClient(echt)

    asyncio.run(c.request("GET", ANGEBOT))

    assert echt.durch == [("GET", ANGEBOT)]


def test_sperre_gilt_als_dauerhaft_nicht_als_aussetzer():
    """Sonst versucht der Nachhol-Job sie alle 20 Minuten erneut, endlos."""
    assert issubclass(EbaySchreibsperre, PersistentError)


def test_die_meldung_sagt_wie_man_scharf_schaltet():
    echt = _FakeHttp()
    c = _NurLesenClient(echt)

    with pytest.raises(EbaySchreibsperre) as e:
        asyncio.run(c.post(ANGEBOT))

    text = str(e.value)
    assert "MOCK_EBAY=false" in text, "ohne den Hinweis sucht der Mensch lange"
    assert "angehalten" in text


def test_uebrige_eigenschaften_werden_durchgereicht():
    """aclose, headers und so weiter duerfen nicht verschwinden."""
    echt = _FakeHttp()
    c = _NurLesenClient(echt)

    assert c.headers == {"da": "ja"}


def test_echter_client_liefert_im_probebetrieb_die_sperre(monkeypatch):
    s = type("S", (), {"use_mock": lambda self, w: True})()
    c = RealEbayClient.__new__(RealEbayClient)
    c.settings = s
    c._client = None

    assert isinstance(c._http(), _NurLesenClient)


def test_scharf_geschaltet_gibt_es_keine_sperre():
    """Bei MOCK_EBAY=false muss der nackte Client kommen - sonst waere gar kein Verkauf moeglich."""
    import httpx
    s = type("S", (), {"use_mock": lambda self, w: False})()
    c = RealEbayClient.__new__(RealEbayClient)
    c.settings = s
    c._client = None

    h = c._http()

    assert isinstance(h, httpx.AsyncClient)
    assert not isinstance(h, _NurLesenClient)
