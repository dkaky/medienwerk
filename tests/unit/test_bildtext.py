"""Aufdrucke von den Bildern lesen - und nichts behaupten, was nicht gelesen wurde.

Nutzerwunsch vom 03.09.2026: der Spruch steht bei Motiv-Bekleidung fast immer
NUR auf dem Bild. Ohne diese Analyse kann die Titelregel ihn nicht in den Titel
schreiben - und sie darf ihn nicht erfinden.

Die beiden wichtigsten Tests hier stammen aus echten Fehlern beim ersten Lauf:

* ``test_gescheiterter_aufruf_wird_nicht_als_ergebnis_gespeichert`` - der Aufruf
  scheiterte an einem 401, und das Ergebnis wurde als "kein Aufdruck gefunden"
  festgehalten. Der Artikel waere danach nie wieder angesehen worden, und das
  System haette dauerhaft behauptet, ein Motiv-Shirt trage keinen Aufdruck.
* ``test_kostenbremse_zaehlt_auch_fehlversuche`` - der Deckel zaehlte nur
  Erfolge. Bei 28 Fehlversuchen lief der Lauf durch alle Artikel, obwohl drei
  gewuenscht waren. Eine Kostenbremse, die nur bei Erfolg bremst, ist keine.
"""
from __future__ import annotations

import asyncio

import pytest

from app.models import Listing, Product
from app.services import bildtext


class _Llm:
    """Antwortet nach Vorgabe und zaehlt, wie oft sie gefragt wurde."""

    def __init__(self, antwort):
        self.antwort = antwort
        self.aufrufe = 0

    async def lies_aufdruck(self, *, product_title, image_urls):
        self.aufrufe += 1
        return dict(self.antwort)


def _entwurf(db, kennung="bt-1", bilder=("https://example.invalid/a.jpg",)):
    p = Product(aliexpress_url=f"https://example.invalid/i/{kennung}",
                aliexpress_id=kennung, title_raw="T-Shirt", images=list(bilder))
    db.add(p)
    db.commit()
    l = Listing(product_id=p.id, title_seo="Motiv-Shirt Papa",
                description="Ein Shirt.",      # Pflichtfeld
                listing_status="draft", price_eur=19.95)
    db.add(l)
    db.commit()
    return l


def test_gefundener_aufdruck_wird_festgehalten(db):
    l = _entwurf(db)
    llm = _Llm({"text": "LECKER BIERCHEN", "sicher": True,
                "sprache": "de", "zielgruppe": "Bierfreunde"})
    daten = asyncio.run(bildtext.analysiere(db, listing_id=l.id, llm=llm))
    assert daten["text"] == "LECKER BIERCHEN"
    assert bildtext.gelesen(db, l.id)["text"] == "LECKER BIERCHEN"


def test_kein_aufdruck_ist_auch_ein_ergebnis(db):
    """Sonst kostet derselbe Artikel bei jedem Lauf erneut Geld."""
    l = _entwurf(db, "bt-2")
    llm = _Llm({"text": "", "sicher": False, "sprache": "", "zielgruppe": ""})
    asyncio.run(bildtext.analysiere(db, listing_id=l.id, llm=llm))
    gemerkt = bildtext.gelesen(db, l.id)
    assert gemerkt is not None and gemerkt["text"] == ""


def test_gescheiterter_aufruf_wird_nicht_als_ergebnis_gespeichert(db):
    """Der Fehler vom ersten Lauf: 401 wurde als "kein Aufdruck" festgehalten."""
    l = _entwurf(db, "bt-3")
    llm = _Llm({"text": "", "sicher": False, "fehler": "401 API key is invalid"})
    with pytest.raises(RuntimeError):
        asyncio.run(bildtext.analysiere(db, listing_id=l.id, llm=llm))
    assert bildtext.gelesen(db, l.id) is None, (
        "Ein gescheiterter Aufruf wurde als Ergebnis festgehalten - der Artikel "
        "wuerde nie wieder angesehen.")


def test_ohne_bilder_wird_nichts_gefragt(db):
    l = _entwurf(db, "bt-4", bilder=())
    llm = _Llm({"text": "sollte nie kommen"})
    daten = asyncio.run(bildtext.analysiere(db, listing_id=l.id, llm=llm))
    assert daten["text"] == "" and llm.aufrufe == 0


def test_kostenbremse_zaehlt_auch_fehlversuche(db):
    """Jeder Versuch kostet - auch der gescheiterte."""
    for i in range(6):
        _entwurf(db, f"bt-brems-{i}")
    llm = _Llm({"text": "", "fehler": "kaputt"})
    ergebnis = asyncio.run(bildtext.analysiere_alle(db, max_bilder=2, llm=llm))
    assert llm.aufrufe == 2, f"Deckel 2, aber {llm.aufrufe} Aufrufe"
    assert ergebnis["fehler"] == 2


def test_bereits_gelesene_werden_uebersprungen(db):
    l = _entwurf(db, "bt-5")
    llm = _Llm({"text": "EINMAL", "sicher": True})
    asyncio.run(bildtext.analysiere(db, listing_id=l.id, llm=llm))
    ergebnis = asyncio.run(bildtext.analysiere_alle(db, max_bilder=10, llm=llm))
    assert ergebnis["schon_gelesen"] == 1
    assert llm.aufrufe == 1, "Derselbe Artikel wurde ein zweites Mal bezahlt"


def test_laeuft_nur_ueber_entwuerfe(db):
    entwurf = _entwurf(db, "bt-6")
    live = _entwurf(db, "bt-7")
    live.listing_status = "active"
    db.commit()
    llm = _Llm({"text": "X", "sicher": True})
    asyncio.run(bildtext.analysiere_alle(db, max_bilder=10, llm=llm))
    assert bildtext.gelesen(db, entwurf.id) is not None
    assert bildtext.gelesen(db, live.id) is None


def test_attrappe_erfindet_keinen_aufdruck():
    """Ein Mock mit Beispieltext wuerde im Probebetrieb einen Titel verfaelschen."""
    from app.integrations.llm import MockLLMClient

    daten = asyncio.run(MockLLMClient().lies_aufdruck(
        product_title="T-Shirt", image_urls=["https://example.invalid/a.jpg"]))
    assert daten["text"] == ""
    assert daten["sicher"] is False
