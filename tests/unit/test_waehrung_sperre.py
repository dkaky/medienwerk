"""Fremde Waehrung darf nie als Euro verbucht werden (Testlauf 27.08.2026).

Beim ersten echten Shop-Import stand ALIEXPRESS_TARGET_CURRENCY auf CNY, waehrend
CNY_TO_EUR_RATE mit 1.0 davon ausging, AliExpress liefere schon Euro. Der Schluessel
fehlte beim Kopieren aus dem Original, wo er auf EUR steht.

Folge: ein T-Shirt fuer 16,30 Euro wurde mit 127,09 Euro Einkauf verbucht, daraus
237,95 Euro Verkaufspreis. Nichts stuerzte ab. Aufgefallen ist es nur, weil die Zahl
bei einem T-Shirt absurd war - bei einem teureren Artikel waere sie durchgegangen.
Genau darum eine harte Sperre statt einer Warnung.
"""
from __future__ import annotations

import pytest

from app.config import Settings


def test_fremde_waehrung_mit_faktor_eins_wird_abgelehnt():
    with pytest.raises(ValueError, match="CNY"):
        Settings(aliexpress_target_currency="CNY", cny_to_eur_rate=1.0)


def test_der_fehler_sagt_was_zu_tun_ist():
    """Eine Sperre, die nur 'ungueltig' sagt, kostet mehr Zeit als sie spart."""
    with pytest.raises(ValueError) as e:
        Settings(aliexpress_target_currency="USD", cny_to_eur_rate=1.0)

    text = str(e.value)
    assert "ALIEXPRESS_TARGET_CURRENCY=EUR" in text, "der empfohlene Weg muss dastehen"
    assert "CNY_TO_EUR_RATE" in text, "der Alternativweg auch"


def test_euro_mit_faktor_eins_ist_richtig():
    """Der Normalfall: AliExpress rechnet um, im Code veraltet kein Kurs."""
    s = Settings(aliexpress_target_currency="EUR", cny_to_eur_rate=1.0)

    assert s.aliexpress_target_currency == "EUR"


def test_fremde_waehrung_mit_echtem_kurs_ist_erlaubt():
    """Wer den Kurs bewusst pflegt, darf das - die Sperre trifft nur das stille Missverhaeltnis."""
    s = Settings(aliexpress_target_currency="CNY", cny_to_eur_rate=0.128)

    assert s.cny_to_eur_rate == 0.128


@pytest.mark.parametrize("geschrieben", ["eur", "Eur", " EUR ", "EUR"])
def test_schreibweise_ist_egal(geschrieben):
    """Ein Tippfehler in der Gross-/Kleinschreibung darf keine Sperre ausloesen."""
    s = Settings(aliexpress_target_currency=geschrieben, cny_to_eur_rate=1.0)

    assert s is not None


def test_leere_waehrung_sperrt_nicht():
    """Ohne Angabe entscheidet der Standardwert - hier gibt es nichts zu widersprechen."""
    s = Settings(aliexpress_target_currency="", cny_to_eur_rate=1.0)

    assert s is not None


def test_der_standardwert_ist_in_sich_stimmig():
    """Die Kombination, mit der das Projekt ausgeliefert wird, muss die Sperre bestehen."""
    s = Settings()

    assert s.aliexpress_target_currency.upper() == "EUR"
    assert s.cny_to_eur_rate == 1.0
