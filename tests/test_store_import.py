"""Tests fuer den AliExpress-Shop-Scraper und den Shop-Import.

Der Headless-Browser wird NICHT gestartet – die Scrape-Funktion wird gemockt.
Getestet wird die Logik drumherum: Shop-Nummer erkennen, URL bauen, Fehler sauber melden,
Einzelfehler ueberspringen statt den Lauf abzubrechen.
"""
from __future__ import annotations

import pytest

from app.integrations import aliexpress_store as store
from app.retry import PersistentError
from app.services import store_import_service as sis


# ---------------------------------------------------------------- Shop-Nummer
@pytest.mark.parametrize("url,erwartet", [
    ("https://www.aliexpress.com/store/1103573332", "1103573332"),
    ("https://de.aliexpress.com/store/1103573332/pages/all-items.html", "1103573332"),
    ("https://www.aliexpress.com/store/home.html?shopId=1103573332", "1103573332"),
    ("https://de.aliexpress.com/w/wholesale.html?SearchText=x&sellerAdminSeq=1103573332",
     "1103573332"),
    ("1103573332", "1103573332"),
    ("https://www.aliexpress.com/store/1103573332?spm=a2g0o.detail", "1103573332"),
])
def test_shop_nummer_wird_erkannt(url, erwartet):
    assert store.extract_store_id(url) == erwartet


@pytest.mark.parametrize("url", [
    "", "   ", None,
    "https://de.aliexpress.com/item/1005011896904230.html",   # Produkt, kein Shop
    "https://www.google.de",
])
def test_ohne_shop_nummer_kein_treffer(url):
    assert store.extract_store_id(url) is None


def test_url_sortiert_nach_bestsellern():
    u = store.store_url("123456")
    assert "all-items.html" in u and "sortType=totalTranpro_desc" in u
    assert "sortType" not in store.store_url("123456", bestseller=False)


# ---------------------------------------------------------------- Browser-Lauf
class _FakeMouse:
    async def wheel(self, x, y):
        return None


class _FakePage:
    """Seite, die pro Aufruf eine vorgegebene Link-Liste liefert (leer = Aussetzer)."""

    def __init__(self, runden: list[list[str]]):
        self.runden = runden
        self.ladungen = 0
        self.mouse = _FakeMouse()

    async def goto(self, url, **kw):
        self.ladungen += 1

    async def wait_for_timeout(self, ms):
        return None

    async def eval_on_selector_all(self, sel, js):
        # pro Seitenladung eine Runde; danach bleibt es beim letzten Stand
        i = min(self.ladungen - 1, len(self.runden) - 1)
        return [f"https://de.aliexpress.com/item/{p}.html" for p in self.runden[i]]


class _FakeBrowser:
    def __init__(self, page):
        self._page = page
        self.geschlossen = False

    async def new_context(self, **kw):
        return self

    async def new_page(self):
        return self._page

    async def close(self):
        self.geschlossen = True


class _FakePW:
    def __init__(self, browser):
        self._b = browser
        self.chromium = self

    async def launch(self, **kw):
        return self._b

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _install_fake_browser(monkeypatch, runden):
    """Ein Fake-Playwright unter ``playwright.async_api`` einhaengen."""
    import sys
    import types

    page = _FakePage(runden)
    browser = _FakeBrowser(page)
    mod = types.ModuleType("playwright")
    sub = types.ModuleType("playwright.async_api")
    sub.async_playwright = lambda: _FakePW(browser)
    mod.async_api = sub
    monkeypatch.setitem(sys.modules, "playwright", mod)
    monkeypatch.setitem(sys.modules, "playwright.async_api", sub)
    return page, browser


@pytest.mark.asyncio
async def test_leere_seite_wird_neu_geladen(monkeypatch):
    """Ein leeres Rendering (live beobachtet) darf den Lauf nicht beenden."""
    page, browser = _install_fake_browser(
        monkeypatch, [[], ["3256800000001", "3256800000002"]])
    ids = await store.fetch_store_product_ids("123456", limit=2, max_scrolls=1)
    assert ids == ["3256800000001", "3256800000002"]
    assert page.ladungen == 2          # einmal leer, einmal erfolgreich
    assert browser.geschlossen is True


@pytest.mark.asyncio
async def test_dauerhaft_leer_meldet_fehler_statt_leerer_liste(monkeypatch):
    page, browser = _install_fake_browser(monkeypatch, [[], [], []])
    with pytest.raises(store.StoreScrapeError, match="Keine Produkte"):
        await store.fetch_store_product_ids("123456", limit=2, max_scrolls=1, versuche=3)
    # 1 Aufwaermbesuch (Startseite, Bot-Tarnung 15.08.) + 3 Ernte-Versuche
    assert page.ladungen == 4
    assert browser.geschlossen is True  # Browser wird auch im Fehlerfall beendet


@pytest.mark.asyncio
async def test_browser_wird_bei_absturz_geschlossen(monkeypatch):
    page, browser = _install_fake_browser(monkeypatch, [["3256800000001"]])

    async def boom(*a, **kw):
        raise RuntimeError("Chromium abgestuerzt")
    monkeypatch.setattr(page, "goto", boom)
    with pytest.raises(store.StoreScrapeError, match="nicht lesbar"):
        await store.fetch_store_product_ids("123456", limit=2)
    assert browser.geschlossen is True


@pytest.mark.asyncio
async def test_reihenfolge_bleibt_bestseller_zuerst(monkeypatch):
    ids_seite = ["3256800000009", "3256800000003", "3256800000007"]
    _install_fake_browser(monkeypatch, [ids_seite])
    ids = await store.fetch_store_product_ids("123456", limit=3, max_scrolls=1)
    assert ids == ids_seite            # keine Sortierung, keine Duplikate


# ---------------------------------------------------------------- Import-Lauf
class _FakeUpload:
    """Ersetzt product_service.upload_product; steuerbar pro Produkt-ID."""

    def __init__(self, verhalten: dict):
        self.verhalten = verhalten
        self.gesehen: list[str] = []

    async def __call__(self, db, *, aliexpress_url: str, **kw):
        pid = aliexpress_url.rsplit("/", 1)[-1].replace(".html", "")
        self.gesehen.append(pid)
        art = self.verhalten.get(pid, "ok")
        if art == "dupe":
            raise PersistentError("Produkt existiert bereits")
        if art == "boom":
            raise RuntimeError("Netzwerk kaputt")
        return {"listing_id": int(pid[-3:]), "title_seo": f"Titel {pid}"}


@pytest.fixture(autouse=True)
def _schnell(monkeypatch):
    monkeypatch.setattr(sis, "_PAUSE_S", 0)


async def _lauf(monkeypatch, ids, verhalten=None, **kw):
    async def fake_fetch(store_id, *, limit=25, **_):
        return ids[:limit]
    monkeypatch.setattr(store, "fetch_store_product_ids", fake_fetch)
    fake = _FakeUpload(verhalten or {})
    import app.services.product_service as ps
    monkeypatch.setattr(ps, "upload_product", fake)
    res = await sis.import_store_products(None, store_url_or_id="1103573332", **kw)
    return res, fake


@pytest.mark.asyncio
async def test_bestseller_werden_als_entwuerfe_angelegt(monkeypatch):
    ids = ["3256800000001", "3256800000002", "3256800000003"]
    res, fake = await _lauf(monkeypatch, ids)
    assert res["n_angelegt"] == 3
    assert res["store_id"] == "1103573332"
    assert fake.gesehen == ids          # Reihenfolge = Bestseller zuerst


@pytest.mark.asyncio
async def test_anzahl_wird_begrenzt(monkeypatch):
    ids = [f"325680000000{i}" for i in range(9)]
    res, fake = await _lauf(monkeypatch, ids, limit=4)
    assert len(fake.gesehen) == 4 and res["n_angelegt"] == 4


@pytest.mark.asyncio
async def test_duplikate_werden_uebersprungen_nicht_gezaehlt(monkeypatch):
    ids = ["3256800000001", "3256800000002"]
    res, _ = await _lauf(monkeypatch, ids, {"3256800000002": "dupe"})
    assert res["n_angelegt"] == 1 and res["n_uebersprungen"] == 1
    assert res["n_fehlgeschlagen"] == 0


@pytest.mark.asyncio
async def test_einzelner_fehler_stoppt_den_lauf_nicht(monkeypatch):
    ids = ["3256800000001", "3256800000002", "3256800000003"]
    res, fake = await _lauf(monkeypatch, ids, {"3256800000002": "boom"})
    assert len(fake.gesehen) == 3                      # auch das dritte wurde versucht
    assert res["n_angelegt"] == 2 and res["n_fehlgeschlagen"] == 1


@pytest.mark.asyncio
async def test_kaputter_link_wird_klar_gemeldet():
    with pytest.raises(PersistentError, match="Shop-Nummer"):
        await sis.import_store_products(None, store_url_or_id="https://www.google.de")


@pytest.mark.asyncio
async def test_bot_block_meldet_fehler_statt_leerem_erfolg(monkeypatch):
    async def fake_fetch(store_id, **_):
        raise store.StoreScrapeError("Shop-Seite nicht lesbar: Timeout")
    monkeypatch.setattr(store, "fetch_store_product_ids", fake_fetch)
    with pytest.raises(PersistentError, match="nicht lesbar"):
        await sis.import_store_products(None, store_url_or_id="1103573332")
    assert sis.status()["running"] is False
    assert "nicht lesbar" in (sis.status()["error"] or "")


@pytest.mark.asyncio
async def test_fortschritt_wird_am_ende_zurueckgesetzt(monkeypatch):
    res, _ = await _lauf(monkeypatch, ["3256800000001"], limit=1)
    s = sis.status()
    # "total" ist die GEWUENSCHTE Anzahl (Zielwert der Anzeige), "gefunden" der Shop-Vorrat
    assert s["running"] is False and s["created"] == 1 and s["total"] == 1
    assert s["gefunden"] == 1


def test_zweiter_start_wird_abgewiesen():
    """Zwei schnelle Klicks duerfen nicht zwei Browser starten (Doppelimport)."""
    sis.release()
    assert sis.try_reserve() is True
    assert sis.try_reserve() is False       # solange der erste laeuft: blockiert
    sis.release()
    assert sis.try_reserve() is True        # nach Freigabe wieder moeglich
    sis.release()


@pytest.mark.asyncio
async def test_hintergrundlauf_gibt_bei_fehler_wieder_frei(monkeypatch):
    async def fake_fetch(store_id, **_):
        raise store.StoreScrapeError("Timeout")
    monkeypatch.setattr(store, "fetch_store_product_ids", fake_fetch)
    sis.release()
    sis.try_reserve()
    await sis.import_store_products_bg(store_url_or_id="1103573332", limit=3)
    assert sis.status()["running"] is False     # sonst blieben alle weiteren Laeufe blockiert
    assert sis.try_reserve() is True
    sis.release()


@pytest.mark.asyncio
async def test_deckel_gegen_ausreisser(monkeypatch):
    ids = [f"32568000{i:05d}" for i in range(sis.MAX_PRODUCTS + 20)]
    _, fake = await _lauf(monkeypatch, ids, limit=5000)
    assert len(fake.gesehen) == sis.MAX_PRODUCTS


# ------------------------------------------------- schon gelistete nicht doppelt importieren
def test_beide_id_schreibweisen_derselben_ware():
    """Shop-Seite liefert 3256..., in der DB steht oft 1005... – beides ist dieselbe Ware."""
    a, b = store.id_variants("3256811824876359")
    assert a == "3256811824876359"
    assert b == "1005012011191111"
    assert store.id_variants(b) == [b, a]          # Umrechnung funktioniert in beide Richtungen
    assert store.id_variants("") == []
    assert store.id_variants("kaputt") == ["kaputt"]


def _produkt(db, *, ae_id: str, url: str | None = None):
    from app.models import Product
    p = Product(aliexpress_id=ae_id, title_raw=f"Ware {ae_id}",
                aliexpress_url=url or f"https://de.aliexpress.com/item/{ae_id}.html")
    db.add(p)
    db.commit()
    return p


def test_vorhandene_ware_wird_erkannt_auch_bei_anderer_schreibweise(db):
    """Kernwunsch: dieselbe AliExpress-Ware darf kein zweites Mal importiert werden."""
    p = _produkt(db, ae_id="1005012011191111")
    assert sis.existing_product(db, "3256811824876359") is p    # andere Schreibweise
    assert sis.existing_product(db, "1005012011191111") is p    # gleiche Schreibweise
    assert sis.existing_product(db, "3256800000000001") is None  # fremde Ware


def test_vorhandene_ware_wird_auch_ueber_die_url_erkannt(db):
    """Falls aliexpress_id fehlt, greift der Link als zweite Absicherung."""
    from app.models import Product
    p = Product(aliexpress_id=None, title_raw="Ohne ID",
                aliexpress_url="https://de.aliexpress.com/item/1005012011191111.html?spm=x")
    db.add(p)
    db.commit()
    assert sis.existing_product(db, "3256811824876359") is p


@pytest.mark.asyncio
async def test_bereits_gelistete_produkte_werden_nicht_importiert(db, monkeypatch):
    _produkt(db, ae_id="3256800000002")
    ids = ["3256800000001", "3256800000002", "3256800000003"]

    async def fake_fetch(store_id, *, limit=25, **_):
        return ids[:limit]
    monkeypatch.setattr(store, "fetch_store_product_ids", fake_fetch)
    fake = _FakeUpload({})
    import app.services.product_service as ps
    monkeypatch.setattr(ps, "upload_product", fake)

    res = await sis.import_store_products(db, store_url_or_id="1103573332", limit=3)
    assert "3256800000002" not in fake.gesehen      # gar nicht erst angefasst
    assert res["n_uebersprungen"] == 1
    assert res["uebersprungen"][0]["grund"].startswith("schon im System")


@pytest.mark.asyncio
async def test_anzahl_wird_mit_neuen_produkten_aufgefuellt(db, monkeypatch):
    """25 gewuenscht heisst 25 NEUE – vorhandene duerfen die Anzahl nicht auffressen."""
    for i in (1, 2, 3):
        _produkt(db, ae_id=f"325680000000{i}")
    vorrat = [f"325680000000{i}" for i in range(1, 8)]   # 1-3 vorhanden, 4-7 neu

    async def fake_fetch(store_id, *, limit=25, **_):
        assert limit > 3                                 # groesserer Vorrat wird geholt
        return vorrat[:limit]
    monkeypatch.setattr(store, "fetch_store_product_ids", fake_fetch)
    fake = _FakeUpload({})
    import app.services.product_service as ps
    monkeypatch.setattr(ps, "upload_product", fake)

    res = await sis.import_store_products(db, store_url_or_id="1103573332", limit=3)
    assert res["n_angelegt"] == 3                        # volle Anzahl trotz 3 Duplikaten
    assert fake.gesehen == ["3256800000004", "3256800000005", "3256800000006"]
    assert res["n_uebersprungen"] == 3


# ---------------------------------------------------------- Neustart mitten im Lauf
def test_neustart_waehrend_des_imports_wird_gemeldet(tmp_path, monkeypatch):
    """Real passiert 03.08.: ein Deploy-Neustart hat den laufenden Import abgeschossen –
    der Nutzer sah 11 statt 20 Produkte und KEINEN Hinweis warum."""
    datei = tmp_path / "store_import_state.json"
    monkeypatch.setattr(sis, "_state_datei", lambda: datei)

    sis.release()
    sis.try_reserve()
    sis._state.update({"store_id": "1104779162", "total": 20, "created": 11, "done": 11})
    sis._speichern()

    # Neustart: frischer Prozess-Zustand, dann Startpruefung
    sis._state.update({"running": False, "store_id": None, "total": 0, "created": 0,
                       "error": None})
    sis.startup_check()

    s = sis.status()
    assert s["running"] is False
    assert s["created"] == 11 and s["total"] == 20
    assert "Neustart" in (s["error"] or "")
    assert "übersprungen" in (s["error"] or "")      # Hinweis: einfach erneut starten
    sis.release()


def test_startpruefung_meldet_nichts_nach_sauberem_lauf(tmp_path, monkeypatch):
    datei = tmp_path / "store_import_state.json"
    monkeypatch.setattr(sis, "_state_datei", lambda: datei)
    sis.release()                      # running=False wird gespeichert
    sis._state["error"] = None
    sis.startup_check()
    assert sis.status()["error"] is None


def test_startpruefung_ohne_datei_ist_harmlos(tmp_path, monkeypatch):
    monkeypatch.setattr(sis, "_state_datei", lambda: tmp_path / "gibtsnicht.json")
    sis._state["error"] = None
    sis.startup_check()
    assert sis.status()["error"] is None


# ------------------------------------------------------- Produkt-Link-Ausweg
@pytest.mark.parametrize("text,erwartet", [
    ("https://de.aliexpress.com/item/1005012807847216.html", ["1005012807847216"]),
    ("https://de.aliexpress.com/item/1005012807847216.html?spm=x "
     "https://www.aliexpress.com/item/1005012853388923.html",
     ["1005012807847216", "1005012853388923"]),
    ("1005012807847216, 1005012853388923", ["1005012807847216", "1005012853388923"]),
    # Shop-URL liefert bewusst NICHTS (der Shop-Pfad bleibt zustaendig)
    ("https://de.aliexpress.com/store/1103573332", []),
    ("kein link", []),
])
def test_produkt_ids_aus_text(text, erwartet):
    assert sis.extract_product_ids(text) == erwartet


async def test_id_import_ueberspringt_seiten_ernte(db, monkeypatch):
    """product_ids gesetzt -> KEINE Seiten-Ernte; jede ID laeuft durch den
    normalen Produkt-Upload (Entwurf), Duplikate werden uebersprungen."""
    async def forbidden_fetch(*_a, **_k):
        raise AssertionError("ID-Import darf die Shop-Seite NICHT ernten")

    hochgeladen = []

    async def fake_upload(_db, *, aliexpress_url):
        hochgeladen.append(aliexpress_url)
        return {"listing_id": len(hochgeladen), "title_seo": "T"}

    from app.services import product_service
    monkeypatch.setattr(store, "fetch_store_product_ids", forbidden_fetch)
    monkeypatch.setattr(product_service, "upload_product", fake_upload)
    r = await sis.import_store_products(
        db, store_url_or_id="egal", limit=5,
        product_ids=["1005012807847216", "1005012853388923"])
    assert r["store_id"] == "ID-Import"
    assert r["n_angelegt"] == 2 and len(hochgeladen) == 2
    assert "1005012807847216" in hochgeladen[0]


@pytest.mark.asyncio
async def test_startup_setzt_unterbrochenen_lauf_automatisch_fort(monkeypatch, tmp_path):
    """Auto-Resume (15.08.): Neustart mitten im Lauf -> der Import wird beim
    Start automatisch wieder eingereiht statt nur gemeldet."""
    import asyncio
    import json
    state_file = tmp_path / "store_import_state.json"
    state_file.write_text(json.dumps({
        "running": True, "store_id": "1105638009", "total": 100, "done": 8,
        "created": 8, "url": "https://de.aliexpress.com/store/1105638009",
        "limit": 100, "product_ids": None}), encoding="utf-8")
    monkeypatch.setattr(sis, "_state_datei", lambda: state_file)
    fortgesetzt = []

    async def fake_bg(*, store_url_or_id, limit, product_ids=None):
        fortgesetzt.append((store_url_or_id, limit, product_ids))
        sis._state["running"] = False

    monkeypatch.setattr(sis, "import_store_products_bg", fake_bg)
    sis.startup_check()            # laeuft im pytest-asyncio-Loop -> Task eingereiht
    assert sis._state["running"] is True and sis._state["error"] is None
    await asyncio.sleep(0)         # Task drankommen lassen
    assert fortgesetzt == [("https://de.aliexpress.com/store/1105638009", 100, None)]
    sis._state["running"] = False  # Zustand fuer Folgetests aufraeumen


def test_store_ansichten_bestseller_zuerst_dann_sorten_und_preisbaender():
    """Ansichten-Katalog (19.08.): &page=N ist tot (Sonde: exakt dieselben 40) —
    stattdessen Sortierungen + Preisbaender derselben Liste."""
    urls = store._store_ansichten("123456")
    assert urls[0] == store.store_url("123456")           # Bestseller bleibt vorn
    assert any("created_desc" in u for u in urls)
    assert any("price_asc" in u for u in urls) and any("price_desc" in u for u in urls)
    assert any("minPrice=0&maxPrice=2" in u for u in urls)
    assert any(u.endswith("minPrice=35") for u in urls)   # offenes oberes Band
    assert len(urls) == len(set(urls))                    # keine Doppel-Ansichten


def _fake_ansichten_ernte(monkeypatch, ausbeute: dict, *, begriffe=None):
    """_seite_ernten je Ansicht stubben (Schluessel = Teilstring der URL).
    ``begriffe``: was die Begriffs-Ableitung fuer die In-Store-Suche liefert
    (Default: nichts — die Sortier-/Preisband-Tests bleiben fokussiert)."""
    urls = []

    async def fake(page, url, *, limit, **_k):
        urls.append(url)
        for key, ids in ausbeute.items():
            if key in url:
                return ids[:limit]
        return []

    monkeypatch.setattr(store, "_seite_ernten", fake)
    monkeypatch.setattr(store, "_suchbegriffe_aus_texten",
                        lambda texte, **k: list(begriffe or []))
    return urls


@pytest.mark.asyncio
async def test_store_ernte_vereinigt_ansichten(monkeypatch):
    """Jede Ansicht zeigt 'ihre' ~40 — die Union bringt die 100er-Ernte.
    Ueberlappungen werden dedupliziert, Bestseller-Reihenfolge bleibt vorn."""
    _install_fake_browser(monkeypatch, [["egal"]])
    urls = _fake_ansichten_ernte(monkeypatch, {
        "totalTranpro_desc": ["a1", "a2", "a3"],
        "created_desc": ["a2", "b1", "b2"],
        "price_asc&minPrice=0&maxPrice=2": ["c9"],
        "price_asc": ["c1"],
        "price_desc": ["a1", "c2"],
    })
    ids = await store.fetch_store_product_ids("123456", limit=8)
    assert ids == ["a1", "a2", "a3", "b1", "b2", "c1", "c2", "c9"]
    assert "sortType=totalTranpro_desc" in urls[0]


@pytest.mark.asyncio
async def test_store_ernte_stoppt_bei_erreichtem_limit(monkeypatch):
    _install_fake_browser(monkeypatch, [["egal"]])
    urls = _fake_ansichten_ernte(monkeypatch, {
        "totalTranpro_desc": [f"x{i}" for i in range(60)]})
    ids = await store.fetch_store_product_ids("123456", limit=4)
    assert len(ids) == 4
    assert len(urls) == 1         # Bestseller-Ansicht reicht, keine weiteren Ladungen


def test_suchbegriffe_aus_texten_liefert_produktwoerter():
    texte = ["Herz Halskette Damen Gold", "Halskette Anhänger Silber Damen",
             "Ohrringe Creolen Gold", "Ohrringe Damen Silber und mit für"]
    b = store._suchbegriffe_aus_texten(texte)
    assert "halskette" in b and "ohrringe" in b and "gold" in b
    assert "herz" not in b            # nur 1x -> kein tragfaehiger Begriff
    assert "und" not in b and "mit" not in b


@pytest.mark.asyncio
async def test_store_ernte_nutzt_in_store_suche(monkeypatch):
    """Sonde 19.08.: Sortierungen liefern dieselben 40 — die IN-STORE-SUCHE je
    Begriff aber ANDERE ~40. Begriffe kommen aus den Karten-Texten der Seite,
    die Suche laeuft direkt nach der Bestseller-Ansicht."""
    _install_fake_browser(monkeypatch, [["b1", "b2"]])
    urls = _fake_ansichten_ernte(monkeypatch, {
        "totalTranpro_desc": ["b1", "b2"],
        "SearchText=halskette": ["s1", "s2"],
    }, begriffe=["halskette"])
    ids = await store.fetch_store_product_ids("123456", limit=4)
    assert ids == ["b1", "b2", "s1", "s2"]
    assert "search?SearchText=halskette" in urls[1]


@pytest.mark.asyncio
async def test_store_ernte_bricht_nach_drei_leeren_ansichten_ab(monkeypatch):
    """Katalog erschoepft (oder Preisfilter wirkungslos): drei Ansichten ohne
    neue Ware beenden den Lauf — statt alle Preisbaender durchzuladen."""
    _install_fake_browser(monkeypatch, [["egal"]])
    urls = _fake_ansichten_ernte(monkeypatch, {"totalTranpro_desc": ["a1", "a2"]})
    ids = await store.fetch_store_product_ids("123456", limit=50)
    assert ids == ["a1", "a2"]
    assert len(urls) == 4         # Ansicht 1 + drei leere -> Schluss
