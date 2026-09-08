"""DSGVO-Loeschung bei eBay-Marketplace-Account-Deletion-Notifications.

eBay verlangt, dass bei einer Account-Schliessung die personenbezogenen Daten des
betroffenen Nutzers geloescht/anonymisiert werden. Gleichzeitig bestehen steuerliche
Aufbewahrungspflichten (GoBD, § 14b UStG: Rechnungen 10 Jahre). Loesung:

* **Anonymisieren** der reinen Kontakt-/PII-Felder: Name, E-Mail, Lieferadresse
  in ``Sale`` und der verknuepften ``OrderAliexpress``.
* **Behalten** der buchhalterisch relevanten Felder (Betraege, Transaktions-IDs,
  Rechnungen) – diese sind aufbewahrungspflichtig und enthalten keine direkten
  Kontaktdaten mehr, nachdem Name/Adresse entfernt wurden.

Matching ist best-effort: eBay liefert ``username``/``userId`` des Kontos; wir
speichern keinen separaten eBay-Usernamen, daher wird gegen ``buyer_name`` und
``buyer_email`` abgeglichen. Jeder Lauf wird in ``task_logs`` auditiert.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Sale
from app.services.common import task_log

logger = logging.getLogger("app.services.deletion")

# Tombstone-Werte fuer anonymisierte PII-Felder.
_TOMBSTONE = "[geloescht – eBay account deletion]"


def extract_deletion_target(payload: dict[str, Any]) -> dict[str, Optional[str]]:
    """Liest username/userId aus dem eBay-Notification-Payload (best-effort).

    Erwartete Struktur: ``{"notification": {"data": {"username", "userId", ...}}}``.
    Fehlende Felder ergeben ``None`` – der Aufrufer entscheidet ueber das Verhalten.
    """
    data = ((payload or {}).get("notification") or {}).get("data") or {}
    return {
        "username": data.get("username"),
        "user_id": data.get("userId"),
    }


def anonymize_buyer(
    db: Session,
    *,
    username: Optional[str],
    user_id: Optional[str],
) -> dict[str, Any]:
    """Anonymisiert PII aller Sales (+ Orders) des betroffenen eBay-Kaeufers.

    Gibt eine Zusammenfassung zurueck: Anzahl betroffener Sales/Orders und das
    Matching-Kriterium. Ist kein Identifikator vorhanden, wird nichts geaendert.
    """
    identifiers = [v for v in (username, user_id) if v]
    summary: dict[str, Any] = {
        "username": username,
        "user_id": user_id,
        "sales_anonymized": 0,
        "orders_anonymized": 0,
        "matched": bool(identifiers),
    }

    if not identifiers:
        logger.warning("deletion notification ohne username/userId – nichts zu tun")
        return summary

    with task_log(db, task_type="gdpr_delete", reference_id=username or user_id) as tl:
        # Best-effort-Matching: buyer_name oder buyer_email entspricht einem Identifikator.
        conditions = []
        for ident in identifiers:
            conditions.append(Sale.buyer_name == ident)
            conditions.append(Sale.buyer_email == ident)
        sales = db.execute(select(Sale).where(or_(*conditions))).scalars().all()

        for sale in sales:
            _anonymize_sale(sale)
            summary["sales_anonymized"] += 1
            if sale.order is not None:
                _anonymize_order(sale.order)
                summary["orders_anonymized"] += 1

        db.commit()
        tl.result_data = {
            "sales_anonymized": summary["sales_anonymized"],
            "orders_anonymized": summary["orders_anonymized"],
        }

    logger.info(
        "gdpr deletion abgeschlossen",
        extra={
            "sales": summary["sales_anonymized"],
            "orders": summary["orders_anonymized"],
        },
    )
    return summary


def _anonymize_sale(sale: Sale) -> None:
    """Entfernt Kontakt-PII einer Sale; Betraege/IDs bleiben (Aufbewahrungspflicht)."""
    sale.buyer_name = _TOMBSTONE
    sale.buyer_email = None
    sale.delivery_address = None


def _anonymize_order(order: Any) -> None:
    """Entfernt Kontakt-PII einer AliExpress-Order."""
    order.delivery_name = _TOMBSTONE
    order.delivery_address = None
