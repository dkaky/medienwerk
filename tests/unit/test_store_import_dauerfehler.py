"""Shop-Import haelt an, wenn gar nichts geht (Testlauf 27.08.2026).

Beim ersten echten Lauf gegen einen AliExpress-Shop war der Zugang abgelaufen.
Ergebnis: dreissig Produkte, dreissig identische Fehlermeldungen, eine Minute
Wartezeit - und der eigentliche Grund ging in der Wiederholung unter.

Die Bremse greift nur, solange NOCH KEIN Produkt geklappt hat. Ein einzelnes
kaputtes Produkt gibt es immer; das darf einen sonst gesunden Lauf nicht kippen.
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


def _shop_mit(monkeypatch, ids):
    """Scraper liefert ``ids``, nichts davon ist schon im System."""
    async def _ids(store_id, limit=None, bestseller=False):
        return list(ids)
    monkeypatch.setattr(sis.store, "extract_store_id", lambda u: "1105629015")
    monkeypatch.setattr(sis.store, "fetch_store_product_ids", _ids)
    monkeypatch.setattr(sis.store, "product_url", lambda pid: f"https://x/{pid}")
    monkeypatch.setattr(sis, "existing_product", lambda db, pid: None)


def _upload(monkeypatch, verhalten):
    """``verhalten`` bekommt die laufende Nummer und liefert Ergebnis oder wirft."""
    from app.services import product_service
    zaehler = {"n": 0}

    async def _up(db, aliexpress_url=None, **kw):
        i = zaehler["n"]
        zaehler["n"] += 1
        return verhalten(i)

    monkeypatch.setattr(product_service, "upload_product", _up)
    return zaehler


@pytest.mark.asyncio
async def test_haelt_an_wenn_kein_einziges_produkt_geht(monkeypatch):
    """Der Fall vom 27.08.: Zugang abgelaufen, alle Produkte scheitern gleich."""
    _shop_mit(monkeypatch, [str(i) for i in range(30)])

    def immer_kaputt(i):
        raise RuntimeError("IllegalRefreshToken: token invalid or expired")

    zaehler = _upload(monkeypatch, immer_kaputt)

    r = await sis.import_store_products(None, store_url_or_id="https://x/store/1", limit=10)

    assert zaehler["n"] == sis._ABBRUCH_NACH, "nach der dritten Fehlmeldung ist Schluss"
    assert r["n_angelegt"] == 0
    assert r["n_fehlgeschlagen"] == sis._ABBRUCH_NACH


@pytest.mark.asyncio
async def test_der_grund_steht_im_zustand(monkeypatch):
    """Damit im Dashboard nicht nur 'fehlgeschlagen: 3' steht."""
    _shop_mit(monkeypatch, [str(i) for i in range(30)])
    _upload(monkeypatch, lambda i: (_ for _ in ()).throw(
        RuntimeError("IllegalRefreshToken")))

    await sis.import_store_products(None, store_url_or_id="https://x/store/1", limit=10)

    fehler = sis._state.get("error") or ""
    assert "IllegalRefreshToken" in fehler, "der echte Grund muss durchgereicht werden"
    assert "Abgebrochen" in fehler


@pytest.mark.asyncio
async def test_ein_kaputtes_produkt_stoppt_nichts(monkeypatch):
    """Nach einem Erfolg greift die Bremse nicht mehr - sonst waere sie zu scharf."""
    _shop_mit(monkeypatch, [str(i) for i in range(30)])

    def erst_gut_dann_kaputt(i):
        if i == 0:
            return {"listing_id": 1, "title_seo": "Erstes Produkt"}
        if i < 9:
            raise RuntimeError("dieses eine Produkt hakt")
        return {"listing_id": i, "title_seo": f"Produkt {i}"}

    zaehler = _upload(monkeypatch, erst_gut_dann_kaputt)

    r = await sis.import_store_products(None, store_url_or_id="https://x/store/1", limit=2)

    assert r["n_angelegt"] == 2, "der Lauf laeuft ueber die Fehler hinweg weiter"
    assert zaehler["n"] == 10, "acht Fehler dazwischen haben ihn nicht gestoppt"


@pytest.mark.asyncio
async def test_die_ersten_zwei_fehler_stoppen_noch_nicht(monkeypatch):
    """Grenzfall: zwei Fehler, dann Erfolg - der Lauf muss durchkommen."""
    _shop_mit(monkeypatch, [str(i) for i in range(30)])

    def zwei_fehler_dann_gut(i):
        if i < 2:
            raise RuntimeError("Aussetzer")
        return {"listing_id": i, "title_seo": f"Produkt {i}"}

    _upload(monkeypatch, zwei_fehler_dann_gut)

    r = await sis.import_store_products(None, store_url_or_id="https://x/store/1", limit=1)

    assert r["n_angelegt"] == 1
