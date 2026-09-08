"""Beleg-Abruf auf Zuruf: Knopf im Dashboard -> Auftrag -> lokaler Helfer.

Der Server kann den Abruf nicht selbst ausloesen (AliExpress gibt den Beleg nur
einem angemeldeten Browser). Der Knopf hinterlegt deshalb einen Auftrag.

Zwei Dinge muessen sitzen:
1. Nur EIN Helfer bekommt den Auftrag – sonst laufen Wajjahat und Kaky
   gleichzeitig durch dieselben Bestellungen.
2. Stirbt der Helfer mitten drin (Rechner zu, Chrome zu), darf der Auftrag nicht
   ewig als „laeuft" haengen bleiben.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services import beleg_abruf


@pytest.fixture(autouse=True)
def eigenes_datenverzeichnis(tmp_path, monkeypatch):
    monkeypatch.setattr(beleg_abruf, "_DATEI", tmp_path / "beleg_abruf.json")


def test_ohne_auftrag_ist_nichts_offen():
    st = beleg_abruf.status()

    assert st["offen"] is False
    assert st["wartet"] is False
    assert st["laeuft"] is False


def test_knopf_hinterlegt_auftrag():
    st = beleg_abruf.anfordern()

    assert st["offen"] is True
    assert st["wartet"] is True          # noch kein Helfer dran
    assert st["laeuft"] is False


def test_nur_ein_helfer_bekommt_den_auftrag():
    beleg_abruf.anfordern()

    erster = beleg_abruf.uebernehmen()
    zweiter = beleg_abruf.uebernehmen()

    assert erster["uebernommen"] is True
    assert zweiter["uebernommen"] is False
    assert "laeuft bereits" in zweiter["grund"]


def test_uebernahme_ohne_auftrag_geht_nicht():
    r = beleg_abruf.uebernehmen()

    assert r["uebernommen"] is False


def test_laufend_ohne_namensnennung():
    """Wer gerade sammelt, wird bewusst NICHT gespeichert (Nutzerwunsch 17.08.)."""
    beleg_abruf.anfordern()
    beleg_abruf.uebernehmen()

    st = beleg_abruf.status()

    assert st["laeuft"] is True
    assert st["wartet"] is False
    assert "uebernommen_von" not in st
    # Auch in der Datei darf nichts Persoenliches liegen
    assert "uebernommen_von" not in beleg_abruf._DATEI.read_text(encoding="utf-8")


def test_zweiter_knopfdruck_startet_nichts_neues():
    """Sonst faellt ein zweiter Sammler mitten in den ersten hinein."""
    beleg_abruf.anfordern()
    beleg_abruf.uebernehmen()

    st = beleg_abruf.anfordern()

    assert st["laeuft"] is True
    assert st.get("hinweis")


def test_ergebnis_landet_im_status():
    beleg_abruf.anfordern()
    beleg_abruf.uebernehmen()

    st = beleg_abruf.fertig(geholt=7, uebersprungen=3, fehler=0)

    assert st["offen"] is False
    assert st["laeuft"] is False
    assert st["ergebnis"] == {"geholt": 7, "uebersprungen": 3, "fehler": 0,
                              "rechnungen": 0}


def test_haengender_helfer_gibt_den_auftrag_wieder_frei(monkeypatch):
    """Rechner zugeklappt -> der Auftrag darf nicht fuer immer blockiert sein."""
    beleg_abruf.anfordern()
    beleg_abruf.uebernehmen()

    spaeter = datetime.now(timezone.utc) + timedelta(minutes=31)
    monkeypatch.setattr(beleg_abruf, "_jetzt", lambda: spaeter)

    assert beleg_abruf.status()["wartet"] is True
    assert beleg_abruf.uebernehmen()["uebernommen"] is True


def test_nach_erledigung_wieder_anforderbar():
    beleg_abruf.anfordern()
    beleg_abruf.uebernehmen()
    beleg_abruf.fertig(geholt=1)

    st = beleg_abruf.anfordern()

    assert st["wartet"] is True
    assert st["ergebnis"] is None      # frischer Auftrag, altes Ergebnis weg


def test_kaputte_datei_blockiert_nicht(tmp_path):
    beleg_abruf._DATEI.parent.mkdir(parents=True, exist_ok=True)
    beleg_abruf._DATEI.write_text("{kein json", encoding="utf-8")

    assert beleg_abruf.status()["offen"] is False
    assert beleg_abruf.anfordern()["wartet"] is True


def test_rechnungszahl_wird_mitgemeldet():
    """Ein Knopfdruck soll sichtbar fertig werden – inklusive der Rechnungen."""
    beleg_abruf.anfordern()
    beleg_abruf.uebernehmen()

    st = beleg_abruf.fertig(geholt=4, uebersprungen=1, rechnungen=4)

    assert st["ergebnis"]["geholt"] == 4
    assert st["ergebnis"]["rechnungen"] == 4


# ---------------------------------------------- 20-Uhr-Warnung im Dashboard
def test_letzter_lauf_ueberdauert_neue_auftraege():
    """Das Dashboard faerbt den Knopf ab 20 Uhr rot, wenn heute noch nichts lief.

    Dafuer darf der Zeitpunkt des letzten Laufs NICHT verlorengehen, sobald jemand
    einen neuen Abruf startet – sonst waere die Warnung nach jedem Klick wieder da.
    """
    beleg_abruf.anfordern()
    beleg_abruf.uebernehmen()
    beleg_abruf.fertig(geholt=3)
    gelaufen = beleg_abruf.status()["zuletzt_gelaufen_am"]
    assert gelaufen

    st = beleg_abruf.anfordern()          # neuer Auftrag

    assert st["zuletzt_gelaufen_am"] == gelaufen


def test_ohne_je_gelaufen_kein_zeitpunkt():
    assert beleg_abruf.status()["zuletzt_gelaufen_am"] is None
    assert beleg_abruf.anfordern()["zuletzt_gelaufen_am"] is None


def test_signal_ist_nur_ein_ja_nein():
    """Der Helfer fragt oeffentlich nach – der Endpunkt darf NICHTS preisgeben.

    Er ist bewusst ohne Anmeldung erreichbar (sonst braeuchte der Helfer eine
    Browser-Sitzung und damit dauerhaft offenes Chrome). Also muss er inhaltslos
    bleiben: kein Zeitpunkt, kein Ergebnis, keine Zahlen.
    """
    from app.routers.invoices import beleg_abruf_signal

    beleg_abruf.anfordern()
    antwort = beleg_abruf_signal()

    assert antwort == {"wartet": True}


def test_signal_ohne_auftrag():
    from app.routers.invoices import beleg_abruf_signal

    assert beleg_abruf_signal() == {"wartet": False}


def test_signal_meldet_laufenden_nicht_als_wartend():
    """Sonst wuerde der zweite Rechner mitten in den laufenden Abruf hineinfahren."""
    from app.routers.invoices import beleg_abruf_signal

    beleg_abruf.anfordern()
    beleg_abruf.uebernehmen()

    assert beleg_abruf_signal() == {"wartet": False}
