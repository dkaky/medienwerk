"""Liefer-Check fuer Auslands-Bestellungen (Nutzerauftrag 16.08.).

Prueft bei Verkaeufen mit Lieferland != DE, ob Haupt- und Ausweich-Quellen ins
Zielland liefern (``aliexpress.ds.freight.query`` je Land — eine Laender-LISTE
gibt die API nicht her). Ergebnis + Klartext-Warnung landen am Sale
(``delivery_check``) und werden im Orders-Tab angezeigt.

FAIL-OPEN durchgaengig: "unbekannt" erzeugt weder Warnung noch Blockade — nur
eine DEFINITIVE Absage warnt bzw. stoppt den Bestell-Versuch (Preflight).
Regel 12 beachtet: erst ALLE Netz-Abfragen, DANN ein kurzer DB-Write.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Sale
from app.retry import PersistentError

logger = logging.getLogger("app.services.delivery_check_service")

# Deutsche Laendernamen fuer verstaendliche Warnungen (Fallback: der Code selbst).
_LAENDER_DE = {
    "AT": "Österreich", "CH": "Schweiz", "FR": "Frankreich", "NL": "Niederlande",
    "BE": "Belgien", "IT": "Italien", "ES": "Spanien", "PL": "Polen",
    "LU": "Luxemburg", "CZ": "Tschechien", "DK": "Dänemark", "SE": "Schweden",
    "FI": "Finnland", "PT": "Portugal", "IE": "Irland", "GR": "Griechenland",
    "HU": "Ungarn", "SK": "Slowakei", "SI": "Slowenien", "HR": "Kroatien",
    "RO": "Rumänien", "BG": "Bulgarien", "LT": "Litauen", "LV": "Lettland",
    "EE": "Estland", "GB": "Großbritannien", "UK": "Großbritannien",
}


def land_name(code: str | None) -> str:
    c = str(code or "").strip().upper()
    return _LAENDER_DE.get(c, c or "?")


def sale_land(sale: Sale) -> str | None:
    """Zielland des Verkaufs (ISO-Code) oder None."""
    c = str(((sale.delivery_address or {}).get("country")) or "").strip().upper()
    return c or None


def warnung_text(land: str, haupt_ok: bool | None, alt_ok: dict) -> str | None:
    """Klartext-Warnung — NUR bei definitiver Absage der Hauptquelle."""
    if haupt_ok is not False:
        return None
    name = land_name(land)
    liefernde = [sid for sid, ok in (alt_ok or {}).items() if ok is True]
    if liefernde:
        return (f"Händler liefert nicht nach {name} — deine Ausweich-Quelle liefert "
                f"dorthin: beim Bestellen über 💡 auswählen.")
    if alt_ok:
        return (f"Händler liefert nicht nach {name}, und keine deiner Ausweich-Quellen "
                f"liefert definitiv dorthin — Alternative suchen oder mit dem Käufer klären.")
    return (f"Händler liefert nicht nach {name} — über 💡 eine Ausweich-Quelle mit "
            f"Versand dorthin suchen oder mit dem Käufer klären.")


async def _avail(ae, product_id, land, sku_id=None) -> bool | None:
    try:
        return await ae.delivery_availability(product_id=str(product_id), country=land,
                                              sku_id=(str(sku_id) if sku_id else None))
    except Exception as exc:  # noqa: BLE001 – unbekannt, nie als Nein werten
        logger.info("liefer-check unbeantwortbar (%s->%s): %s",
                    product_id, land, str(exc)[:120])
        return None


async def pruefe_sale(db: Session, sale: Sale) -> dict | None:
    """Voll-Check (Haupt- + Ausweich-Quellen) mit Persistenz am Sale.

    None = kein Check noetig (DE-Lieferung, kein Produkt, keine Quelle).
    Erst alle Netz-Abfragen, dann EIN kurzer Commit (Regel 12).
    """
    land = sale_land(sale)
    if not land or land == "DE":
        return None
    listing = sale.listing
    product = getattr(listing, "product", None) if listing else None
    if product is None or not product.aliexpress_id:
        return None

    from app.integrations import get_aliexpress_client
    ae = get_aliexpress_client()
    haupt_ok = await _avail(ae, product.aliexpress_id, land)
    alt_ok: dict = {}
    for slot in (((product.alternatives or {}).get("sources") or [])[1:4]):
        sid = str(slot.get("aliexpress_id") or "").strip()
        if sid:
            alt_ok[sid] = await _avail(ae, sid, land)

    check = {
        "land": land,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "haupt_ok": haupt_ok,
        "alt_ok": alt_ok,
        "warnung": warnung_text(land, haupt_ok, alt_ok),
    }
    sale.delivery_check = check
    db.commit()
    if check["warnung"]:
        logger.info("liefer-check warnung", extra={"sale_id": sale.id, "land": land})
    return check


async def preflight_bestellung(db: Session, sale: Sale, *, product_id,
                               sku_id=None, ist_hauptquelle: bool = True) -> None:
    """Frischer Check der GEWAEHLTEN Quelle direkt vor dem Bestell-Versuch.

    Stoppt NUR bei definitiver Absage (PersistentError mit Selbsthilfe-Weg) —
    unbekannt laesst die Bestellung durch (fail-open; ein echtes Liefer-Problem
    scheitert dann sauber bei AliExpress). force=True beim Aufrufer umgeht den
    Check bewusst ("Trotzdem bestellen").

    ``sku_id``: die konkret zu bestellende Varianten-SKU (Versandoptionen sind
    SKU-abhaengig) — ohne sie prueft der Client die erste SKU des Produkts.
    ``ist_hauptquelle``: nur dann wird ``haupt_ok`` im gespeicherten Voll-Check
    aktualisiert — eine scheiternde AUSWEICH-Quelle darf den Hauptquellen-Befund
    nicht vergiften (Review-Fund 16.08.).
    """
    land = sale_land(sale)
    if not land or land == "DE":
        return
    from app.integrations import get_aliexpress_client
    ok = await _avail(get_aliexpress_client(), product_id, land, sku_id=sku_id)
    if ok is not False:
        return
    # Gespeicherten Voll-Check aktualisieren, damit das Orders-Tab die Warnung zeigt
    # (kurzer Write NACH dem Netz-Call).
    check = dict(sale.delivery_check or {})
    check["land"] = land
    check["checked_at"] = datetime.now(timezone.utc).isoformat()
    if ist_hauptquelle:
        check["haupt_ok"] = False
        check["warnung"] = warnung_text(land, False, check.get("alt_ok") or {})
    else:
        alt_ok = dict(check.get("alt_ok") or {})
        alt_ok[str(product_id)] = False
        check["alt_ok"] = alt_ok
        check["warnung"] = check.get("warnung") or warnung_text(
            land, check.get("haupt_ok"), alt_ok)
    sale.delivery_check = check
    db.commit()
    name = land_name(land)
    raise PersistentError(
        f"Diese Quelle liefert NICHT nach {name} – es wurde nichts bestellt. "
        f"Das kannst du selbst beheben: über 💡 eine Ausweich-Quelle mit Versand "
        f"nach {name} wählen und erneut bestellen. Bist du sicher, dass geliefert "
        f"wird, geht auch bewusst 'Trotzdem bestellen'.")
