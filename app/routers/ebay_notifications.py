"""eBay Marketplace Account Deletion / Closure Notification (Pflicht für Production).

eBay aktiviert ein Production-Keyset erst, wenn ein oeffentlicher HTTPS-Endpunkt die
Challenge-Validierung besteht (oder eine Exemption vorliegt):

* GET  ?challenge_code=...  -> 200 JSON {"challengeResponse": sha256(code+token+url) hex}
* POST (signierte JSON-Notification) -> 200 ack; Signatur best-effort verifiziert.

WICHTIG: Der Endpunkt muss von eBay erreichbar sein (oeffentliches HTTPS, nicht localhost).
Lokal: per Tunnel (cloudflared/ngrok) exponieren oder die App deployen. Die im Hash
verwendete URL muss EXAKT der bei eBay registrierten URL entsprechen
(Settings: EBAY_DELETION_ENDPOINT_URL).
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.database import SessionLocal
from app.integrations import get_ebay_client
from app.integrations.ebay import RealEbayClient
from app.services.deletion_service import anonymize_buyer, extract_deletion_target

logger = logging.getLogger("app.routers.ebay_notifications")
router = APIRouter(prefix="/ebay", tags=["eBay Notifications"])

_PATH = "/marketplace-account-deletion"


@router.get(_PATH)
async def deletion_challenge(challenge_code: str):
    """GET-Challenge: SHA256(challengeCode + verificationToken + endpointURL) als HEX."""
    s = get_settings()
    endpoint_url = s.ebay_deletion_endpoint_url or ""
    response = RealEbayClient.account_deletion_challenge_response(
        challenge_code, s.ebay_ipn_verification_token, endpoint_url
    )
    # eBay erwartet exakt diesen Body + Content-Type application/json + HTTP 200.
    return JSONResponse(content={"challengeResponse": response}, status_code=200)


@router.post(_PATH)
async def deletion_notification(request: Request):
    """POST-Notification: Signatur best-effort pruefen, immer mit 200 quittieren."""
    raw = await request.body()
    sig = request.headers.get("x-ebay-signature", "")
    valid = False
    try:
        valid = await run_in_threadpool(
            get_ebay_client().verify_ipn_signature, raw, sig
        )
    except Exception as exc:  # noqa: BLE001 – Ack darf nicht an der Pruefung scheitern
        logger.warning("deletion notification verify error", extra={"error": str(exc)})
    logger.info("marketplace account deletion notification",
                extra={"signature_valid": valid, "bytes": len(raw)})

    # Betroffene Nutzerdaten anonymisieren (DSGVO). Darf das Ack nicht blockieren –
    # eBay erwartet immer HTTP 200, sonst wird die Notification erneut zugestellt.
    try:
        payload = json.loads(raw or b"{}")
        target = extract_deletion_target(payload)
        await run_in_threadpool(_anonymize, target["username"], target["user_id"])
    except Exception as exc:  # noqa: BLE001 – Ack darf nicht an der Loeschung scheitern
        logger.error("deletion anonymize error", extra={"error": str(exc)})

    return JSONResponse(content={"status": "acknowledged"}, status_code=200)


def _anonymize(username: str | None, user_id: str | None) -> None:
    """Synchroner DB-Lauf (im Threadpool): eigene Session, sauber geschlossen."""
    db = SessionLocal()
    try:
        anonymize_buyer(db, username=username, user_id=user_id)
    finally:
        db.close()
