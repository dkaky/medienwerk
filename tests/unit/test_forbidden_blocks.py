"""Kunden-Beschreibung darf NIE Verkäufer-Hinweise enthalten.

Vorfall 09.07.2026: die KI schrieb einen Marken-/Abmahn-Hinweis
("⚠️ WICHTIGER HINWEIS – Designs stehen möglicherweise unter Markenrechten Dritter …")
an den ANFANG der Produktbeschreibung – das las der Kunde. Solche rechtlichen/Marken-/
Urheberrechts-Hinweise gehören ausschließlich in strategic_note (für den Verkäufer).
"""
from __future__ import annotations

from app.integrations.llm import strip_forbidden_blocks


INCIDENT = (
    "⚠️ WICHTIGER HINWEIS\n"
    "Dieses Produkt enthält Designs, die möglicherweise unter Markenrechten von Dritten "
    "stehen. Bitte vor dem Kauf die lokalen Gesetze konsultieren.\n\n"
    "✨ Edle Halskette\n"
    "Hochwertige Edelstahl-Kette für jeden Anlass.\n\n"
    "§ 19 UStG – Kleinunternehmer, keine USt."
)


def test_strips_trademark_warning_keeps_product_text():
    out = strip_forbidden_blocks(INCIDENT, [])
    # Der Verkäufer-Hinweis ist komplett raus …
    for bad in ("WICHTIGER HINWEIS", "Markenrecht", "lokalen Gesetze", "unter Markenrechten"):
        assert bad not in out
    # … aber der echte Produkttext + § 19-Footer bleiben erhalten.
    assert "Edle Halskette" in out
    assert "Edelstahl-Kette" in out
    assert "§ 19" in out


def test_strips_legal_variants():
    for bad in [
        "Steht unter Markenrechten Dritter.",
        "Urheberrechtlich geschützte Motive – Nutzung auf eigene Gefahr.",
        "Bitte vor dem Kauf die geltenden Gesetze prüfen.",
        "❗️ ACHTUNG",
        "Rechtlicher Hinweis:",
        "Trademark of third parties may apply.",
    ]:
        keep = "Schönes Produkt mit tollen Details."
        out = strip_forbidden_blocks(bad + "\n\n" + keep, [])
        assert keep in out
        assert bad.split()[0] not in out or out.strip() == keep  # der Warnblock ist weg


def test_keeps_legitimate_care_and_guarantee_free_text():
    # „Hinweis" allein (Pflegehinweis) ist erlaubt – nur RECHTS-/Marken-Hinweise raus.
    desc = "🧼 Pflegehinweis\nHandwäsche empfohlen, nicht bleichen.\n\n📦 Lieferumfang\n1x Kette"
    out = strip_forbidden_blocks(desc, [])
    assert "Pflegehinweis" in out and "Handwäsche" in out and "Lieferumfang" in out


def test_keeps_legit_product_fit_note():
    # Realer Nachbarfall (#311): ein PRODUKT-Passhinweis für den Kunden ("nur für flache
    # Autodächer, Antennengröße vor dem Kauf überprüfen") ist KEIN Rechtshinweis – bleibt drin.
    desc = ("⚠️ Wichtige Hinweise\nGeeignet nur für flache Autodächer, nicht für gekrümmte "
            "Dächer. Bitte Antennengröße vor dem Kauf überprüfen.\n\n📦 Lieferumfang\n1x Antenne")
    out = strip_forbidden_blocks(desc, [])
    assert "flache Autodächer" in out and "Antennengröße" in out and "Lieferumfang" in out


def test_warnings_list_records_removal():
    w: list[str] = []
    strip_forbidden_blocks(INCIDENT, w)
    assert w and any("Beschreibung" in m or "entfernt" in m for m in w)
