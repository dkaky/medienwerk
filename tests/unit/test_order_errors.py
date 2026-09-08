"""Tests: AliExpress-Ablehnungscodes werden in verstaendliche Meldungen uebersetzt.

Nutzer-Vorgabe 16.08.: Fehlermeldungen muessen ohne Fachwissen verstaendlich sein
und sagen, was der Nutzer SELBST tun kann (Vorfall Order 1290, P-TRADE-SKU-UNSALEABLE).
"""
from __future__ import annotations

from app.services.order_service import _klartext_ablehnung


def test_sku_unsaleable_wird_uebersetzt_mit_selbsthilfe():
    raw = ("code=P-TRADE-SKU-UNSALEABLE msg=sku can not be sold "
           "pid=1005005994647254 sku_attr='14:100005979#Transparent Black'")
    out = _klartext_ablehnung(raw)
    assert "Variante" in out
    assert "selbst beheben" in out
    assert "Ausweich-Quelle" in out
    # Rohtext bleibt als Diagnose-Anhang erhalten
    assert "P-TRADE-SKU-UNSALEABLE" in out


def test_code_matching_ist_case_insensitiv():
    out = _klartext_ablehnung("fehler: p-trade-sku-unsaleable aufgetreten")
    assert "selbst beheben" in out


def test_unbekannter_code_bleibt_roh():
    raw = "code=SOMETHING-NEW msg=unbekannt"
    assert _klartext_ablehnung(raw) == raw


def test_langer_rohtext_wird_begrenzt():
    out = _klartext_ablehnung("X" * 5000)
    assert len(out) <= 1200


def test_adresse_und_limit_haben_selbsthilfe():
    assert "PLZ" in _klartext_ablehnung("DELIVERY_ADDRESS_VALIDATE_FAIL")
    assert "warten" in _klartext_ablehnung("PLACE_ORDER_LIMIT reached")
