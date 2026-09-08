"""Kontoansicht: jede Bewegung mit Beleg und Kategorie.

Ein Buchhaltungsprogramm denkt vom KONTO her — jeder Euro muss erklaert sein. Der
POD Shop war beleg-zentriert gebaut; Folge (gemessen 18.08.2026): 172 Buchungen ueber
−3.915,26 EUR ohne Kategorie und ohne Beleg, nicht einmal ansehbar.

Der heikelste Punkt: **nicht jede Bewegung braucht einen Beleg.** Privatentnahmen und
Umbuchungen brauchen eine Kategorie, aber keine Rechnung. Ohne diese Unterscheidung
jagt man Belegen hinterher, die es nie geben wird.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.models import BankTransaction, Invoice, OrderAliexpress
from app.retry import PersistentError
from app.services import konto_service

_lfd = iter(range(1, 10_000))


def _buchung(db, *, betrag="-25.00", gegenpartei="Temu", kategorie=None,
             order_id=None, invoices=None, match_info=None, tage_alt=1):
    tx = BankTransaction(
        bank_ref=f"kontist:{next(_lfd)}",
        transaction_date=datetime.now(timezone.utc).replace(microsecond=0),
        amount=Decimal(betrag), counterparty_name=gegenpartei,
        description="Kartenzahlung", kontierung=kategorie,
        kontierung_source="manuell" if kategorie else None,
        order_id=order_id, invoices=invoices, match_info=match_info)
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return tx


def test_zeigt_offene_abbuchung(db):
    _buchung(db)

    r = konto_service.uebersicht(db)

    assert r["zaehler"]["ohne_beleg"] == 1
    assert r["zaehler"]["ohne_kategorie"] == 1
    assert r["buchungen"][0]["gegenpartei"] == "Temu"
    assert r["buchungen"][0]["belegt"] is False


def test_gutschrift_braucht_keinen_beleg(db):
    """Einnahmen sind ueber den eBay-Finanzbericht nachgewiesen – sonst jagt man Phantome."""
    _buchung(db, betrag="1500.00", gegenpartei="eBay", kategorie="ebay_auszahlung")

    r = konto_service.uebersicht(db)

    assert r["zaehler"]["ohne_beleg"] == 0


def test_wareneinkauf_gilt_ueber_die_bestellung_als_belegt(db):
    o = OrderAliexpress(aliexpress_order_id="3070000000000001", status="delivered")
    db.add(o); db.commit(); db.refresh(o)
    _buchung(db, gegenpartei="AliExpress", kategorie="wareneinkauf", order_id=o.id)

    r = konto_service.uebersicht(db)

    assert r["zaehler"]["ohne_beleg"] == 0
    assert r["buchungen"][0]["beleg_nummer"] == "AE_3070000000000001"
    assert r["buchungen"][0]["beleg_art"] == "wareneinkauf"


def test_filter_zeigt_nur_die_arbeit(db):
    _buchung(db, gegenpartei="Temu")                                   # offen
    _buchung(db, gegenpartei="Kontist", kategorie="kontofuehrung")     # nur Kategorie

    assert len(konto_service.uebersicht(db, filter="alle")["buchungen"]) == 2
    ohne = konto_service.uebersicht(db, filter="ohne_beleg")["buchungen"]
    assert {b["gegenpartei"] for b in ohne} == {"Temu", "Kontist"}
    assert len(konto_service.uebersicht(db, filter="ohne_kategorie")["buchungen"]) == 1


def test_summe_der_offenen_wird_ausgewiesen(db):
    """Die Zahl, die weh tut: wie viel Geld ist unerklaert abgeflossen."""
    _buchung(db, betrag="-100.00")
    _buchung(db, betrag="-23.50")

    r = konto_service.uebersicht(db)

    assert r["summen"]["ohne_beleg"] == pytest.approx(-123.50)


# ------------------------------------------------- kein Beleg noetig
def test_privatentnahme_gilt_als_erklaert(db):
    tx = _buchung(db, betrag="-500.00", gegenpartei="Ueberweisung")

    konto_service.kein_beleg_noetig(db, tx_id=tx.id, grund="Privatentnahme")

    r = konto_service.uebersicht(db)
    assert r["zaehler"]["ohne_beleg"] == 0
    assert r["buchungen"][0]["grund_kein_beleg"] == "Privatentnahme"
    assert r["buchungen"][0]["beleg_art"] == "kein_beleg_noetig"


def test_ohne_begruendung_geht_es_nicht(db):
    """Ohne Beleg UND ohne Begruendung waere die Buchung nicht pruefbar."""
    tx = _buchung(db)

    with pytest.raises(PersistentError):
        konto_service.kein_beleg_noetig(db, tx_id=tx.id, grund="   ")


def test_laesst_sich_zuruecknehmen(db):
    tx = _buchung(db)
    konto_service.kein_beleg_noetig(db, tx_id=tx.id, grund="doch nicht")

    konto_service.kein_beleg_noetig(db, tx_id=tx.id, rueckgaengig=True)

    assert konto_service.uebersicht(db)["zaehler"]["ohne_beleg"] == 1


# ------------------------------------------------- Kategorie
def test_kategorie_von_hand(db):
    tx = _buchung(db)

    r = konto_service.setze_kategorie(db, tx_id=tx.id, kategorie="it_hosting")

    assert r["kategorie"] == "it_hosting"
    assert r["quelle"] == "manuell"        # Regeln fassen das nie wieder an


def test_erfundene_kategorie_wird_abgelehnt(db):
    tx = _buchung(db)

    with pytest.raises(PersistentError):
        konto_service.setze_kategorie(db, tx_id=tx.id, kategorie="phantasie")


# ------------------------------------------------- Beleg hochladen
def test_beleg_haengt_danach_an_der_buchung(db):
    tx = _buchung(db, betrag="-49.90", gegenpartei="Temu")

    r = konto_service.beleg_zu_buchung(db, tx_id=tx.id, file_bytes=b"PDF", ext="pdf",
                                       kategorie="wareneinkauf", beschreibung="Temu-Ware")

    assert r["betrag"] == pytest.approx(49.90)     # Vorzeichen gedreht, Betrag der Buchung
    db.refresh(tx)
    assert tx.invoices and r["beleg_id"] in tx.invoices
    assert konto_service.uebersicht(db)["zaehler"]["ohne_beleg"] == 0


def test_beleg_uebernimmt_datum_und_betrag_der_buchung(db):
    """Nicht abtippen lassen – die Buchung ist die Wahrheit."""
    tx = _buchung(db, betrag="-17.25")

    r = konto_service.beleg_zu_buchung(db, tx_id=tx.id, file_bytes=b"PDF", ext="pdf")

    inv = db.get(Invoice, r["beleg_id"])
    assert float(inv.amount) == pytest.approx(17.25)
    assert inv.invoice_date.date() == tx.transaction_date.date()


def test_nur_gespiegelte_kontist_buchungen(db):
    """Alt-/Testdaten ohne kontist:-Kennung wuerden das Bild verfaelschen."""
    db.add(BankTransaction(bank_ref="alt:1", amount=Decimal("-9.99"),
                           transaction_date=datetime.now(timezone.utc)))
    db.commit()

    assert konto_service.uebersicht(db)["zaehler"]["alle"] == 0


# ------------------------------------------- Belege aus Kontist uebernehmen
class _FakeKontist:
    """Kontist mit einer Buchung, an der ein Beleg haengt."""

    def __init__(self, knoten, datei=b"%PDF-1.4 Rechnung"):
        self._knoten = knoten
        self._datei = datei
        self.geladen = []

    async def fetch_transaction_assets(self, *a, **k):
        return self._knoten

    async def lade_anhang(self, url):
        self.geladen.append(url)
        return self._datei


@pytest.fixture
def kontist_mit_beleg(monkeypatch):
    def _setze(tx, *, name="Rechnung Shine.pdf", filetype="application/pdf"):
        knoten = [{"id": str(tx.bank_ref).split("kontist:", 1)[-1],
                   "hasAssets": True, "receiptName": name,
                   "assets": [{"id": "a1", "name": name, "filetype": filetype,
                               "fullsize": "https://kontist.example/a1"}]}]
        fake = _FakeKontist(knoten)
        from app.integrations import kontist as k
        monkeypatch.setattr(k, "fetch_transaction_assets",
                            fake.fetch_transaction_assets, raising=False)
        monkeypatch.setattr(k, "lade_anhang", fake.lade_anhang, raising=False)
        return fake
    return _setze


@pytest.mark.asyncio
async def test_vorschau_aendert_nichts(db, kontist_mit_beleg):
    tx = _buchung(db, betrag="-41.28", gegenpartei="Shine Germany GmbH")
    kontist_mit_beleg(tx)

    r = await konto_service.belege_von_kontist(db)

    assert r["geprueft"] == 1
    assert r["uebernommen"] == 0
    db.refresh(tx)
    assert not tx.invoices


@pytest.mark.asyncio
async def test_beleg_wird_uebernommen(db, kontist_mit_beleg):
    tx = _buchung(db, betrag="-41.28", gegenpartei="Shine Germany GmbH")
    kontist_mit_beleg(tx)

    r = await konto_service.belege_von_kontist(db, anwenden=True)

    assert r["uebernommen"] == 1
    db.refresh(tx)
    assert tx.invoices and tx.status == "invoiced"
    inv = db.get(Invoice, tx.invoices[0])
    assert float(inv.amount) == pytest.approx(41.28)   # Betrag aus der BUCHUNG
    assert konto_service.uebersicht(db)["zaehler"]["ohne_beleg"] == 0


@pytest.mark.asyncio
async def test_zweiter_lauf_laedt_nicht_erneut(db, kontist_mit_beleg):
    """Wiederholbar: was schon einen Beleg hat, wird uebersprungen."""
    tx = _buchung(db, betrag="-41.28")
    fake = kontist_mit_beleg(tx)
    await konto_service.belege_von_kontist(db, anwenden=True)

    r = await konto_service.belege_von_kontist(db, anwenden=True)

    assert r["schon_da"] == 1
    assert r["uebernommen"] == 0
    assert len(fake.geladen) == 1          # nur EIN Download insgesamt


@pytest.mark.asyncio
async def test_alte_buchungen_bleiben_aussen_vor(db, kontist_mit_beleg):
    """Nutzer-Vorgabe: nicht die ganze Historie nachziehen."""
    tx = _buchung(db, betrag="-41.28")
    tx.transaction_date = datetime(2026, 1, 5, tzinfo=timezone.utc)
    db.commit()
    kontist_mit_beleg(tx)

    r = await konto_service.belege_von_kontist(
        db, seit=datetime(2026, 8, 1, tzinfo=timezone.utc), anwenden=True)

    assert r["geprueft"] == 0
    assert r["uebernommen"] == 0


@pytest.mark.asyncio
async def test_fehler_stoppt_den_lauf_nicht(db, kontist_mit_beleg, monkeypatch):
    tx1 = _buchung(db, betrag="-10.00")
    fake = kontist_mit_beleg(tx1)

    async def _kaputt(url):
        raise RuntimeError("Download abgelehnt")
    from app.integrations import kontist as k
    monkeypatch.setattr(k, "lade_anhang", _kaputt, raising=False)

    r = await konto_service.belege_von_kontist(db, anwenden=True)

    assert r["uebernommen"] == 0
    assert len(r["fehler"]) == 1
    assert "Download abgelehnt" in r["fehler"][0]["grund"]


@pytest.mark.asyncio
async def test_nachtlauf_holt_nur_frische_belege(db, kontist_mit_beleg, monkeypatch):
    """„Neue ab jetzt" – die Historie soll NICHT nachgezogen werden."""
    alt = _buchung(db, betrag="-10.00", gegenpartei="Alt")
    alt.transaction_date = datetime.now(timezone.utc) - __import__("datetime").timedelta(days=90)
    neu = _buchung(db, betrag="-20.00", gegenpartei="Neu")
    db.commit()

    knoten = []
    for tx in (alt, neu):
        knoten.append({"id": str(tx.bank_ref).split("kontist:", 1)[-1],
                       "hasAssets": True, "receiptName": "R.pdf",
                       "assets": [{"id": "a", "name": "R.pdf", "filetype": "pdf",
                                   "fullsize": "https://kontist.example/a"}]})
    fake = _FakeKontist(knoten)
    from app.integrations import kontist as k
    monkeypatch.setattr(k, "fetch_transaction_assets", fake.fetch_transaction_assets,
                        raising=False)
    monkeypatch.setattr(k, "lade_anhang", fake.lade_anhang, raising=False)

    r = await konto_service.belege_von_kontist(
        db, seit=datetime.now(timezone.utc) - __import__("datetime").timedelta(days=30),
        anwenden=True)

    assert r["uebernommen"] == 1
    db.refresh(alt); db.refresh(neu)
    assert not alt.invoices          # die alte bleibt unangetastet
    assert neu.invoices


def test_hochgeladene_rechnung_ist_anklickbar(db):
    """Ohne Beleg-ID kann die Zeile nicht verlinken – dann sieht man sie nie wieder."""
    tx = _buchung(db, betrag="-41.28", gegenpartei="Shine Germany GmbH")
    konto_service.beleg_zu_buchung(db, tx_id=tx.id, file_bytes=b"PDF", ext="pdf")

    zeile = konto_service.uebersicht(db)["buchungen"][0]

    assert zeile["belegt"] is True
    assert zeile["beleg_url"], "ohne Verweis kein Klick auf die Rechnung"
    assert db.get(Invoice, zeile["beleg_ids"][0]) is not None


def test_wareneinkauf_zeigt_die_RECHNUNG_nicht_den_beleg(db):
    """Fuer die Buchhaltung zaehlt die Rechnung – der Beleg ist nur ihr Nachweis.

    Beim Wareneinkauf gibt es beides: das Original von AliExpress (``file_path``)
    und die daraus erzeugte Rechnung (``generated_path``). Der Klick muss auf die
    Rechnung fuehren.
    """
    o = OrderAliexpress(aliexpress_order_id="3070000000000042", status="delivered")
    db.add(o); db.commit(); db.refresh(o)
    inv = Invoice(type="aliexpress_purchase", reference_id="3070000000000042",
                  invoice_number="AE_3070000000000042", is_original=True,
                  file_path="invoices/2026-08/original_3070000000000042.png",
                  generated_path="invoices/2026-08/rechnung_3070000000000042.html")
    db.add(inv); db.commit(); db.refresh(inv)
    _buchung(db, gegenpartei="AliExpress", kategorie="wareneinkauf", order_id=o.id)

    zeile = konto_service.uebersicht(db)["buchungen"][0]

    assert zeile["beleg_nummer"] == "AE_3070000000000042"
    assert zeile["beleg_url"] == f"/api/v1/invoices/{inv.id}/rechnung"


def test_ohne_erzeugte_rechnung_fuehrt_der_klick_auf_den_beleg(db):
    """Ersatzweise – besser der Beleg als gar nichts."""
    o = OrderAliexpress(aliexpress_order_id="3070000000000043", status="delivered")
    db.add(o); db.commit(); db.refresh(o)
    inv = Invoice(type="aliexpress_purchase", reference_id="3070000000000043",
                  invoice_number="AE_3070000000000043", is_original=True,
                  file_path="invoices/2026-08/original_3070000000000043.png")
    db.add(inv); db.commit(); db.refresh(inv)
    _buchung(db, gegenpartei="AliExpress", kategorie="wareneinkauf", order_id=o.id)

    zeile = konto_service.uebersicht(db)["buchungen"][0]

    assert zeile["beleg_url"] == f"/api/v1/invoices/{inv.id}/download"


# --------------------------------- „Rechnung liegt vor, Zuordnung offen"
def test_wareneinkauf_ohne_zuordnung_ist_nicht_dasselbe_wie_ohne_beleg(db):
    """Der wichtigste Unterschied in der ganzen Ansicht.

    Bei AliExpress LIEGT die Rechnung in der Belegablage — nur welche Bestellung zu
    welcher Abbuchung gehoert, konnte der Abgleich nicht eindeutig entscheiden
    (gleiche Betraege am selben Tag). Das in denselben Topf zu werfen wie eine
    Temu-Abbuchung ohne jeden Beleg wuerde die echte Arbeit unsichtbar machen:
    371 statt 162.
    """
    _buchung(db, gegenpartei="Aliexpress.com", kategorie="wareneinkauf")
    _buchung(db, gegenpartei="Temu.com")

    r = konto_service.uebersicht(db)

    assert r["zaehler"]["zuordnung_offen"] == 1
    assert r["zaehler"]["ohne_beleg"] == 1        # nur Temu


def test_filter_trennt_die_beiden_faelle(db):
    _buchung(db, gegenpartei="Aliexpress.com", kategorie="wareneinkauf")
    _buchung(db, gegenpartei="Temu.com")

    zuord = konto_service.uebersicht(db, filter="zuordnung_offen")["buchungen"]
    offen = konto_service.uebersicht(db, filter="ohne_beleg")["buchungen"]

    assert [b["gegenpartei"] for b in zuord] == ["Aliexpress.com"]
    assert [b["gegenpartei"] for b in offen] == ["Temu.com"]


def test_zugeordneter_wareneinkauf_ist_erledigt(db):
    """Sobald die Bestellung dranhaengt, ist es kein Sonderfall mehr."""
    o = OrderAliexpress(aliexpress_order_id="3070000000000077", status="delivered")
    db.add(o); db.commit(); db.refresh(o)
    _buchung(db, gegenpartei="Aliexpress.com", kategorie="wareneinkauf", order_id=o.id)

    r = konto_service.uebersicht(db)

    assert r["zaehler"]["zuordnung_offen"] == 0
    assert r["zaehler"]["erledigt"] == 1


# ------------------------------------------------- Stichtag 1.1.2026
def test_buchungen_vor_2026_bleiben_draussen(db):
    """Davor liegt Kontohistorie, die mit diesem Geschaeftsjahr nichts zu tun hat."""
    alt = _buchung(db, betrag="-999.00", gegenpartei="Alt")
    alt.transaction_date = datetime(2025, 11, 20, tzinfo=timezone.utc)
    _buchung(db, betrag="-10.00", gegenpartei="Neu")
    db.commit()

    r = konto_service.uebersicht(db)

    assert r["zaehler"]["alle"] == 1
    assert [b["gegenpartei"] for b in r["buchungen"]] == ["Neu"]


def test_der_1_januar_ist_drin(db):
    """Stichtag heisst AB dem 1.1., nicht danach."""
    tx = _buchung(db, betrag="-5.00", gegenpartei="Neujahr")
    tx.transaction_date = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db.commit()

    assert konto_service.uebersicht(db)["zaehler"]["alle"] == 1


def test_summen_zaehlen_nur_ab_stichtag(db):
    """Sonst haengen alte Abbuchungen als „ohne Beleg" in den Zahlen."""
    alt = _buchung(db, betrag="-500.00")
    alt.transaction_date = datetime(2025, 6, 1, tzinfo=timezone.utc)
    _buchung(db, betrag="-20.00")
    db.commit()

    r = konto_service.uebersicht(db)

    assert r["summen"]["ohne_beleg"] == pytest.approx(-20.00)


def test_ohne_datum_verschwindet_nicht_stillschweigend(db):
    """Nicht einzuordnen ist kein Grund, sie unsichtbar zu machen."""
    tx = _buchung(db, betrag="-7.00", gegenpartei="Ohne Datum")
    tx.transaction_date = None
    db.commit()

    r = konto_service.uebersicht(db)

    assert [b["gegenpartei"] for b in r["buchungen"]] == ["Ohne Datum"]


def test_stichtag_laesst_sich_verschieben(db):
    """Fuer den Blick zurueck – die Daten sind ja da."""
    alt = _buchung(db, betrag="-500.00", gegenpartei="Alt")
    alt.transaction_date = datetime(2025, 6, 1, tzinfo=timezone.utc)
    db.commit()

    r = konto_service.uebersicht(db, ab=datetime(2025, 1, 1, tzinfo=timezone.utc))

    assert r["zaehler"]["alle"] == 1


# ------------------------------------------------- Aktualisieren auf Knopfdruck
@pytest.fixture
def kontist_bereit(monkeypatch):
    from app.integrations import kontist as k
    monkeypatch.setattr(k, "is_configured", lambda: True)
    monkeypatch.setattr(k, "is_connected", lambda: True)


@pytest.mark.asyncio
async def test_ohne_verbindung_klare_ansage(db, monkeypatch):
    from app.integrations import kontist as k
    monkeypatch.setattr(k, "is_connected", lambda: False)
    monkeypatch.setattr(k, "is_configured", lambda: True)

    with pytest.raises(PersistentError):
        await konto_service.aktualisieren(db)


@pytest.mark.asyncio
async def test_zaehlt_was_dazugekommen_ist(db, kontist_bereit, monkeypatch):
    """Die Zahlen im Hinweis muessen aus den ECHTEN Rueckgaben kommen.

    Die Dienste heissen ihre Felder unterschiedlich ("new", "kontiert_neu",
    "zugeordnet") — wer hier daneben greift, zeigt stillschweigend Nullen an.
    """
    from app.services import bank_sync_service, kontierung_service

    async def _spiegeln(db_): return {"fetched": 12, "new": 4}
    monkeypatch.setattr(bank_sync_service, "sync_bank_transactions", _spiegeln)
    monkeypatch.setattr(kontierung_service, "kontiere_neue",
                        lambda db_: {"kontiert_neu": 3, "unkontiert": 9})
    monkeypatch.setattr(bank_sync_service, "match_gleicher_tag",
                        lambda db_, **k: {"geprueft": 5, "zugeordnet": 2})

    async def _belege(db_, **k): return {"uebernommen": 1}
    monkeypatch.setattr(konto_service, "belege_von_kontist", _belege)

    r = await konto_service.aktualisieren(db)

    assert r == {"neu": 4, "kontiert": 3, "zugeordnet": 2, "belege": 1, "fehler": []}


@pytest.mark.asyncio
async def test_ein_ausfall_stoppt_die_anderen_schritte_nicht(db, kontist_bereit,
                                                             monkeypatch):
    """Sonst verliert man wegen einer Kleinigkeit den ganzen Abruf."""
    from app.services import bank_sync_service, kontierung_service

    async def _spiegeln(db_): return {"new": 2}
    monkeypatch.setattr(bank_sync_service, "sync_bank_transactions", _spiegeln)

    def _kaputt(db_):
        raise RuntimeError("Regelwerk defekt")
    monkeypatch.setattr(kontierung_service, "kontiere_neue", _kaputt)
    monkeypatch.setattr(bank_sync_service, "match_gleicher_tag",
                        lambda db_, **k: {"zugeordnet": 7})

    async def _belege(db_, **k): return {"uebernommen": 0}
    monkeypatch.setattr(konto_service, "belege_von_kontist", _belege)

    r = await konto_service.aktualisieren(db)

    assert r["neu"] == 2 and r["zugeordnet"] == 7      # lief trotzdem durch
    assert len(r["fehler"]) == 1
    assert "Kontieren" in r["fehler"][0]


# ----------------------- "Zuordnung offen" haengt an der GEGENPARTEI, nicht an der
#                         Kategorie (Fund von Wajjahat, 19.08.)
def test_temu_bleibt_eine_echte_luecke(db):
    """Fuer Temu gibt es KEINE Rechnung in der Belegablage.

    Vorher hing der Zustand an ``kontierung == "wareneinkauf"``. Seit Temu dieselbe
    Kategorie traegt, behauptete die Zeile „Rechnung liegt vor" — und verschwand
    damit aus genau der Ansicht, in der man die Luecke bearbeitet haette.
    """
    _buchung(db, gegenpartei="Temu.com", kategorie="wareneinkauf")

    r = konto_service.uebersicht(db)

    assert r["zaehler"]["ohne_beleg"] == 1
    assert r["zaehler"]["zuordnung_offen"] == 0
    assert r["buchungen"][0]["beleg_art"] == "offen"


@pytest.mark.parametrize("name", ["Aliexpress.com", "aliexpress", "Aliexpresscom",
                                  "www aliexpress com", "Aliexpress"])
def test_alle_aliexpress_schreibweisen(db, name):
    """Das Konto fuehrt fuenf verschiedene Schreibweisen — alle muessen greifen."""
    _buchung(db, gegenpartei=name, kategorie="wareneinkauf")

    assert konto_service.uebersicht(db)["zaehler"]["zuordnung_offen"] == 1


def test_qksource_ist_auch_eine_luecke(db):
    _buchung(db, gegenpartei="Qksource.com", kategorie="wareneinkauf")

    assert konto_service.uebersicht(db)["zaehler"]["ohne_beleg"] == 1


def test_kategorie_aendert_den_zustand_nicht_mehr(db):
    """Kontieren darf eine Zeile nicht aus „Ohne Beleg" wegzaubern."""
    tx = _buchung(db, gegenpartei="Temu.com")
    assert konto_service.uebersicht(db)["zaehler"]["ohne_beleg"] == 1

    konto_service.setze_kategorie(db, tx_id=tx.id, kategorie="wareneinkauf")

    assert konto_service.uebersicht(db)["zaehler"]["ohne_beleg"] == 1
