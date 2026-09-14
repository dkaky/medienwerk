"""eBay-Loeschmeldungen abholen - netzwerkfrei.

Die zwei Zusagen, auf die es ankommt:
  * Eine echte Meldung zu einem eigenen Kaeufer loescht dessen Kontaktdaten - und
    nur dessen.
  * Keine Meldung geht verloren, weil eine Pruefung gerade nicht moeglich war.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import Settings
from app.database import SessionLocal
from app.models import Sale
from app.services import ebay_loeschmeldungen as lm

URL = "https://beispiel.supabase.co/functions/v1/ebay-deletion"
JETZT = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


def _settings(**over) -> Settings:
    base = dict(ebay_loesch_abhol_url=URL, ebay_loesch_abhol_token="abhol-geheim")
    base.update(over)
    return Settings(**base)


def _meldung(mid: int, username: str, alter_tage: float = 0) -> dict:
    roh = json.dumps({"notification": {"data": {"username": username, "userId": f"id-{username}"}}})
    eingang = (JETZT - timedelta(days=alter_tage)).isoformat()
    return {"id": mid, "eingegangen_am": eingang, "signatur": "sig", "roh": roh}


class _Funktion:
    """Nachbau der Supabase-Funktion: liefert Meldungen, merkt sich Quittungen."""

    def __init__(self, meldungen: list[dict], status: int = 200) -> None:
        self.meldungen, self.status, self.quittiert, self.token = meldungen, status, [], None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.token = request.headers.get("x-abhol-token")
        if request.method == "GET" and "abholen" in request.url.params:
            return httpx.Response(self.status, json={"meldungen": self.meldungen})
        if request.method == "POST" and "quittieren" in request.url.params:
            self.quittiert += json.loads(request.content)["ids"]
            return httpx.Response(200, json={"geloescht": len(self.quittiert)})
        return httpx.Response(404)


@pytest.fixture
def db():
    sitzung = SessionLocal()
    yield sitzung
    sitzung.close()


def _verkauf(db, name: str) -> Sale:
    sale = Sale(ebay_transaction_id=f"T-{name}", buyer_name=name, buyer_email=f"{name}@mail.de",
                delivery_address={"strasse": "Hauptstr. 1", "ort": "Bonn"})
    db.add(sale)
    db.commit()
    return sale


async def _lauf(db, funktion, pruefe=lambda roh, sig: True, **settings_over):
    return await lm.hole_und_verarbeite(db, s=_settings(**settings_over),
                                        transport=httpx.MockTransport(funktion),
                                        pruefe_signatur=pruefe, jetzt=JETZT)


async def test_eigener_kaeufer_wird_anonymisiert_fremder_nicht(db):
    eigener = _verkauf(db, "kaeufer_eigen")
    anderer = _verkauf(db, "kaeufer_bleibt")
    f = _Funktion([_meldung(1, "kaeufer_eigen"), _meldung(2, "irgendwer_fremd")])

    z = await _lauf(db, f)

    db.refresh(eigener)
    db.refresh(anderer)
    assert eigener.buyer_email is None and eigener.delivery_address is None
    assert anderer.buyer_email == "kaeufer_bleibt@mail.de"
    assert z["anonymisiert"] == 1 and z["fremde_konten"] == 1
    assert f.quittiert == [1, 2]
    assert f.token == "abhol-geheim"


async def test_nicht_pruefbare_meldung_bleibt_liegen(db):
    """Netzaussetzer bei eBay: nichts quittieren, beim naechsten Lauf erneut."""
    _verkauf(db, "kaeufer_eigen")
    f = _Funktion([_meldung(1, "kaeufer_eigen")])

    def kaputt(roh, sig):
        raise RuntimeError("eBay-Schluessel nicht ladbar")

    z = await _lauf(db, f, pruefe=kaputt)
    assert z["zurueckgestellt"] == 1
    assert f.quittiert == []


async def test_ungueltige_signatur_erst_nach_frist_verworfen(db):
    kunde = _verkauf(db, "kaeufer_eigen")
    f = _Funktion([_meldung(1, "kaeufer_eigen", alter_tage=0.5),
                   _meldung(2, "kaeufer_eigen", alter_tage=lm.VERWERFEN_NACH_TAGEN + 1)])

    z = await _lauf(db, f, pruefe=lambda roh, sig: False)

    db.refresh(kunde)
    assert kunde.buyer_email == "kaeufer_eigen@mail.de"   # nichts auf Verdacht geloescht
    assert z["zurueckgestellt"] == 1 and z["verworfen"] == 1
    assert f.quittiert == [2]


async def test_falscher_abholschluessel_meldet_klartext(db):
    with pytest.raises(lm.AbholFehler, match="EBAY_LOESCH_ABHOL_TOKEN"):
        await _lauf(db, _Funktion([], status=401))


async def test_ohne_einrichtung_klare_meldung(db):
    with pytest.raises(lm.AbholFehler, match="fehlen"):
        await lm.hole_und_verarbeite(db, s=_settings(ebay_loesch_abhol_url=""),
                                     pruefe_signatur=lambda r, s: True)


async def test_leere_warteschlange_quittiert_nichts(db):
    f = _Funktion([])
    z = await _lauf(db, f)
    assert z["abgeholt"] == 0 and f.quittiert == []
