"""Eigenstaendiger eBay-Client fuer den lokalen MCP-Server (nur httpx).

Bewusst unabhaengig von der Haupt-App, damit die MCP-venv minimal bleibt
(mcp + httpx). Endpunkte/Scopes entsprechen der recherchierten eBay-REST-API
(developer.ebay.com, Stand 2026).

LOGIN: Ein eBay-Konto kann nicht programmatisch "eingeloggt" werden. Du erzeugst
einmalig per OAuth-Consent einen **User-Refresh-Token** (developer.ebay.com ->
Application Keys -> User Tokens) und traegst ihn als EBAY_REFRESH_TOKEN ein. Der
Server tauscht ihn bei Bedarf gegen einen Access-Token. Ohne Token: Mock-Modus.
"""
from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

_SCOPES = " ".join([
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.analytics.readonly",
])
_LOCALE = {"EBAY_DE": "de-DE", "EBAY_AT": "de-AT", "EBAY_US": "en-US", "EBAY_GB": "en-GB"}


@dataclass
class EbayConfig:
    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""
    marketplace_id: str = "EBAY_DE"
    use_sandbox: bool = True
    timeout: float = 30.0

    @property
    def mock(self) -> bool:
        return not (self.client_id and self.client_secret and self.refresh_token)

    @property
    def host(self) -> str:
        return "https://api.sandbox.ebay.com" if self.use_sandbox else "https://api.ebay.com"

    @classmethod
    def from_env(cls) -> "EbayConfig":
        sb = os.getenv("EBAY_USE_SANDBOX", "true").strip().lower() in ("1", "true", "yes", "on")
        return cls(
            client_id=os.getenv("EBAY_CLIENT_ID", "").strip(),
            client_secret=os.getenv("EBAY_CLIENT_SECRET", "").strip(),
            refresh_token=os.getenv("EBAY_REFRESH_TOKEN", "").strip(),
            marketplace_id=os.getenv("EBAY_MARKETPLACE_ID", "EBAY_DE").strip(),
            use_sandbox=sb,
        )


class EbayError(Exception):
    pass


class EbayClient:
    def __init__(self, config: Optional[EbayConfig] = None) -> None:
        self.cfg = config or EbayConfig.from_env()
        self._http: Optional[httpx.Client] = None
        self._token: Optional[str] = None
        self._token_exp: float = 0.0

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=self.cfg.timeout)
        return self._http

    def _basic(self) -> str:
        raw = f"{self.cfg.client_id}:{self.cfg.client_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def _get_token(self) -> str:
        if self._token and time.monotonic() < self._token_exp:
            return self._token
        try:
            r = self._client().post(
                f"{self.cfg.host}/identity/v1/oauth2/token",
                headers={"Authorization": self._basic(),
                         "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "refresh_token",
                      "refresh_token": self.cfg.refresh_token, "scope": _SCOPES},
            )
            r.raise_for_status()
            body = r.json()
        except httpx.HTTPStatusError as exc:
            raise EbayError(f"eBay OAuth {exc.response.status_code}: {exc.response.text[:300]}")
        except httpx.RequestError as exc:
            raise EbayError(f"Netzwerkfehler: {exc}")
        token = body.get("access_token")
        if not token:
            raise EbayError(f"Kein access_token erhalten ({body.get('error') or body})")
        self._token = token
        self._token_exp = time.monotonic() + int(body.get("expires_in") or 7200) - 300
        return token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_token()}", "Accept": "application/json",
                "X-EBAY-C-MARKETPLACE-ID": self.cfg.marketplace_id}

    def _get(self, url: str, params: dict | None = None) -> Any:
        try:
            r = self._client().get(url, params=params, headers=self._headers())
            r.raise_for_status()
            return r.json() if r.content else {}
        except httpx.HTTPStatusError as exc:
            raise EbayError(f"eBay {exc.response.status_code}: {exc.response.text[:300]}")
        except httpx.RequestError as exc:
            raise EbayError(f"Netzwerkfehler: {exc}")

    # ------------------------------------------------------------------ Tools
    def test_connection(self) -> dict:
        if self.cfg.mock:
            return {"mode": "mock", "ok": True,
                    "note": "Kein EBAY_REFRESH_TOKEN gesetzt -> Mock. Fuer Echtbetrieb "
                            "Client-ID/Secret + Refresh-Token setzen.",
                    "sandbox": self.cfg.use_sandbox}
        self._get_token()
        return {"mode": "live", "ok": True, "sandbox": self.cfg.use_sandbox,
                "marketplace": self.cfg.marketplace_id}

    def get_orders(self, days: int = 7, limit: int = 50) -> dict:
        if self.cfg.mock:
            return {"mode": "mock", "total": 1, "orders": [{
                "orderId": "06-12345-67890", "orderFulfillmentStatus": "NOT_STARTED",
                "buyer": {"username": "max_mustermann"},
                "pricingSummary": {"total": {"value": "29.99", "currency": "EUR"}},
            }]}
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return self._get(f"{self.cfg.host}/sell/fulfillment/v1/order",
                         params={"filter": f"creationdate:[{since}..]", "limit": str(limit)})

    def get_order(self, order_id: str) -> dict:
        if self.cfg.mock:
            return {"mode": "mock", "orderId": order_id,
                    "pricingSummary": {"total": {"value": "29.99", "currency": "EUR"}}}
        return self._get(f"{self.cfg.host}/sell/fulfillment/v1/order/{order_id}")

    def get_listing_analytics(self, listing_id: str, days: int = 7) -> dict:
        if self.cfg.mock:
            return {"mode": "mock", "listing_id": listing_id,
                    "impressions": 137, "views": 9, "ctr": 0.065}
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        dr = f"[{start.strftime('%Y%m%d')}..{end.strftime('%Y%m%d')}]"
        return self._get(f"{self.cfg.host}/sell/analytics/v1/traffic_report", params={
            "dimension": "LISTING",
            "filter": f"listing_ids:{{{listing_id}}},date_range:{dr}",
            "metric": "LISTING_IMPRESSION_TOTAL,LISTING_VIEWS_TOTAL,CLICK_THROUGH_RATE",
        })

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None
