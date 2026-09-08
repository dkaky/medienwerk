"""Der Riegel zwischen Studio und Handel.

Das gesamte Handelssystem geht davon aus, dass hinter jedem eBay-Angebot ein
AliExpress-Produkt steht. Ein Studio-Angebot hat keines. Ohne Riegel wuerden
Automatiken es anfassen, als waere es ein Dropshipping-Artikel: Preise anpassen,
Bestand auf null ziehen, im schlimmsten Fall beenden oder ueber den falschen Weg
veroeffentlichen.

Zwei Funktionen genuegen dafuer:

* ``is_studio(listing)`` - fuer Schleifen ueber einzelne Angebote
* ``exclude_studio(stmt)`` - fuer Datenbankabfragen, die viele Angebote holen

Doppelter Riegel: Ist der Schalter ``STUDIO_ENABLED`` aus, liefert ``is_studio``
immer ``False`` und ``exclude_studio`` gibt die Abfrage unveraendert zurueck. Der
Handel laeuft dann nachweislich exakt wie vorher - das ist keine Behauptung,
sondern in ``tests/unit/test_studio_killswitch.py`` festgehalten.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from app.config import get_settings

# Die Menge der Studio-Angebote aendert sich selten, wird aber oft gebraucht.
# Deshalb kurz zwischengespeichert - und bei jeder Aenderung ausdruecklich verworfen.
_CACHE_TTL_S = 60.0
_cache: frozenset[int] | None = None
_cache_zeit = 0.0


def studio_enabled() -> bool:
    """Ist der Studio-Trakt eingeschaltet?"""
    return bool(getattr(get_settings(), "studio_enabled", False))


def invalidate() -> None:
    """Zwischenspeicher verwerfen. Nach JEDER Aenderung an den Verknuepfungen."""
    global _cache, _cache_zeit
    _cache = None
    _cache_zeit = 0.0


def studio_listing_ids(db: Any = None) -> frozenset[int]:
    """Nummern aller Angebote, die zum Studio gehoeren.

    Bei ausgeschaltetem Schalter ohne Datenbankzugriff sofort leer.
    """
    global _cache, _cache_zeit

    if not studio_enabled():
        return frozenset()

    jetzt = time.monotonic()
    if _cache is not None and (jetzt - _cache_zeit) < _CACHE_TTL_S:
        return _cache

    from app.studio.models import StudioListingLink

    eigene_sitzung = db is None
    if eigene_sitzung:
        from app.database import SessionLocal

        db = SessionLocal()
    try:
        ids = frozenset(db.scalars(select(StudioListingLink.listing_id)).all())
    except Exception:  # noqa: BLE001
        # Im Zweifel lieber "kein Studio-Angebot" als ein Absturz mitten im
        # Handelsbetrieb. Der Riegel darf nie die Ursache eines Ausfalls sein.
        return frozenset()
    finally:
        if eigene_sitzung:
            db.close()

    _cache = ids
    _cache_zeit = jetzt
    return ids


def is_studio(listing: Any, db: Any = None) -> bool:
    """Gehoert dieses Angebot zum Studio?

    Vertraegt ``None`` und Objekte ohne ``id`` - der Riegel steht in Schleifen,
    die nicht wegen einer Sonderform stehenbleiben duerfen.
    """
    if listing is None or not studio_enabled():
        return False
    kennung = getattr(listing, "id", None)
    if kennung is None:
        return False
    return kennung in studio_listing_ids(db)


def exclude_studio(stmt: Any, db: Any = None) -> Any:
    """Studio-Angebote aus einer Abfrage herausnehmen.

    Ohne Studio-Angebote (oder bei ausgeschaltetem Schalter) wird die Abfrage
    unveraendert zurueckgegeben - dieselbe SQL wie vorher, Zeichen fuer Zeichen.
    """
    ids = studio_listing_ids(db)
    if not ids:
        return stmt
    from app.models import Listing

    return stmt.where(Listing.id.notin_(ids))


def require_studio_enabled() -> None:
    """FastAPI-Abhaengigkeit: sperrt die Studio-Endpunkte, solange der Schalter aus ist.

    Absichtlich 404 statt 403 - solange der Bereich nicht in Betrieb ist, muss
    von aussen nicht erkennbar sein, dass es ihn gibt.
    """
    if not studio_enabled():
        raise HTTPException(status_code=404, detail="Nicht gefunden")
