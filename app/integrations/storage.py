"""Belegablage (Spec Kap. 4.5 / Bereich 3).

MVP-Empfehlung der Spec: lokale/Cloud-Ablage mit Struktur
/invoices/{YYYY-MM}/{type}_{id}.pdf und SHA256-Dedup. Hier: lokales
Dateisystem. gdrive/lexoffice als spaeterer Ausbau (TODO).
"""
from __future__ import annotations

import abc
import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class StoredFile:
    file_path: str
    file_hash: str
    deduped: bool = False  # True, wenn identischer Inhalt bereits existierte


class InvoiceStorage(abc.ABC):
    @abc.abstractmethod
    def store(self, *, content: bytes, period: str, file_type: str, ref_id: str,
              ext: str = "pdf") -> StoredFile:
        """Legt eine Belegdatei ab. period = 'YYYY-MM'."""

    @abc.abstractmethod
    def read(self, file_path: str) -> bytes:
        """Liest eine zuvor abgelegte Belegdatei (fuer Download)."""

    @abc.abstractmethod
    def delete(self, file_path: str) -> bool:
        """Entfernt eine Belegdatei. True = war da und ist jetzt weg."""


class LocalInvoiceStorage(InvoiceStorage):
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)

    def store(self, *, content: bytes, period: str, file_type: str, ref_id: str,
              ext: str = "pdf") -> StoredFile:
        file_hash = hashlib.sha256(content).hexdigest()
        folder = self.base_dir / period
        folder.mkdir(parents=True, exist_ok=True)
        safe_ref = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(ref_id))
        # Der Inhalts-Hash MUSS in den Dateinamen. Ohne ihn ergibt sich der Pfad nur aus
        # Typ + ref_id + Monat – und bei manuellen Ausgaben ist ref_id die KATEGORIE.
        # Zwei Bewirtungsbelege im selben Monat landeten dadurch auf derselben Datei,
        # der zweite hat das Foto des ersten ueberschrieben (Datenverlust, 27.07.).
        # Mit dem Hash ist der Pfad je Inhalt eindeutig – und die Dedup unten greift
        # weiterhin, weil identischer Inhalt wieder denselben Namen ergibt.
        target = folder / f"{file_type}_{safe_ref}_{file_hash[:10]}.{ext}"

        # Dedup: gleicher Hash bei existierender Datei -> nicht erneut schreiben.
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == file_hash:
            return StoredFile(file_path=str(target), file_hash=file_hash, deduped=True)

        target.write_bytes(content)
        return StoredFile(file_path=str(target), file_hash=file_hash, deduped=False)

    def read(self, file_path: str) -> bytes:
        return Path(file_path).read_bytes()

    def delete(self, file_path: str) -> bool:
        """Belegdatei entfernen (fuer das endgueltige Loeschen einer Ausgabe).

        Sicherheitsnetz: es wird ausschliesslich innerhalb der Belegablage geloescht –
        ein Pfad von ausserhalb fliegt raus, statt irgendwo im Dateisystem zu wirken.
        """
        ziel = Path(file_path).resolve()
        basis = self.base_dir.resolve()
        if basis != ziel.parent and basis not in ziel.parents:
            raise ValueError(f"Pfad liegt ausserhalb der Belegablage: {file_path}")
        try:
            ziel.unlink()
        except FileNotFoundError:
            return False
        try:                       # leeren Monatsordner gleich mit aufraeumen
            ziel.parent.rmdir()
        except OSError:
            pass
        return True
