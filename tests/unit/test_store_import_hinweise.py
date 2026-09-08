"""Hinweise aus der Texterzeugung ueberleben den Shop-Import (Testlauf 27.08.2026).

upload_product meldet Auffaelligkeiten - "Titel auf 80 Zeichen gekuerzt", Marken-
Hinweise, entfernte Mengenangaben. Bei einem einzeln hochgeladenen Produkt liest
der Mensch das in der Antwort. Beim Shop-Import mit zehn Stueck wurden genau zwei
Felder uebernommen, listing_id und Titel; alles andere fiel weg.

Ausgerechnet dort ist es wichtig: bei zehn oder fuenfzig Entwuerfen liest niemand
jeden Titel einzeln. Im Testlauf waren zwei von zehn Titeln abgeschnitten, einer
mit offenem Anfuehrungszeichen - gemeldet hatte es das System, gehoert niemand.
"""
from __future__ import annotations

import pytest

from app.services import store_import_service as sis


@pytest.fixture(autouse=True)
def sauberer_zustand(tmp_path, monkeypatch):
    monkeypatch.setattr(sis, "_state_datei", lambda: tmp_path / "store.json")
    monkeypatch.setattr(sis, "_PAUSE_S", 0)
    sis._state.update({"running": False, "abbruch": False, "abgebrochen": False,
                       "created": 0, "total": 0, "done": 0, "skipped": 0,
                       "failed": 0, "error": None, "letzte": []})
    yield
    sis._state["running"] = False


def _shop(monkeypatch, anzahl=5):
    async def _ids(store_id, limit=None, bestseller=False):
        return [str(i) for i in range(anzahl)]
    monkeypatch.setattr(sis.store, "extract_store_id", lambda u: "1")
    monkeypatch.setattr(sis.store, "fetch_store_product_ids", _ids)
    monkeypatch.setattr(sis.store, "product_url", lambda pid: f"https://x/{pid}")
    monkeypatch.setattr(sis, "existing_product", lambda db, pid: None)


def _antworten(monkeypatch, bauen):
    from app.services import product_service
    zaehler = {"n": 0}

    async def _up(db, aliexpress_url=None, **kw):
        i = zaehler["n"]
        zaehler["n"] += 1
        return bauen(i)

    monkeypatch.setattr(product_service, "upload_product", _up)


@pytest.mark.asyncio
async def test_hinweise_landen_beim_produkt(monkeypatch):
    _shop(monkeypatch)
    _antworten(monkeypatch, lambda i: {
        "listing_id": i, "title_seo": f"Titel {i}",
        "warnings": ["Titel auf 80 Zeichen gekuerzt"] if i == 0 else []})

    r = await sis.import_store_products(None, store_url_or_id="https://x/store/1", limit=2)

    erstes = r["angelegt"][0]
    assert erstes["warnungen"] == ["Titel auf 80 Zeichen gekuerzt"]
    assert r["angelegt"][1]["warnungen"] == []


@pytest.mark.asyncio
async def test_sammelzahl_sagt_wieviele_einen_blick_brauchen(monkeypatch):
    """Ohne Zahl haette die Oberflaeche keinen Anlass, ueberhaupt hinzusehen."""
    _shop(monkeypatch)
    _antworten(monkeypatch, lambda i: {
        "listing_id": i, "title_seo": f"Titel {i}",
        "warnings": ["Marke unsicher"] if i in (0, 2) else []})

    r = await sis.import_store_products(None, store_url_or_id="https://x/store/1", limit=4)

    assert r["n_mit_hinweis"] == 2
    assert r["n_angelegt"] == 4


@pytest.mark.asyncio
async def test_ohne_hinweise_ist_die_zahl_null(monkeypatch):
    _shop(monkeypatch)
    _antworten(monkeypatch, lambda i: {"listing_id": i, "title_seo": f"Titel {i}"})

    r = await sis.import_store_products(None, store_url_or_id="https://x/store/1", limit=3)

    assert r["n_mit_hinweis"] == 0
    assert all(c["warnungen"] == [] for c in r["angelegt"])


@pytest.mark.asyncio
async def test_leere_eintraege_zaehlen_nicht_als_hinweis(monkeypatch):
    """Ein leerer String ist kein Hinweis - sonst blinkt die Oberflaeche grundlos."""
    _shop(monkeypatch)
    _antworten(monkeypatch, lambda i: {
        "listing_id": i, "title_seo": f"Titel {i}", "warnings": ["", None]})

    r = await sis.import_store_products(None, store_url_or_id="https://x/store/1", limit=2)

    assert r["n_mit_hinweis"] == 0


@pytest.mark.asyncio
async def test_fehlendes_warnungsfeld_bricht_nichts(monkeypatch):
    """Aeltere Antworten ohne das Feld duerfen den Import nicht kippen."""
    _shop(monkeypatch)
    _antworten(monkeypatch, lambda i: {"listing_id": i, "title_seo": "Titel"})

    r = await sis.import_store_products(None, store_url_or_id="https://x/store/1", limit=2)

    assert r["n_angelegt"] == 2
