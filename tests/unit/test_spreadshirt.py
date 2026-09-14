"""Tests der Spreadshirt-Anbindung.

Kein Test geht ins Netz: alle Aufrufe laufen ueber einen ``httpx.MockTransport``,
der die Anfrage festhaelt, statt sie zu verschicken.

Die wichtigsten Tests hier sind nicht die ueber die Nutzdaten, sondern die ueber
die beiden Stellen, an denen Spreadshirt schweigend abweist: den User-Agent und
den ``mediaType=json``-Schalter.
"""

from __future__ import annotations

import asyncio
import hashlib

import httpx
import pytest

from app.integrations.spreadshirt import (
    BASE_URL_EU,
    SpreadshirtClient,
    SpreadshirtFehler,
    signatur,
)

UA = "Medienwerk-POD-Shop/1.0 (https://www.ebay.de/str/druckhelden; medienwerk@outlook.com)"


def _client(handler, **kw) -> SpreadshirtClient:
    kw.setdefault("shop_id", "123456")
    kw.setdefault("user_agent", UA)
    return SpreadshirtClient("schluessel-abc", transport=httpx.MockTransport(handler), **kw)


# --- Anlegen: die zwei Stolpersteine ----------------------------------------

def test_ohne_schluessel_kein_client():
    with pytest.raises(ValueError, match="SPREADSHIRT_API_KEY"):
        SpreadshirtClient("", user_agent=UA)


def test_ohne_user_agent_kein_client():
    """Spreadshirt sperrt Anfragen ohne Kennung - und erklaert das nicht.

    Ein 403 im Betrieb waere kaum zu deuten, deshalb faellt es hier auf.
    """
    with pytest.raises(ValueError, match="SPREADSHIRT_USER_AGENT"):
        SpreadshirtClient("schluessel-abc", user_agent="   ")


def test_ohne_shop_nummer_kein_lesen():
    def handler(_):  # pragma: no cover - darf nie erreicht werden
        raise AssertionError("Es haette gar nicht erst gesendet werden duerfen")

    client = _client(handler, shop_id="")
    with pytest.raises(SpreadshirtFehler, match="SPREADSHIRT_SHOP_ID"):
        asyncio.run(client.shop())


def test_shopname_statt_shopnummer_wird_erklaert():
    """Der teuerste Irrtum: Spreadshirt adressiert Shops ueber eine ZAHL.

    Mit dem Namen aus der Adresszeile antwortet die Schnittstelle 404
    "Not found." - und man sucht den Fehler beim Schluessel statt bei der
    Nummer. Deshalb faellt es hier auf, bevor gesendet wird.
    """
    def handler(_):  # pragma: no cover - darf nie erreicht werden
        raise AssertionError("Es haette gar nicht erst gesendet werden duerfen")

    client = _client(handler, shop_id="shop.medienwerk")
    with pytest.raises(SpreadshirtFehler, match="ZAHL des Shops"):
        asyncio.run(client.shop())


# --- Der Kopf ----------------------------------------------------------------

def test_kopf_traegt_schluessel_und_kennung():
    gesehen = {}

    def handler(anfrage: httpx.Request) -> httpx.Response:
        gesehen["auth"] = anfrage.headers["Authorization"]
        gesehen["ua"] = anfrage.headers["User-Agent"]
        gesehen["url"] = str(anfrage.url)
        return httpx.Response(200, json={"id": "123456"})

    asyncio.run(_client(handler).shop())
    assert gesehen["auth"] == 'SprdAuth apiKey="schluessel-abc"'
    assert gesehen["ua"] == UA
    assert gesehen["url"].startswith(f"{BASE_URL_EU}/shops/123456")


def test_json_wird_ausdruecklich_verlangt():
    """Ohne ``mediaType=json`` antwortet Spreadshirt in XML - und alles bricht."""
    gesehen = {}

    def handler(anfrage: httpx.Request) -> httpx.Response:
        gesehen["mediaType"] = anfrage.url.params.get("mediaType")
        return httpx.Response(200, json={"id": "123456"})

    asyncio.run(_client(handler).shop())
    assert gesehen["mediaType"] == "json"


# --- Fehler ------------------------------------------------------------------

def test_401_wird_gemeldet_nicht_verschluckt():
    """Der haeufigste Fall: EU-Schluessel an der NA-Basis (oder umgekehrt)."""
    def handler(_):
        return httpx.Response(401, text="Unauthorized")

    with pytest.raises(SpreadshirtFehler, match="401"):
        asyncio.run(_client(handler).shop())


def test_xml_antwort_wird_als_fehler_gemeldet():
    def handler(_):
        return httpx.Response(200, text="<shop><id>1</id></shop>")

    with pytest.raises(SpreadshirtFehler, match="kein JSON"):
        asyncio.run(_client(handler).shop())


def test_429_wird_wiederholt(monkeypatch):
    """Bei Ueberlast wird gewartet und erneut gefragt, nicht aufgegeben."""
    monkeypatch.setattr(asyncio, "sleep", _sofort)
    versuche = []

    def handler(_):
        versuche.append(1)
        if len(versuche) < 3:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"id": "123456"})

    ergebnis = asyncio.run(_client(handler).shop())
    assert ergebnis == {"id": "123456"}
    assert len(versuche) == 3


def test_dauerhafter_429_endet_im_fehler(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _sofort)

    def handler(_):
        return httpx.Response(429, text="slow down")

    with pytest.raises(SpreadshirtFehler, match="429"):
        asyncio.run(_client(handler).shop())


async def _sofort(_sekunden):
    return None


# --- Artikel seitenweise -----------------------------------------------------

def test_artikel_alle_holt_die_naechste_seite():
    seiten = [
        {"articles": [{"id": str(i)} for i in range(50)]},
        {"articles": [{"id": "50"}, {"id": "51"}]},
    ]
    aufrufe = []

    def handler(anfrage: httpx.Request) -> httpx.Response:
        aufrufe.append(int(anfrage.url.params["offset"]))
        return httpx.Response(200, json=seiten[len(aufrufe) - 1])

    alle = asyncio.run(_client(handler).artikel_alle())
    assert len(alle) == 52
    assert aufrufe == [0, 50]


def test_artikel_alle_haelt_sich_an_die_bremse():
    """Ohne Obergrenze laeuft ein grosser Shop die Schnittstelle leer."""
    def handler(_):
        return httpx.Response(200, json={"articles": [{"id": "x"}] * 50})

    alle = asyncio.run(_client(handler).artikel_alle(hoechstens=120))
    assert len(alle) == 120


def test_leere_seite_beendet_den_lauf():
    def handler(_):
        return httpx.Response(200, json={"articles": []})

    assert asyncio.run(_client(handler).artikel_alle()) == []


# --- Signatur fuer spaetere Schreibzugriffe ----------------------------------

def test_signatur_nutzt_die_volle_url():
    """Mit dem Pfad allein stimmt die Signatur nicht - Spreadshirt antwortet 401."""
    url = f"{BASE_URL_EU}/baskets"
    erwartet = hashlib.sha1(f"POST {url} 1700000000 geheim".encode()).hexdigest()
    assert signatur("POST", url, "geheim", 1700000000) == erwartet
    assert signatur("POST", "/baskets", "geheim", 1700000000) != erwartet


def test_signierter_kopf_enthaelt_daten_und_sig():
    client = _client(lambda _: httpx.Response(200), api_secret="geheim")
    kopf = client.kopf_signiert("POST", f"{BASE_URL_EU}/baskets")
    assert 'apiKey="schluessel-abc"' in kopf["Authorization"]
    assert 'data="POST ' in kopf["Authorization"]
    assert 'sig="' in kopf["Authorization"]
    assert "sessionId" not in kopf["Authorization"]


def test_signierter_kopf_haengt_die_session_an():
    client = _client(lambda _: httpx.Response(200), api_secret="geheim")
    kopf = client.kopf_signiert("POST", f"{BASE_URL_EU}/orders", session="sitzung-1")
    assert 'sessionId="sitzung-1"' in kopf["Authorization"]


def test_ohne_secret_kein_signierter_kopf():
    client = _client(lambda _: httpx.Response(200))
    with pytest.raises(SpreadshirtFehler, match="SPREADSHIRT_API_SECRET"):
        client.kopf_signiert("POST", f"{BASE_URL_EU}/baskets")
