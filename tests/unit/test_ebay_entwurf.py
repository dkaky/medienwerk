"""Die drei Riegel fuer den Weg "Listing bei eBay anlegen".

Vorgeschichte: Es gab nur EINEN Knopf zu eBay, und der veroeffentlichte sofort.
Die Entwurfslogik (``draft_only=True``) lag ungenutzt im Dienst, weil keine
Adresse darauf zeigte. Zugleich ging beim Import die Kategorie "0" an eBay -
eine Nummer, die es nicht gibt. eBay lehnte ab (Fehler 25002), der Aufrufer
verbuchte das als blosse Warnung, und der Entwurf entstand nie.

Diese Tests halten die drei Reparaturen fest:

1. Es gibt einen Weg zu eBay, der NICHT veroeffentlicht.
2. Ohne Kategorie wird eine ermittelt - und wenn das misslingt, wird es gesagt
   statt verschluckt.
3. Veroeffentlichen braucht eine ausdrueckliche Bestaetigung.
"""
from __future__ import annotations

import asyncio

import pytest

from app.integrations.native_listing import NativeListingBackend


# --------------------------------------------------------------------------
# 2. Kategorie: nie wieder "0"
# --------------------------------------------------------------------------
class _EbayAttrappe:
    """Minimal-eBay: merkt sich, womit das Angebot angelegt wurde."""

    def __init__(self, vorschlag: str | None = "260012"):
        self.vorschlag = vorschlag
        self.benutzte_kategorie: str | None = None
        self.inventar_geloescht = False

    async def suggest_category(self, title):
        return self.vorschlag

    async def create_inventory_item(self, sku, **kw):
        return sku

    async def create_offer(self, sku, *, price_eur, category_id, quantity,
                           listing_description=None, **kw):
        self.benutzte_kategorie = category_id
        return "offer-1"

    async def delete_inventory_item(self, sku):
        self.inventar_geloescht = True


def _entwurf(ebay, *, category_id=None, titel="Angler T-Shirt Herren lustig"):
    backend = NativeListingBackend(ebay)
    return asyncio.run(backend.create_draft(
        sku="SKU-1", title_seo=titel, description="Beschreibung",
        image_urls=["https://example.invalid/a.jpg"],
        category_id=category_id, price_eur=19.99, quantity=5,
    ))


def test_ohne_kategorie_wird_eine_ermittelt():
    """Fehlt die Kategorie, fragt der Backend eBay - statt "0" zu schicken."""
    ebay = _EbayAttrappe(vorschlag="260012")
    _entwurf(ebay)
    assert ebay.benutzte_kategorie == "260012"


def test_null_kategorie_kommt_nie_bei_ebay_an():
    """Der eigentliche Fehler: die Zeichenkette "0" als Kategorie."""
    ebay = _EbayAttrappe(vorschlag="15687")
    _entwurf(ebay)
    assert ebay.benutzte_kategorie != "0"
    assert ebay.benutzte_kategorie


def test_vorhandene_kategorie_wird_nicht_ueberschrieben():
    """Wer eine Kategorie mitgibt, bekommt genau die - kein Vorschlag dazwischen."""
    ebay = _EbayAttrappe(vorschlag="999999")
    _entwurf(ebay, category_id="260012")
    assert ebay.benutzte_kategorie == "260012"


def test_ohne_ermittelbare_kategorie_wird_es_gesagt():
    """Findet eBay nichts, bricht es hoerbar ab statt still zu scheitern."""
    ebay = _EbayAttrappe(vorschlag=None)
    with pytest.raises(ValueError, match="Kategorie"):
        _entwurf(ebay)
    # Kein halbes Angebot: das Inventar-Item wurde gar nicht erst angelegt.
    assert ebay.benutzte_kategorie is None


# --------------------------------------------------------------------------
# 1. + 3. Die beiden Adressen: Entwurf ohne, Live nur mit Bestaetigung
# --------------------------------------------------------------------------
def _routen():
    from app.routers import products
    return {r.path: r for r in products.router.routes}


def test_entwurfs_adresse_existiert():
    """Der Entwurfsweg braucht eine Adresse - sonst bleibt die Logik unerreichbar."""
    assert "/api/v1/products/{listing_id}/ebay-draft" in _routen()


def test_entwurf_ruft_draft_only():
    """Die Adresse muss draft_only setzen, sonst veroeffentlicht sie doch."""
    import inspect
    from app.routers import products
    quelle = inspect.getsource(products.ebay_entwurf)
    assert "draft_only=True" in quelle


def test_publish_verlangt_bestaetigung():
    """Ein Fehlklick allein darf nicht veroeffentlichen."""
    import inspect
    from app.routers import products
    quelle = inspect.getsource(products.publish)
    assert "bestaetigt" in quelle
    assert "428" in quelle

# --------------------------------------------------------------------------
# 4. Der Attrappen-Betrieb muss halten, was er verspricht
# --------------------------------------------------------------------------
def test_attrappe_weist_den_bewussten_klick_nicht_mehr_ab(client):
    """Bei MOCK_EBAY=true darf ein bewusster Klick durch - Automatik nicht.

    GEAENDERTE REGEL (01.09.2026). Vorher wies dieser Test nach, dass BEIDE
    Knoepfe im Probebetrieb ein 409 bekommen. Nutzerwunsch, woertlich: "mock
    ebay kann ja gerne auf true bleiben, wennich ein entwurf live schalten will
    dann soll ich das doch duerfen."

    Der Probebetrieb behaelt seinen eigentlichen Zweck - nichts geht OHNE Zutun
    raus. Was faellt, ist nur die pauschale Abweisung am Router. Die Sperre
    selbst (``_NurLesenClient``) steht unveraendert und oeffnet sich einzig fuer
    ein Listing, fuer das eine Freigabe vorliegt; siehe die Tests in
    ``test_ebay_schreibsperre.py`` und ``test_freigabe.py``.

    Geprueft wird hier nur, dass der Weg NICHT MEHR mit 409 endet. Was danach
    kommt (422, weil Listing 1 in dieser leeren Test-DB nicht existiert), ist
    fuer diese Regel gleichgueltig.
    """
    for weg in ("/api/v1/products/1/ebay-draft",
                "/api/v1/products/1/publish?bestaetigt=true"):
        antwort = client.post(weg)
        assert antwort.status_code != 409, f"{weg} wird faelschlich noch abgewiesen"


def test_riegel_greift_vor_der_bestaetigungspruefung_nicht(client):
    """Die Reihenfolge stimmt: erst Bestaetigung, dann Attrappen-Riegel.

    Ein unbestaetigter Live-Klick muss weiterhin 428 bekommen - sonst lernt der
    Betreiber im Probebetrieb, dass ein Klick reicht, und wundert sich spaeter.
    """
    antwort = client.post("/api/v1/products/1/publish")
    assert antwort.status_code == 428
