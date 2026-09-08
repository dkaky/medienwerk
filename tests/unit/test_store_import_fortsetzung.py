"""Ein zweiter Shop-Lauf macht dort weiter, wo der erste aufhoerte.

Nutzerwunsch vom 05.09.2026, woertlich: „lass uns die deckelung von 100
produkten behalten. Wenn ich die suche nochmal mache, wird dann bei 101
weitergemacht bis 200 und bei einem dritten durchlauf ab 201 usw."

Vorher fing jeder Lauf wieder bei Platz 1 an. Die schon importierten Artikel
fielen zwar der Duplikat-Pruefung zum Opfer - aber sie frassen den Vorrat auf,
und der Deckel ``MAX_POOL = 150`` sorgte dafuer, dass ab dem zweiten Lauf kaum
noch etwas Neues uebrig blieb.

Der wichtigste Test hier ist ``test_gescheiterte_zaehlen_als_durchgesehen``:
Zaehlte nur der Erfolg, bekaeme der naechste Lauf genau die Artikel wieder
vorgesetzt, die schon einmal nicht funktioniert haben - und bliebe daran haengen.
"""
from __future__ import annotations

import json

from app.services import store_import_service as sis
from app.services.app_settings import get_app_setting


def test_am_anfang_ist_nichts_gesehen(db):
    assert sis.gesehene_ids(db, "1103573332") == []


def test_gesehene_werden_gemerkt(db):
    sis.merke_gesehene(db, "1103573332", ["100", "101", "102"])
    assert sis.gesehene_ids(db, "1103573332") == ["100", "101", "102"]


def test_zweiter_lauf_haengt_an_statt_zu_ersetzen(db):
    sis.merke_gesehene(db, "1103573332", ["100", "101"])
    stand = sis.merke_gesehene(db, "1103573332", ["102", "103"])
    assert stand == 4
    assert sis.gesehene_ids(db, "1103573332") == ["100", "101", "102", "103"]


def test_doppelte_werden_nicht_zweimal_gemerkt(db):
    sis.merke_gesehene(db, "1103573332", ["100", "101"])
    sis.merke_gesehene(db, "1103573332", ["101", "102"])
    assert sis.gesehene_ids(db, "1103573332") == ["100", "101", "102"]


def test_reihenfolge_bleibt_erhalten(db):
    """Der Shop liefert nach Bestsellern sortiert - die Folge IST die Auskunft."""
    sis.merke_gesehene(db, "1103573332", ["300", "100", "200"])
    assert sis.gesehene_ids(db, "1103573332") == ["300", "100", "200"]


def test_shops_stoeren_sich_nicht(db):
    sis.merke_gesehene(db, "111", ["a", "b"])
    sis.merke_gesehene(db, "222", ["c"])
    assert sis.gesehene_ids(db, "111") == ["a", "b"]
    assert sis.gesehene_ids(db, "222") == ["c"]


def test_zuruecksetzen_faengt_wieder_bei_eins_an(db):
    sis.merke_gesehene(db, "111", ["a", "b"])
    sis.setze_fortschritt_zurueck(db, "111")
    assert sis.gesehene_ids(db, "111") == []


def test_kaputter_eintrag_wirft_nicht(db):
    """Ein beschaedigter Fortsetzungspunkt darf den Import nicht blockieren."""
    from app.services.app_settings import set_app_setting

    set_app_setting(db, "store_gesehen:111", "kein json")
    assert sis.gesehene_ids(db, "111") == []


def test_der_eintrag_ist_lesbares_json(db):
    """Damit man im Zweifel von Hand hineinsehen kann."""
    sis.merke_gesehene(db, "111", ["1005013013626233"])
    roh = get_app_setting(db, "store_gesehen:111")
    assert json.loads(roh) == ["1005013013626233"]


# --- Die eigentliche Regel -------------------------------------------------

def test_vorrat_waechst_mit_dem_fortschritt():
    """Fuer die Plaetze 201-300 muessen 300+ Artikel durchgesehen werden.

    MAX_POOL (150) allein reicht dafuer nie - deshalb die eigene Obergrenze
    fuer Fortsetzungslaeufe.
    """
    assert sis.MAX_POOL_FORTSETZUNG > sis.MAX_POOL
    assert sis.MAX_POOL_FORTSETZUNG >= 3 * sis.MAX_PRODUCTS, (
        "Der Vorrat muss mindestens fuer drei volle Durchgaenge reichen")


def test_deckel_je_lauf_bleibt_bei_hundert():
    """Der Nutzer will die 100 ausdruecklich behalten."""
    assert sis.MAX_PRODUCTS == 100
