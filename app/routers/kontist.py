"""Kontist-Bankkonto: Verbinden (OAuth2) + Nur-Lese-Zusammenfassung fuers Dashboard.

Alle Endpunkte liegen hinter dem Dashboard-Login. /connect -> Kontist-Login des
Nutzers -> /callback tauscht den Code (Server-seitig, mit client_secret) und
speichert die Tokens. /summary cached 60s (Kontist-Rate-Limit <100/min).
"""
from __future__ import annotations

import logging
import secrets
import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.integrations import kontist
from app.retry import PersistentError

logger = logging.getLogger("app.routers.kontist")

router = APIRouter(prefix="/api/v1/kontist", tags=["kontist"])

_pending_states: set[str] = set()
_cache: dict = {"at": 0.0, "data": None}


@router.get("/connect")
def connect():
    """Zum Kontist-Login weiterleiten (einmalige Freigabe durch den Nutzer)."""
    if not kontist.is_configured():
        raise HTTPException(status_code=409,
                            detail="KONTIST_CLIENT_ID/SECRET fehlen in der Server-.env")
    state = secrets.token_urlsafe(24)
    _pending_states.add(state)
    while len(_pending_states) > 20:      # Alt-States kappen
        _pending_states.pop()
    return RedirectResponse(kontist.authorize_url(state))


@router.get("/callback")
async def callback(code: str | None = None, state: str | None = None,
                   error: str | None = None):
    """OAuth-Ruecksprung von Kontist: Code gegen Tokens tauschen."""
    if error:
        raise HTTPException(status_code=409, detail=f"Kontist: {error}")
    if not code or not state or state not in _pending_states:
        raise HTTPException(status_code=400,
                            detail="Ungueltiger Ruecksprung (state/code) — bitte "
                                   "/api/v1/kontist/connect erneut oeffnen")
    _pending_states.discard(state)
    try:
        await kontist.exchange_code(code)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502,
                            detail=f"Token-Tausch fehlgeschlagen: {str(exc)[:150]}")
    _cache["at"] = 0.0
    return RedirectResponse("/?kontist=verbunden")


@router.get("/bank")
def bank(db: Session = Depends(get_db)):
    """Bank-Abgleich Kontist <-> AliExpress-Bestellungen (offene Posten, nur lesen)."""
    if not kontist.is_configured():
        return {"configured": False}
    from app.services import bank_sync_service, kontierung_service
    return {"configured": True,
            "ebay": bank_sync_service.ebay_payout_summary(),
            "archiv": bank_sync_service.archive_status(),
            "kontierung": kontierung_service.kontierung_summary(db),
            **bank_sync_service.bank_reconciliation(db)}


@router.post("/aktualisieren")
async def aktualisieren(db: Session = Depends(get_db)):
    """Kontobuchungen sofort nachholen (sonst nur nachts um 3:15).

    Spiegeln, kontieren, Wareneinkaeufe zuordnen, Kontist-Belege holen.
    """
    from app.services import konto_service
    try:
        return await konto_service.aktualisieren(db)
    except PersistentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)[:300])


@router.post("/match-aliexpress")
def match_aliexpress(db: Session = Depends(get_db)):
    """Den AliExpress-Bank-Abgleich sofort laufen lassen (sonst nur nachts).

    Noetig, wenn sich die Einkaufsbetraege geaendert haben: der Abgleich vergleicht
    den Betrag auf 2 Cent genau, und bis zur Beleg-Pruefung standen dort teils
    Schaetzwerte (real: 15,80 statt 15,94 -> Treffer verpasst). Rein lokal,
    wiederholbar, laesst Mehrdeutiges offen.
    """
    from app.services import bank_sync_service

    ergebnis = bank_sync_service.match_aliexpress_orders(db)
    return {**ergebnis, "offen": bank_sync_service.bank_reconciliation(db).get("ali_total", 0)
            - bank_sync_service.bank_reconciliation(db).get("ali_matched", 0)}


@router.post("/match-gleicher-tag")
def match_gleicher_tag(anwenden: bool = False, tage: int | None = None,
                       db: Session = Depends(get_db)):
    """Abbuchung + Bestellung am selben Tag mit demselben Betrag zuordnen.

    Ohne ``anwenden=true`` nur Vorschau. Zugeordnet wird nur bei beidseitiger
    Eindeutigkeit; der Kombinations-Abgleich bleibt unberuehrt.
    """
    from app.services import bank_sync_service

    return bank_sync_service.match_gleicher_tag(db, anwenden=anwenden, tage=tage)


@router.get("/buchungen")
def buchungen(filter: str = "alle", ab: str | None = None,
              limit: int = 500, db: Session = Depends(get_db)):
    """Kontoansicht: jede Bewegung mit Beleg und Kategorie.

    ``filter``: alle | ohne_beleg | zuordnung_offen | ohne_kategorie | erledigt.
    ``ab`` als YYYY-MM-DD; ohne Angabe ab ``KONTO_START`` (1.1.2026).
    """
    from datetime import datetime as _dt, timezone as _tz

    from app.services import konto_service
    grenze = None
    if ab:
        try:
            grenze = _dt.strptime(ab, "%Y-%m-%d").replace(tzinfo=_tz.utc)
        except ValueError:
            raise HTTPException(status_code=400, detail="ab muss YYYY-MM-DD sein")
    return konto_service.uebersicht(db, filter=filter, ab=grenze, limit=limit)


@router.post("/buchungen/{tx_id}/kategorie")
def buchung_kategorie(tx_id: int, wert: str | None = None,
                      db: Session = Depends(get_db)):
    """Kategorie von Hand setzen (leer = wieder entfernen)."""
    from app.services import konto_service
    try:
        return konto_service.setze_kategorie(db, tx_id=tx_id, kategorie=wert or None)
    except PersistentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/buchungen/{tx_id}/kein-beleg")
def buchung_kein_beleg(tx_id: int, grund: str = "", rueckgaengig: bool = False,
                       db: Session = Depends(get_db)):
    """Privatentnahme, Umbuchung, Steuerzahlung: hier ist kein Beleg zu erwarten."""
    from app.services import konto_service
    try:
        return konto_service.kein_beleg_noetig(db, tx_id=tx_id, grund=grund,
                                               rueckgaengig=rueckgaengig)
    except PersistentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/buchungen/{tx_id}/beleg", status_code=201)
async def buchung_beleg(tx_id: int, file: UploadFile = File(...),
                        kategorie: str | None = Form(None),
                        beschreibung: str = Form(""),
                        db: Session = Depends(get_db)):
    """Rechnung direkt an diese Kontobewegung haengen (Temu, Werbung, Abos …)."""
    from app.services import konto_service
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Leere Datei")
    ext = ((file.filename or "").rsplit(".", 1)[-1] or "pdf").lower()
    if ext not in ("png", "jpg", "jpeg", "webp", "pdf"):
        ext = "pdf"
    try:
        return konto_service.beleg_zu_buchung(db, tx_id=tx_id, file_bytes=data, ext=ext,
                                              kategorie=kategorie,
                                              beschreibung=beschreibung)
    except PersistentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/buchungen/belege-von-kontist")
async def belege_von_kontist(seit: str | None = None, anwenden: bool = False,
                             db: Session = Depends(get_db)):
    """In Kontist hinterlegte Belege uebernehmen (Vorschau ohne ``anwenden``).

    ``seit`` als YYYY-MM-DD grenzt ein – bewusst, damit nicht die ganze Historie
    nachgezogen wird.
    """
    from datetime import datetime as _dt, timezone as _tz

    from app.services import konto_service
    ab = None
    if seit:
        try:
            ab = _dt.strptime(seit, "%Y-%m-%d").replace(tzinfo=_tz.utc)
        except ValueError:
            raise HTTPException(status_code=400, detail="seit muss YYYY-MM-DD sein")
    try:
        return await konto_service.belege_von_kontist(db, seit=ab, anwenden=anwenden)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)[:300])


@router.get("/buchungen/{tx_id}/kontist-rohdaten")
async def kontist_rohdaten(tx_id: int, db: Session = Depends(get_db)):
    """DIAGNOSE: was meldet Kontist zu genau dieser Buchung?

    Gebraucht, weil Kontist offenbar zwischen Foto-Anhang (``assets``) und erkannter
    Rechnung (``document*``) unterscheidet — beides muss das Programm finden.
    """
    from app.models import BankTransaction
    tx = db.get(BankTransaction, tx_id)
    if tx is None:
        raise HTTPException(status_code=404, detail="Buchung nicht gefunden")
    knoten_id = str(tx.bank_ref or "").split("kontist:", 1)[-1]
    try:
        alle = await kontist.fetch_transaction_assets()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)[:300])
    treffer = next((n for n in alle if str(n.get("id")) == knoten_id), None)
    return {"buchung": {"id": tx.id, "gegenpartei": tx.counterparty_name,
                        "betrag": float(tx.amount or 0), "bank_ref": tx.bank_ref},
            "kontist": treffer or "(keine Entsprechung gefunden)"}


@router.get("/felder")
async def felder(typ: str = "Transaction"):
    """DIAGNOSE (nur lesen): welche Felder bietet die Kontist-Schnittstelle?

    Gebraucht fuer die Frage, ob in Kontist hinterlegte Belege abrufbar sind.
    Raten ist keine Option — ein unbekanntes Feld wuerde den naechtlichen
    Bank-Sync mit einem GraphQL-Fehler abbrechen.
    """
    try:
        return await kontist.felder_von(typ)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)[:300])


@router.get("/summary")
async def summary():
    """Kontostand + letzte Transaktionen (60s-Cache)."""
    now = time.monotonic()
    if _cache["data"] is not None and now - _cache["at"] < 60:
        return _cache["data"]
    data = await kontist.summary()
    if data.get("connected") and not data.get("error"):
        _cache["data"] = data
        _cache["at"] = now
    return data
