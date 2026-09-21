"""Warnung, wenn die SQLite-Datenbank in einem Cloud-Sync-Ordner liegt."""
from pathlib import Path

from app.database import in_synchronisiertem_ordner


def test_onedrive_und_dropbox_werden_erkannt():
    assert in_synchronisiertem_ordner(Path("C:/Users/x/OneDrive/Dokumente/Shop/data/pod_studio.db"))
    assert in_synchronisiertem_ordner(Path("C:/Users/x/OneDrive - Firma/data/pod_studio.db"))
    assert in_synchronisiertem_ordner(Path("C:/Users/x/Dropbox/shop/pod_studio.db"))


def test_lokaler_ordner_ist_in_ordnung():
    assert not in_synchronisiertem_ordner(Path("C:/Users/x/AppData/Local/Medienwerk/pod_studio.db"))
