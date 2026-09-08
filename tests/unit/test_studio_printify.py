"""Tests der Printify-Anbindung.

Kein Test geht ins Netz - der Client wird durch einen Doppelgaenger ersetzt.
"""

from __future__ import annotations

import asyncio

import pytest

from app.studio.printify import fit_scale
from app.studio.printify.products import PRODUCT_TYPES
from app.studio.printify.service import erstelle_produkt, produkttyp, verkaufspreis_cents


# --- Passform-Rechnung --------------------------------------------------------

def test_hochkant_motiv_auf_breitem_bereich_wird_verkleinert():
    """Der Kern der ganzen Anbindung.

    Ein hochkantes Motiv auf einem breiten Druckbereich (Tassen-Wickeldruck)
    muss verkleinert werden, sonst laeuft es oben und unten hinaus. Genau dafuer
    gibt es diese Rechnung statt eines festen Wertes.
    """
    varianten = [{"placeholders": [{"position": "front", "width": 2000, "height": 1000}]}]
    assert fit_scale(varianten, "front", (1024, 1536)) == pytest.approx(0.3333, abs=0.001)


def test_passendes_motiv_bleibt_unveraendert():
    varianten = [{"placeholders": [{"position": "front", "width": 1000, "height": 2000}]}]
    assert fit_scale(varianten, "front", (1024, 1536)) == 1.0


def test_ohne_masse_konservativ():
    """Lieber unveraendert lassen als auf Verdacht verkleinern."""
    varianten = [{"placeholders": [{"position": "front", "width": 2000, "height": 1000}]}]
    assert fit_scale(varianten, "front", None) == 1.0


def test_restriktivste_variante_gewinnt():
    """Das Motiv muss in ALLE Varianten passen, nicht nur in die erste."""
    varianten = [
        {"placeholders": [{"position": "front", "width": 1000, "height": 2000}]},
        {"placeholders": [{"position": "front", "width": 2000, "height": 1000}]},
    ]
    assert fit_scale(varianten, "front", (1024, 1536)) < 0.5


# --- Preisrechnung ------------------------------------------------------------

def test_preis_deckt_die_druckkosten():
    for typ in PRODUCT_TYPES.values():
        assert verkaufspreis_cents(typ) > typ.landed_cost_cents


def test_preis_hat_die_uebliche_endung():
    """Dieselbe Preisendung wie im Handelsteil - ein Shop, eine Sprache."""
    for typ in PRODUCT_TYPES.values():
        assert verkaufspreis_cents(typ) % 100 in (95, 99)


def test_unbekannter_produkttyp_wird_abgelehnt():
    with pytest.raises(ValueError, match="Unbekannter Produkttyp"):
        produkttyp("fliegender-teppich")


# --- Produkt anlegen ----------------------------------------------------------

class FakePrintify:
    """Doppelgaenger: merkt sich, was gesendet wurde."""

    def __init__(self):
        self.gesendet = None

    async def bild_hochladen(self, dateiname, inhalt):
        return {"id": "bild-123", "file_name": dateiname}

    async def varianten(self, blueprint_id, anbieter_id):
        return {
            "variants": [
                {"id": 1, "placeholders": [{"position": "front", "width": 1000, "height": 1200}]},
                {"id": 2, "placeholders": [{"position": "front", "width": 1000, "height": 1200}]},
            ]
        }

    async def produkt_anlegen(self, payload, shop_id=None):
        self.gesendet = payload
        return {"id": "prod-999", "images": [{"src": "https://beispiel/mockup1.png"}]}


@pytest.fixture
def motiv(tmp_path):
    from PIL import Image

    pfad = tmp_path / "motiv.png"
    Image.new("RGBA", (1024, 1536), (100, 100, 200, 255)).save(pfad)
    return pfad


def test_produkt_wird_als_entwurf_angelegt(motiv):
    """Propose-only: angelegt ja, veroeffentlicht nein."""
    fake = FakePrintify()
    ergebnis = asyncio.run(
        erstelle_produkt(
            bild_pfad=motiv,
            titel="Retro-Dackel",
            beschreibung="Ein Dackel mit Sonnenbrille",
            produkttyp_key="tshirt",
            client=fake,
        )
    )

    assert ergebnis.printify_id == "prod-999"
    assert ergebnis.varianten == 2
    assert ergebnis.preis_cents > ergebnis.kosten_cents
    assert ergebnis.mockups == ["https://beispiel/mockup1.png"]
    # Nichts im Auftrag deutet auf Veroeffentlichung hin
    assert "publish" not in str(fake.gesendet).lower()


def test_alle_varianten_bekommen_denselben_preis(motiv):
    fake = FakePrintify()
    asyncio.run(
        erstelle_produkt(
            bild_pfad=motiv, titel="X", beschreibung="Y", produkttyp_key="tshirt", client=fake
        )
    )
    preise = {v["price"] for v in fake.gesendet["variants"]}
    assert len(preise) == 1


def test_fehlendes_motiv_meldet_sich_klar(tmp_path):
    with pytest.raises(FileNotFoundError):
        asyncio.run(
            erstelle_produkt(
                bild_pfad=tmp_path / "gibt-es-nicht.png",
                titel="X",
                beschreibung="Y",
                client=FakePrintify(),
            )
        )
