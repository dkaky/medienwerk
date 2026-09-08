"""DHL Shipment Tracking - Unified API (Abfrage/GET): ECHTER Zustellstatus per Sendungsnummer.

Nutzt den STRUKTURIERTEN ``status.statusCode`` (delivered/transit/pre-transit/failure) statt
Freitext -> keine Sprach-/Tempus-Fallen. Read-only. Der API-Key kommt aus der Umgebung
(``DHL_API_KEY``), NIE aus dem Code/Git.

Free-Tier-Limit: 250 Abrufe/Tag, max. 1 Abruf / 5 Sekunden (429 bei Ueberschreitung) -> der
Aufrufer drosselt + budgetiert (siehe order_service.promote_delivered_from_dhl).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("app.integrations.dhl")

_BASE = "https://api-eu.dhl.com/track/shipments"


class DhlRateLimited(Exception):
    """HTTP 429 – Tageslimit oder 1-Abruf/5s-Takt erreicht."""


@dataclass
class DhlStatus:
    tracking_number: str
    found: bool                     # False = DHL kennt die Nummer (noch) nicht / kein DHL-Paket
    delivered: bool
    status_code: Optional[str]      # "delivered" | "transit" | "pre-transit" | "failure" | ...
    status_text: Optional[str]
    delivered_at: Optional[datetime]


def _parse_iso(v) -> Optional[datetime]:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


class DhlTrackingClient:
    """Duenner Client um GET /track/shipments. Ein Client pro Lauf; jede Abfrage 1 HTTP-Call."""

    def __init__(self, api_key: str, *, timeout: float = 15.0):
        self._key = api_key
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self._key)

    @staticmethod
    def _classify_probe(status_code: int) -> dict:
        """HTTP-Status eines Test-Abrufs -> Klartext-Verdikt (ohne den Key preiszugeben).
        200/404 = DHL hat den Key AKZEPTIERT (404 = Testnummer unbekannt, Auth trotzdem ok)."""
        if status_code in (200, 404):
            return {"ok": True, "http": status_code,
                    "reason": "Key gültig – DHL antwortet (Zustell-Erkennung aktiv)."}
        if status_code in (401, 403):
            return {"ok": False, "http": status_code,
                    "reason": "DHL lehnt den Key ab – ungültig, abgelaufen oder das Produkt "
                              "'Shipment Tracking – Unified' ist nicht freigeschaltet."}
        if status_code == 429:
            return {"ok": True, "http": status_code,
                    "reason": "Key ok, aber Tageslimit/Takt (429) gerade erreicht – später erneut."}
        return {"ok": False, "http": status_code,
                "reason": f"Unerwartete DHL-Antwort (HTTP {status_code})."}

    async def probe(self) -> dict:
        """Self-Test: prueft, ob DHL den konfigurierten Key AKZEPTIERT (Auth), ohne ihn preiszugeben.
        Read-only, EIN HTTP-Call mit einer Beispiel-Sendungsnummer."""
        if not self._key:
            return {"ok": False, "reason": "Kein DHL-API-Key konfiguriert."}
        headers = {"DHL-API-Key": self._key, "Accept": "application/json"}
        params = {"trackingNumber": "00340434161094015902", "language": "de"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(_BASE, headers=headers, params=params)
        except Exception as exc:  # noqa: BLE001 – Netz/Timeout -> DHL nicht erreichbar
            return {"ok": False, "reason": f"DHL nicht erreichbar: {str(exc)[:120]}"}
        return self._classify_probe(resp.status_code)

    async def get_status(self, tracking_number: str) -> DhlStatus:
        tn = (tracking_number or "").strip()
        if not tn:
            return DhlStatus(tn, found=False, delivered=False, status_code=None,
                             status_text=None, delivered_at=None)
        headers = {"DHL-API-Key": self._key, "Accept": "application/json"}
        # service NICHT hart setzen -> DHL matcht die Nummer selbst; unbekannte Nummer -> 404.
        params = {"trackingNumber": tn, "language": "de"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(_BASE, headers=headers, params=params)
        if resp.status_code == 404:     # DHL kennt die Nummer nicht (z.B. Hermes / zu neu)
            return DhlStatus(tn, found=False, delivered=False, status_code=None,
                             status_text=None, delivered_at=None)
        if resp.status_code == 429:
            raise DhlRateLimited("DHL 429: Tageslimit/Takt erreicht")
        resp.raise_for_status()
        data = resp.json() or {}
        shipments = data.get("shipments") or []
        if not shipments:
            return DhlStatus(tn, found=False, delivered=False, status_code=None,
                             status_text=None, delivered_at=None)
        st = (shipments[0] or {}).get("status") or {}
        code = str(st.get("statusCode") or "").strip().lower()
        text = st.get("status") or st.get("description")
        delivered = code == "delivered"
        return DhlStatus(tn, found=True, delivered=delivered, status_code=code or None,
                         status_text=text, delivered_at=_parse_iso(st.get("timestamp")) if delivered else None)
