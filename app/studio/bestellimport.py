"""Verkaeufe von eBay in die eigene Bestellliste holen - nur LESEND.

Anlass (20.09.2026): Ein Verkauf war bei eBay angekommen, im Dashboard aber nirgends
zu sehen. Beim Ausbau des Dropshipping-Betriebs war der Bestellimport mit entfallen;
uebrig blieb eine Liste (``pod_orders``), die niemand mehr fuellte.

Was hier passiert: die Bestellungen der letzten Tage werden bei eBay abgefragt
(``GET /sell/fulfillment/v1/order``) und je Bestellnummer EINMAL angelegt oder
aufgefrischt. An eBay wird nichts geschrieben, nichts bestellt, kein Druckauftrag
ausgeloest (Eiserne Regel 1). Was der Druck kostet, ist nicht bekannt und bleibt leer
(Eiserne Regel 3).
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.studio.models import PodListing, PodOrder, PodProduct

logger = logging.getLogger("app.studio.bestellimport")

KANAL = "ebay"
#: Nicht oefter als alle paar Minuten bei eBay nachfragen, auch wenn die Seite oft geoeffnet wird.
MIN_ABSTAND_SEKUNDEN = 300
_letzter_lauf = 0.0

_SKU = re.compile(r"^MW-(\d+)-([a-z_]+?)(?:-|$)")
_SKU_VOLL = re.compile(r"^MW-(\d+)-([a-z_]+)(?:-([A-Za-z]+))?(?:-([A-Za-z0-9]+))?$")


def _status(bestellung: dict) -> str:
    if (bestellung.get("cancelStatus") or {}).get("cancelState") == "CANCELED":
        return "cancelled"
    if str(bestellung.get("orderPaymentStatus") or "").upper() not in {"PAID", "PARTIALLY_REFUNDED"}:
        return "pending"
    return {"NOT_STARTED": "new", "IN_PROGRESS": "in_progress",
            "FULFILLED": "shipped"}.get(str(bestellung.get("orderFulfillmentStatus") or "").upper(), "new")


def _zeit(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _kaeufer(b: dict) -> dict:
    """Name, Anschrift und E-Mail des Kaeufers aus der eBay-Bestellung - fuer die
    Verkaufsrechnung. eBay liefert das in fulfillmentStartInstructions[0].shippingStep.shipTo."""
    schritte = b.get("fulfillmentStartInstructions") or []
    ship_to = ((schritte[0] if schritte else {}).get("shippingStep") or {}).get("shipTo") or {}
    adresse = ship_to.get("contactAddress") or {}
    return {
        "name": ship_to.get("fullName") or (b.get("buyer") or {}).get("username") or "",
        "strasse": " ".join(filter(None, [adresse.get("addressLine1"), adresse.get("addressLine2")])),
        "plz": adresse.get("postalCode") or "",
        "ort": adresse.get("city") or "",
        "land": adresse.get("countryCode") or "",
        "email": ship_to.get("email") or "",
    }


def _position(pos: dict) -> dict:
    """Eine Bestellposition: was, in welcher Farbe und Groesse, welches Motiv (aus der Artikelnummer)."""
    from app.studio import mockup_plan

    sku = str(pos.get("sku") or "")
    m = _SKU_VOLL.match(sku)
    design_id = int(m.group(1)) if m else None
    produktart = m.group(2) if m else None
    code = m.group(3) if m else None
    farbe = None
    if code:
        farbe = next((f.name for f in mockup_plan.FARBEN if f.hersteller.replace(" ", "") == code), code)
    return {"titel": str(pos.get("title") or ""), "menge": int(pos.get("quantity") or 1), "sku": sku,
            "design_id": design_id, "produktart": produktart, "farbcode": code, "farbe": farbe,
            "groesse": m.group(4) if m else None}


def _angebot(db: Session, positionen: list[dict]) -> int | None:
    """Zu welchem eigenen Angebot gehoert die Bestellung? Erst ueber die eBay-Artikelnummer, dann ueber die SKU."""
    for pos in positionen:
        artikel = str(pos.get("legacyItemId") or "")
        if artikel:
            treffer = db.execute(select(PodListing).where(PodListing.external_id == artikel)).scalars().first()
            if treffer:
                return treffer.id
        m = _SKU.match(str(pos.get("sku") or ""))
        if m:
            produkt = db.execute(select(PodProduct).where(
                PodProduct.design_id == int(m.group(1)), PodProduct.produktart == m.group(2))).scalars().first()
            if produkt:
                treffer = db.execute(select(PodListing).where(PodListing.product_id == produkt.id)).scalars().first()
                if treffer:
                    return treffer.id
    return None


def uebernehme(db: Session, bestellungen: list[dict[str, Any]]) -> dict:
    """Legt neue Bestellungen an und frischt vorhandene auf. Idempotent je eBay-Bestellnummer."""
    neu = aktualisiert = 0
    for b in bestellungen:
        nummer = str(b.get("orderId") or "")
        if not nummer:
            continue
        positionen = b.get("lineItems") or []
        summe = ((b.get("pricingSummary") or {}).get("total") or {}).get("value")
        titel = "; ".join(f"{p.get('quantity', 1)}x {p.get('title', '')}".strip() for p in positionen)[:480]
        zeile = db.execute(select(PodOrder).where(
            PodOrder.channel == KANAL, PodOrder.external_id == nummer)).scalars().first()
        if zeile is None:
            zeile = PodOrder(channel=KANAL, external_id=nummer)
            db.add(zeile)
            neu += 1
        else:
            aktualisiert += 1
        zeile.status = _status(b)
        zeile.sale_total_eur = round(float(summe), 2) if summe not in (None, "") else None
        zeile.ordered_at = _zeit(b.get("creationDate"))
        zeile.listing_id = _angebot(db, positionen)
        zeile.note = titel or None
        zeile.positionen_json = json.dumps([_position(p) for p in positionen], ensure_ascii=False)
        zeile.kaeufer_json = json.dumps(_kaeufer(b), ensure_ascii=False)
    db.commit()
    return {"gelesen": len(bestellungen), "neu": neu, "aktualisiert": aktualisiert}


async def gleiche_ab(db: Session, ebay: Any, *, tage: int = 60, erzwingen: bool = False) -> dict:
    """Bestellungen der letzten ``tage`` Tage bei eBay lesen und uebernehmen."""
    global _letzter_lauf
    if not erzwingen and time.monotonic() - _letzter_lauf < MIN_ABSTAND_SEKUNDEN and _letzter_lauf:
        return {"gelesen": 0, "neu": 0, "aktualisiert": 0, "uebersprungen": True}
    seit = datetime.now(timezone.utc) - timedelta(days=tage)
    bestellungen = await ebay.list_all_orders(since=seit)
    _letzter_lauf = time.monotonic()
    ergebnis = uebernehme(db, bestellungen)
    if ergebnis["neu"]:
        logger.info("Bestellabgleich: %s neue Bestellung(en) von eBay", ergebnis["neu"])
    try:
        from app.services import invoice_service

        ergebnis["rechnungen"] = invoice_service.generate_missing_pod_sale_invoices(db)
    except Exception as exc:  # noqa: BLE001 - Rechnungslauf darf den Bestellabgleich nie reissen
        logger.error("Bestellabgleich: Verkaufsrechnungen fehlgeschlagen: %s", str(exc)[:200])
    return ergebnis
