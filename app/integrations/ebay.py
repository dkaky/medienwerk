"""eBay-Integration (Spec Kap. 4.1).

Trading/Inventory/Analytics/Fulfillment-APIs ueber OAuth2. Hier als Interface
plus deterministischer Mock. Der Real-Client skizziert den OAuth2-Flow.
"""
from __future__ import annotations

import abc
import asyncio
import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app.retry import PersistentError, RateLimitError, TransientError

logger = logging.getLogger("app.integrations.ebay")


class EbaySchreibsperre(PersistentError):
    """Ein Schreibzugriff auf eBay wurde im Probebetrieb angehalten."""


class _NurLesenClient:
    """Laesst Lesezugriffe echt durch und haelt jeden Schreibzugriff an.

    Warum hier und nicht an den Aufrufstellen: MOCK_EBAY=true soll heissen "es geht
    nichts an das eBay-Konto raus". Das stimmte nicht. Der Riegel sass an zwei
    Router-Endpunkten, aber golive_service baut ausdruecklich einen echten Client
    ("unabhaengig von MOCK_EBAY", Zeile 3 dort), damit Lesen im Probebetrieb
    weiterlaeuft. An diesem Riegel vorbei fuehrten mindestens sieben Wege:

      - scheduler.py: _publish_retry_job, alle 20 Minuten, holt gescheiterte
        Veroeffentlichungen automatisch nach
      - publish_queue: reiht bei jedem Serverstart haengende Go-Lives neu ein
      - POST /products/{id}/end          beendet echte Angebote
      - POST /products/shipping/free     aendert Versandregeln fuer ALLE Listings
      - POST /products/{id}/promote      schaltet kostenpflichtige Anzeigen
      - naechtlicher Mengenrabatt-Job    entfernt echte Staffeln
      - Titel-Uebernahme in der Optimierung

    Zwei davon laufen ohne jeden Klick. Einzeln abzusichern hiesse, dieselbe
    Pruefung an sieben Stellen zu wiederholen und beim achten neuen Aufrufer wieder
    zu vergessen. Deshalb sitzt sie hier: an der einen Stelle, durch die alles muss.

    GET bleibt frei - Bestellungen, Preise und Angebote sollen im Probebetrieb echt
    zu sehen sein, genau das war der Zweck des echten Clients. Die Anmeldung ist
    ebenfalls frei, obwohl sie POST benutzt: sie holt ein Zugriffsrecht, sie aendert
    nichts am Konto. Ohne diese Ausnahme stuerben auch die Lesezugriffe.
    """

    # POST hierhin ist Anmeldung, kein Eingriff ins Konto.
    _ANMELDUNG = ("/identity/v1/oauth2/token",
                  "/developer/key_management/v1/signing_key")

    def __init__(self, echt: httpx.AsyncClient) -> None:
        self._echt = echt

    def __getattr__(self, name):        # aclose, headers, ... unveraendert durchreichen
        return getattr(self._echt, name)

    def get(self, *a, **kw):
        return self._echt.get(*a, **kw)

    def _halt(self, verb: str, url) -> None:
        if any(a in str(url) for a in self._ANMELDUNG):
            return
        logger.warning("Probebetrieb: %s an eBay angehalten (%s)", verb, str(url)[:120])
        raise EbaySchreibsperre(
            f"Probebetrieb (MOCK_EBAY=true): {verb} an eBay wurde angehalten. "
            f"Es geht nichts an das echte Konto raus. Zum Scharfschalten "
            f"MOCK_EBAY=false setzen. Adresse war: {str(url)[:160]}")

    def post(self, url, *a, **kw):
        self._halt("POST", url)
        return self._echt.post(url, *a, **kw)

    def put(self, url, *a, **kw):
        self._halt("PUT", url)
        return self._echt.put(url, *a, **kw)

    def patch(self, url, *a, **kw):
        self._halt("PATCH", url)
        return self._echt.patch(url, *a, **kw)

    def delete(self, url, *a, **kw):
        self._halt("DELETE", url)
        return self._echt.delete(url, *a, **kw)

    def request(self, method, url, *a, **kw):
        if str(method).upper() != "GET":
            self._halt(str(method).upper(), url)
        return self._echt.request(method, url, *a, **kw)


@dataclass
class EbayAnalytics:
    listing_id: str
    impressions: int
    clicks: int

    @property
    def ctr(self) -> float:
        return round(self.clicks / self.impressions, 4) if self.impressions else 0.0


@dataclass
class EbayOrder:
    ebay_order_id: str
    transaction_id: str
    invoice_amount: float
    currency: str = "EUR"


class EbayClient(abc.ABC):
    @abc.abstractmethod
    async def create_inventory_item(self, sku: str, *, title: str, description: str,
                                    image_urls: list[str], quantity: int,
                                    aspects: Optional[dict] = None) -> None:
        """Inventory API: Inventory-Item anlegen/ersetzen (Basis fuer ein Offer)."""

    @abc.abstractmethod
    async def create_offer(self, sku: str, *, price_eur: float, category_id: str,
                           quantity: int, merchant_location_key: Optional[str] = None,
                           listing_policies: Optional[dict] = None,
                           listing_description: Optional[str] = None) -> str:
        """Inventory API: Offer zur SKU anlegen, gibt offerId zurueck (noch unpubliziert)."""

    @abc.abstractmethod
    async def delete_inventory_item(self, sku: str) -> None:
        """Inventory API: Inventory-Item per SKU loeschen (Rollback verwaister Items)."""

    @abc.abstractmethod
    async def publish_listing(self, draft_id: str, *, title: str, category_id: str) -> str:
        """Draft/Offer live stellen, gibt ebay_item_id/listingId zurueck."""

    @abc.abstractmethod
    async def update_inventory(self, item_id: str, *, title: Optional[str] = None,
                               category_id: Optional[str] = None,
                               price_eur: Optional[float] = None,
                               quantity: Optional[int] = None) -> None:
        """Inventory API: Titel/Kategorie/Preis/Menge aktualisieren."""

    @abc.abstractmethod
    async def get_listing_analytics(self, item_id: str, *, days: int = 7) -> EbayAnalytics:
        """Analytics API: Impressionen/Klicks der letzten `days` Tage."""

    @abc.abstractmethod
    async def get_order(self, ebay_order_id: str) -> EbayOrder:
        """Fulfillment API: Order-/Rechnungsdaten."""

    @abc.abstractmethod
    def verify_ipn_signature(self, raw_body: bytes, signature: str) -> bool:
        """Validiert die Signatur einer eingehenden IPN."""


class MockEbayClient(EbayClient):
    """Deterministische Antworten – fuer Entwicklung/Tests ohne eBay-Account."""

    def __init__(self) -> None:
        self._published: dict[str, str] = {}

    @staticmethod
    def _seed(value: str) -> int:
        return int(hashlib.sha256(value.encode()).hexdigest(), 16)

    async def create_inventory_item(self, sku: str, *, title: str, description: str,
                                    image_urls: list[str], quantity: int,
                                    aspects: Optional[dict] = None,
                                    brand: Optional[str] = None,
                                    mpn: Optional[str] = None,
                                    ean: Optional[list[str]] = None) -> None:
        return None

    async def create_offer(self, sku: str, *, price_eur: float, category_id: str,
                           quantity: int, merchant_location_key: Optional[str] = None,
                           listing_policies: Optional[dict] = None,
                           listing_description: Optional[str] = None) -> str:
        return f"offer_{self._seed(sku) % 1_000_000_000}"

    async def create_shipping_fulfillment(self, ebay_order_id: str, *, tracking_number: str,
                                          line_items: list[dict], carrier_code: Optional[str] = None,
                                          shipped_date: Optional[str] = None) -> str:
        return f"ful_{self._seed(ebay_order_id + tracking_number) % 1_000_000}"

    async def get_shipping_fulfillments(self, ebay_order_id: str) -> list[dict]:
        return []

    async def delete_inventory_item(self, sku: str) -> None:
        return None

    async def get_inventory_item(self, sku: str) -> Optional[dict]:
        return None   # kein Live-Item -> Achsennamen-Abgleich faellt auf den alten Pfad zurueck

    async def get_inventory_item_group(self, group_key: str) -> Optional[dict]:
        return None

    async def publish_listing(self, draft_id: str, *, title: str, category_id: str) -> str:
        item_id = str(10_000_000_000 + self._seed(draft_id) % 1_000_000_000)
        self._published[draft_id] = item_id
        return item_id

    async def update_inventory(self, item_id: str, *, title: Optional[str] = None,
                               category_id: Optional[str] = None,
                               price_eur: Optional[float] = None,
                               quantity: Optional[int] = None) -> None:
        return None

    async def get_item_pictures(self, item_id: str) -> list:
        return []

    async def revise_item_pictures(self, item_id: str, image_urls: list) -> None:
        return None

    async def revise_item_title(self, item_id: str, title: str) -> None:
        return None

    async def search_competitor_titles(self, keywords: str, *, limit: int = 12) -> list[str]:
        base = (keywords or "Produkt").split(" - ")[0][:40]
        return [f"{base} Premium Set XL", f"{base} Profi Neu 2026",
                f"{base} Original Top Qualität"][:limit]

    async def search_competitor_offers(self, keywords: str, *, limit: int = 20) -> list[dict]:
        base = (keywords or "Produkt").split(" - ")[0][:40]
        seed = self._seed(keywords or "x")
        offers = []
        for i in range(min(6, max(limit, 1))):
            offers.append({"title": f"{base} Variante {i+1}",
                           "price_eur": round(15 + (seed + i * 7) % 20 + 0.95, 2),
                           "currency": "EUR", "url": f"https://www.ebay.de/itm/{seed+i}",
                           "item_id": f"v1|{seed+i}|0", "condition": "Neu", "sold": None})
        return offers[:limit]

    async def get_required_aspects(self, category_id: str) -> list[dict]:
        """Feste Merkmalsliste - die Attrappe fragt eBays Taxonomie nicht.

        Nachgebildet wird die Form des echten Clients, nicht sein Inhalt:
        ``[{name, required, variation, values}]``. Zwei Pflichtmerkmale und eines
        ohne Zwang, damit sich im Probebetrieb beide Faelle zeigen - ein Formular,
        das nur Pflichtfelder kennt, faellt sonst erst beim Echtbetrieb auf.

        "Marke" traegt bewusst KEINE Werteliste: eBay laesst dort freien Text zu,
        und ein Formular muss mit beidem umgehen koennen.
        """
        return [
            {"name": "Marke", "required": True, "variation": False, "values": []},
            {"name": "Material", "required": True, "variation": False,
             "values": ["Baumwolle", "Polyester", "Mischgewebe"]},
            {"name": "Farbe", "required": False, "variation": True,
             "values": ["Schwarz", "Weiß", "Blau"]},
        ]

    async def build_aspects(self, category_id: str, base: dict | None = None) -> dict:
        """Pflichtmerkmale auffuellen - gleiche Regel wie beim echten Client."""
        aspects = {k: (v if isinstance(v, list) else [v]) for k, v in (base or {}).items()}
        for a in await self.get_required_aspects(category_id):
            if a["required"] and a["name"] and a["name"] not in aspects:
                aspects[a["name"]] = [a["values"][0] if a["values"] else "Sonstige"]
        return aspects

    async def suggest_categories(self, title: str, *, limit: int = 4) -> list[str]:
        """Feste Vorschlagsliste - die Attrappe fragt eBay nicht.

        Muss vorhanden sein, seit der Entwurfsweg die Kategorie selbst ermittelt,
        statt die ungueltige "0" zu schicken. 15687 ist die eBay-Kategorie fuer
        Herren-T-Shirts und damit ein plausibler Wert fuer Testlaeufe.
        """
        return ["15687", "260012"][:limit]

    async def suggest_category(self, title: str) -> Optional[str]:
        cats = await self.suggest_categories(title, limit=1)
        return cats[0] if cats else None

    async def suggest_categories_mit_namen(self, title: str, *, limit: int = 6) -> list[dict]:
        """Zwei benannte Vorschlaege - reicht, um die Auswahl im Formular zu zeigen."""
        return [
            {"id": "15687", "name": "Herren-T-Shirts",
             "pfad": "Kleidung & Accessoires > Herren > Herrenbekleidung"},
            {"id": "260012", "name": "Poster & Kunstdrucke",
             "pfad": "Kunst > Kunstdrucke"},
        ][:limit]

    async def promote_listings(self, listing_ids, *, bid_pct=None):
        return {"campaign_id": "MOCK-CAMP", "rate_pct": round((bid_pct or 0.10) * 100, 1),
                "created": len(listing_ids), "already": 0, "failed": 0}

    async def update_ad_rate(self, listing_ids, *, bid_pct):
        return {"campaign_id": "MOCK-CAMP", "rate_pct": round(bid_pct * 100, 1),
                "updated": len(listing_ids), "failed": 0}

    async def set_ad_rate(self, listing_ids, *, bid_pct):
        return {"campaign_id": "MOCK-CAMP", "rate_pct": round(bid_pct * 100, 1),
                "created": len(listing_ids), "already": 0, "failed": 0, "updated": 0}

    async def get_ad_rates(self, listing_ids=None) -> dict:
        # Mock hat keine echten Anzeigenraten -> leeres dict (Kalkulation nutzt Pauschale).
        return {}

    async def get_listing_analytics(self, item_id: str, *, days: int = 7) -> EbayAnalytics:
        seed = self._seed(item_id)
        impressions = seed % 200            # 0..199
        # Jedes 4. Listing simuliert 0 Klicks -> Optimierungs-Kandidat.
        clicks = 0 if seed % 4 == 0 else (seed % 13)
        return EbayAnalytics(listing_id=item_id, impressions=impressions, clicks=clicks)

    async def get_all_listing_analytics(self, item_ids=None, *, days: int = 30) -> dict:
        return {}   # Mock: leerer Report -> refresh_click_data laesst Werte unveraendert

    async def get_order(self, ebay_order_id: str) -> EbayOrder:
        amount = 9.99 + (self._seed(ebay_order_id) % 5000) / 100.0
        return EbayOrder(
            ebay_order_id=ebay_order_id,
            transaction_id=f"txn_{ebay_order_id}",
            invoice_amount=round(amount, 2),
        )

    def verify_ipn_signature(self, raw_body: bytes, signature: str) -> bool:
        # Mock akzeptiert alles ausser leerer Signatur.
        return bool(signature)


# OAuth-Scopes (Spec Kap. 4.1 / Recherche). Sell-APIs brauchen einen USER-Token.
_SCOPE_INVENTORY = "https://api.ebay.com/oauth/api_scope/sell.inventory"
_SCOPE_ANALYTICS = "https://api.ebay.com/oauth/api_scope/sell.analytics.readonly"
_SCOPE_FULFILLMENT = "https://api.ebay.com/oauth/api_scope/sell.fulfillment"
_SCOPE_ACCOUNT = "https://api.ebay.com/oauth/api_scope/sell.account"  # Business-Policies + Location
_SCOPE_FINANCES = "https://api.ebay.com/oauth/api_scope/sell.finances"  # echte Gebuehren (Steuer)
_SCOPE_MARKETING = "https://api.ebay.com/oauth/api_scope/sell.marketing"  # Promoted Listings
_SCOPE_APP = "https://api.ebay.com/oauth/api_scope"  # Application-Scope (Notification)
_SELL_SCOPES = " ".join([_SCOPE_INVENTORY, _SCOPE_ANALYTICS, _SCOPE_FULFILLMENT, _SCOPE_ACCOUNT])
# Fuer die NAECHSTE Re-Autorisierung (authurl): alle Scopes inkl. Finances + Marketing.
_ALL_SCOPES = " ".join([_SELL_SCOPES, _SCOPE_FINANCES, _SCOPE_MARKETING])

# Marktplatz -> (Content-Language, Waehrung)
_MARKETPLACE_LOCALE = {
    "EBAY_DE": ("de-DE", "EUR"),
    "EBAY_AT": ("de-AT", "EUR"),
    "EBAY_US": ("en-US", "USD"),
    "EBAY_GB": ("en-GB", "GBP"),
}


class RealEbayClient(EbayClient):
    """Echter eBay-Client gegen die REST Sell-/Notification-APIs (OAuth2).

    Implementiert auf Basis der recherchierten developer.ebay.com-Doku (Stand 2026):

    * OAuth2: User-Access-Token via refresh_token-Grant, im Speicher gecacht und
      ~5 min vor Ablauf erneuert. Sell-APIs funktionieren NICHT mit App-Token.
    * Inventory API ist SKU-/Offer-zentriert (nicht itemId-zentriert): `update_inventory`
      interpretiert den uebergebenen Identifier als **SKU** und loest die offerId per
      getOffers auf. (Datenmodell-Hinweis: dazu sollte der Service `listing.ebay_sku`
      uebergeben; optional eine `ebay_offer_id`-Spalte persistieren, um den Lookup zu sparen.)
    * Analytics liefert KEINE echten Klick-Zahlen — `clicks` wird aus LISTING_VIEWS_TOTAL
      als Naeherung gefuellt (siehe Spec-Review). Impressionen = LISTING_IMPRESSION_TOTAL.
    * Neue Verkaeufe werden per Polling (`list_recent_orders` -> getOrders) erkannt; es gibt
      kein generell verfuegbares Push-Topic. `verify_ipn_signature` implementiert die
      MODERNE Notification-Signatur (ECDSA/SHA1, Public-Key aus getPublicKey), nicht das
      veraltete X-EBAY-SIG.

    Fehler werden auf die Retry-Taxonomie (transient/persistent/rate-limit) gemappt.
    """

    _TOKEN_BUFFER_S = 300  # Token 5 min vor Ablauf erneuern
    _BULK_MAX = 25         # eBay bulkUpdatePriceQuantity: max. 25 SKUs pro Request

    def __init__(self, settings) -> None:
        self.settings = settings
        self._host = (
            "https://api.sandbox.ebay.com" if settings.ebay_use_sandbox else "https://api.ebay.com"
        )
        self._inv = f"{self._host}/sell/inventory/v1"
        self._ful = f"{self._host}/sell/fulfillment/v1"
        self._account = f"{self._host}/sell/account/v1"
        self._analytics = f"{self._host}/sell/analytics/v1/traffic_report"
        self._token_url = f"{self._host}/identity/v1/oauth2/token"

        self._client: Optional[httpx.AsyncClient] = None
        self._user_token: Optional[str] = None
        self._user_token_expiry: float = 0.0  # time.monotonic-Deadline
        self._token_lock = asyncio.Lock()     # serialisiert Token-Refresh
        # Public-Key-Cache fuer Signaturpruefung: kid -> (pem, monotonic-deadline)
        self._pubkey_cache: dict[str, tuple[str, float]] = {}

    # ------------------------------------------------------------------ Helpers
    @property
    def _currency(self) -> str:
        return _MARKETPLACE_LOCALE.get(self.settings.ebay_marketplace_id, ("en-US", "USD"))[1]

    @property
    def _content_language(self) -> str:
        return _MARKETPLACE_LOCALE.get(self.settings.ebay_marketplace_id, ("en-US", "USD"))[0]

    def _basic_auth(self) -> str:
        raw = f"{self.settings.ebay_client_id}:{self.settings.ebay_client_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def _http(self):
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        if self.settings.use_mock("ebay"):
            # Probebetrieb: lesen ja, schreiben nein. Siehe _NurLesenClient.
            #
            # EINZIGE Ausnahme: ein Mensch hat fuer genau dieses Listing bewusst
            # geklickt und der Worker hat die Freigabe gerade verbraucht. Der
            # Zeitplaner sieht dieses Tor nie - es gilt nur in der Aufgabe, die
            # es geoeffnet hat. Siehe app/services/freigabe.py.
            from app.services import freigabe
            if not freigabe.schreiben_erlaubt():
                return _NurLesenClient(self._client)
        return self._client

    def _translate(self, exc: Exception) -> Exception:
        """httpx-Fehler -> Retry-Taxonomie."""
        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            if code == 429:
                retry_after = 5.0
                try:
                    retry_after = float(exc.response.headers.get("retry-after", 5))
                except (TypeError, ValueError):
                    pass
                return RateLimitError(str(exc), retry_after=retry_after)
            if code >= 500:
                return TransientError(f"eBay {code}: {exc.response.text[:300]}")
            return PersistentError(f"eBay {code}: {exc.response.text[:300]}")
        if isinstance(exc, httpx.RequestError):
            return TransientError(str(exc))
        return TransientError(str(exc))

    # ------------------------------------------------------------------ OAuth2
    async def _get_user_token(self) -> str:
        """Gecachter User-Access-Token; erneuert via refresh_token-Grant vor Ablauf.

        Double-checked Locking serialisiert nebenlaeufige Refreshes (geteilte
        Singleton-Instanz via lru_cache) -> kein Thundering-Herd am Token-Endpunkt.
        """
        if self._user_token and time.monotonic() < self._user_token_expiry:
            return self._user_token
        if not self.settings.ebay_refresh_token:
            raise PersistentError("EBAY_REFRESH_TOKEN fehlt (User-Consent erforderlich)")

        async with self._token_lock:
            # erneuter Check im Lock: wartende Coroutine nutzt frischen Token
            if self._user_token and time.monotonic() < self._user_token_expiry:
                return self._user_token
            try:
                resp = await self._http().post(
                    self._token_url,
                    headers={"Authorization": self._basic_auth(),
                             "Content-Type": "application/x-www-form-urlencoded"},
                    data={"grant_type": "refresh_token",
                          "refresh_token": self.settings.ebay_refresh_token,
                          "scope": _SELL_SCOPES},
                )
                resp.raise_for_status()
                body = resp.json()
            except Exception as exc:  # noqa: BLE001
                raise self._translate(exc) from exc

            token = body.get("access_token")
            if not token:
                raise PersistentError(
                    f"eBay OAuth: kein access_token im Response ({body.get('error') or body})"
                )
            self._user_token = token
            self._user_token_expiry = (
                time.monotonic() + int(body.get("expires_in") or 7200) - self._TOKEN_BUFFER_S
            )
            return self._user_token

    async def _get_finances_token(self) -> str:
        """User-Token MIT sell.finances-Scope (eigener Cache; bricht den Hauptpfad nie).

        Schlaegt fehl, solange der Refresh-Token ohne sell.finances erteilt wurde ->
        klare Meldung, dass eine einmalige Re-Autorisierung noetig ist.
        """
        if getattr(self, "_fin_token", None) and time.monotonic() < getattr(self, "_fin_token_exp", 0):
            return self._fin_token
        try:
            resp = await self._http().post(
                self._token_url,
                headers={"Authorization": self._basic_auth(),
                         "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "refresh_token",
                      "refresh_token": self.settings.ebay_refresh_token,
                      "scope": _SELL_SCOPES + " " + _SCOPE_FINANCES},
            )
            resp.raise_for_status()
            body = resp.json()
            token = body.get("access_token")
            if not token:
                raise ValueError(body.get("error_description") or body.get("error") or "kein Token")
        except Exception as exc:  # noqa: BLE001
            raise PersistentError(
                "Finances-API nicht autorisiert – einmalige eBay-Re-Autorisierung mit "
                f"sell.finances-Scope noetig (scripts/ebay_oauth.py authurl). Detail: {exc}"
            ) from exc
        self._fin_token = token
        self._fin_token_exp = time.monotonic() + int(body.get("expires_in") or 7200) - self._TOKEN_BUFFER_S
        return token

    async def _get_marketing_token(self) -> str:
        """User-Token MIT sell.marketing-Scope (Promoted Listings). Eigener Cache."""
        if getattr(self, "_mkt_token", None) and time.monotonic() < getattr(self, "_mkt_token_exp", 0):
            return self._mkt_token
        try:
            resp = await self._http().post(
                self._token_url,
                headers={"Authorization": self._basic_auth(),
                         "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "refresh_token",
                      "refresh_token": self.settings.ebay_refresh_token,
                      "scope": _SELL_SCOPES + " " + _SCOPE_MARKETING},
            )
            resp.raise_for_status()
            body = resp.json()
            token = body.get("access_token")
            if not token:
                raise ValueError(body.get("error_description") or body.get("error") or "kein Token")
        except Exception as exc:  # noqa: BLE001
            raise PersistentError(
                "Anzeigen (Promoted Listings) nicht autorisiert – einmalige eBay-Re-Autorisierung "
                f"mit sell.marketing-Scope noetig (scripts/ebay_oauth.py authurl). Detail: {exc}"
            ) from exc
        self._mkt_token = token
        self._mkt_token_exp = time.monotonic() + int(body.get("expires_in") or 7200) - self._TOKEN_BUFFER_S
        return token

    async def _marketing_headers(self) -> dict:
        return {"Authorization": f"Bearer {await self._get_marketing_token()}",
                "Accept": "application/json", "Content-Type": "application/json",
                "X-EBAY-C-MARKETPLACE-ID": self.settings.ebay_marketplace_id}

    async def create_volume_pricing(self, item_id: str, *, tiers: list, name: str) -> str:
        """eBay Volume Pricing (Mengenrabatt) fuer EIN Listing aktivieren (Marketing API).

        ``tiers``: [(min_qty, discount_pct)], z.B. [(2, 5.0), (3, 10.0)]. Die erste Regel
        (minQuantity 1 -> 0 %) ist Pflicht-Basislinie. Gibt die promotionId zurueck.
        Nur auf manuellen Klick aufrufen (Aussen-/Geld-Aktion, propose-only).
        """
        headers = await self._marketing_headers()
        mkt = f"{self._host}/sell/marketing/v1"
        # eBay-Volume-Pricing verlangt: erste Regel minQuantity 1 mit 0% als Basislinie,
        # percentageOffOrder als GANZZAHLIGEN String (kein "5.0"), und ein endDate.
        rules = [{"ruleOrder": 1, "discountSpecification": {"minQuantity": 1},
                  "discountBenefit": {"percentageOffOrder": "0"}}]
        for i, (q, pct) in enumerate(sorted(tiers, key=lambda t: t[0]), start=2):
            rules.append({"ruleOrder": i, "discountSpecification": {"minQuantity": int(q)},
                          "discountBenefit": {"percentageOffOrder": f"{int(round(float(pct)))}"}})
        now = datetime.now(timezone.utc)
        body = {
            "name": (name or "Mengenrabatt")[:90],
            "marketplaceId": self.settings.ebay_marketplace_id,
            "promotionType": "VOLUME_DISCOUNT",
            "promotionStatus": "SCHEDULED",
            "startDate": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endDate": (now + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "discountRules": rules,
            "inventoryCriterion": {"inventoryCriterionType": "INVENTORY_BY_VALUE",
                                   "listingIds": [str(item_id)]},
        }
        try:
            resp = await self._http().post(f"{mkt}/item_promotion", headers=headers, json=body)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        loc = resp.headers.get("location") or resp.headers.get("Location") or ""
        pid = loc.rstrip("/").rsplit("/", 1)[-1]
        return pid or ((resp.json() or {}).get("promotionId") if resp.content else "")

    async def delete_volume_pricing(self, promotion_id: str) -> None:
        """Mengenrabatt-Promotion wieder entfernen (Zuruecknehmen).

        eBay laesst eine LAUFENDE Promotion weder direkt loeschen (38216) noch auf
        PAUSED/ENDED setzen (38240) noch das Startdatum aendern (38260). Der EINZIGE
        funktionierende Weg (empirisch 13.07.): PUT mit promotionStatus=SCHEDULED
        (einziger akzeptierter Wert), Original-startDate und endDate=jetzt+2min ->
        die Aktion ENDET von selbst -> danach ist DELETE erlaubt. Der Rabatt ist ab
        dem PUT wirtschaftlich tot; das DELETE raeumt nur auf (best effort hier,
        sonst beim naechsten Aufruf).
        """
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        headers = await self._marketing_headers()
        mkt = f"{self._host}/sell/marketing/v1"

        async def _delete() -> bool:
            resp = await self._http().delete(f"{mkt}/item_promotion/{promotion_id}",
                                             headers=headers)
            if resp.status_code < 400:
                return True
            if "38216" in (resp.text or ""):
                return False           # noch nicht beendet -> erst end-daten
            resp.raise_for_status()
            return True

        try:
            if await _delete():
                return
            # Laufend: endDate auf jetzt+2min ziehen (Status muss SCHEDULED heissen).
            resp = await self._http().get(f"{mkt}/item_promotion/{promotion_id}", headers=headers)
            resp.raise_for_status()
            promo = resp.json() or {}
            keep = ("name", "description", "marketplaceId", "promotionType",
                    "discountRules", "inventoryCriterion", "startDate")
            body = {k: v for k, v in promo.items() if k in keep and v is not None}
            body["promotionStatus"] = "SCHEDULED"
            body["endDate"] = (_dt.now(_tz.utc) + _td(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
            resp = await self._http().put(f"{mkt}/item_promotion/{promotion_id}",
                                          headers=headers, json=body)
            resp.raise_for_status()
            # Bis ~3 Minuten auf ENDED warten, dann loeschen (best effort).
            import asyncio as _aio
            for _ in range(7):
                await _aio.sleep(30)
                if await _delete():
                    return
            # Nicht geloescht, aber die Aktion endet in Minuten von selbst -> Ziel erreicht.
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc

    async def ensure_ad_campaign(self, *, name: str = "Druckhelden-Standard") -> str:
        """Promoted-Listings-Standard-Kampagne (Cost-per-Sale) finden oder anlegen -> campaignId."""
        if getattr(self, "_campaign_id", None):
            return self._campaign_id
        headers = await self._marketing_headers()
        mkt = f"{self._host}/sell/marketing/v1"
        try:
            resp = await self._http().get(f"{mkt}/ad_campaign",
                                          params={"limit": "100"}, headers=headers)
            resp.raise_for_status()
            for c in (resp.json() or {}).get("campaigns") or []:
                model = ((c.get("fundingStrategy") or {}).get("fundingModel") or "")
                if model == "COST_PER_SALE" and c.get("campaignStatus") in ("RUNNING", "CREATED"):
                    self._campaign_id = c.get("campaignId")
                    return self._campaign_id
            body = {
                "campaignName": name,
                "marketplaceId": self.settings.ebay_marketplace_id,
                "fundingStrategy": {"fundingModel": "COST_PER_SALE"},
                "startDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            resp = await self._http().post(f"{mkt}/ad_campaign", headers=headers, json=body)
            resp.raise_for_status()
            loc = resp.headers.get("location") or resp.headers.get("Location") or ""
            self._campaign_id = loc.rstrip("/").rsplit("/", 1)[-1]
            return self._campaign_id
        except PersistentError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc

    async def promote_listings(self, listing_ids: list[str], *,
                               bid_pct: Optional[float] = None) -> dict:
        """Anzeigen (Promoted Listings Standard) fuer Listings anlegen.

        bid_pct als Bruch (0.10 = 10%). bulkCreateAdsByListingId; 409 = Ad existiert
        bereits (ok/idempotent).
        """
        rate = (bid_pct if bid_pct is not None else self.settings.ebay_ad_rate_pct) * 100
        campaign = await self.ensure_ad_campaign()
        headers = await self._marketing_headers()
        body = {"requests": [{"listingId": str(lid), "bidPercentage": f"{rate:.1f}"}
                             for lid in listing_ids if lid]}
        try:
            resp = await self._http().post(
                f"{self._host}/sell/marketing/v1/ad_campaign/{campaign}/bulk_create_ads_by_listing_id",
                headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json() or {}
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        created = existed = failed = 0
        for r in data.get("responses") or []:
            code = int(r.get("statusCode") or 0)
            if code in (200, 201):
                created += 1
            elif code == 409:
                existed += 1
            else:
                failed += 1
        return {"campaign_id": campaign, "rate_pct": round(rate, 1),
                "created": created, "already": existed, "failed": failed}

    # ---------------------------------------------- Digitale Signaturen (Finances)
    _SIGNING_KEY_FILE = "./data/ebay_signing_key.json"

    async def _ensure_signing_key(self) -> dict:
        """Ed25519-Signierschluessel (Key Management API) laden oder einmalig erzeugen.

        eBay verlangt fuer die Finances API (EU-Verkaeufer) signierte Requests
        (RFC 9421). Der private Schluessel wird NUR bei der Erzeugung geliefert ->
        persistente Ablage in data/ebay_signing_key.json.
        """
        import json as _json
        from pathlib import Path
        p = Path(self._SIGNING_KEY_FILE)
        if p.exists():
            try:
                data = _json.loads(p.read_text())
                if data.get("private_key") and data.get("jwe"):
                    return data
            except ValueError:
                pass
        token = await self._get_finances_token()
        host = ("https://apiz.sandbox.ebay.com" if self.settings.ebay_use_sandbox
                else "https://apiz.ebay.com")
        try:
            resp = await self._http().post(
                f"{host}/developer/key_management/v1/signing_key",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json={"signingKeyCipher": "ED25519"})
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        data = {"signing_key_id": body.get("signingKeyId"),
                "private_key": body.get("privateKey"),
                "public_key": body.get("publicKey"),
                "jwe": body.get("jwe")}
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_json.dumps(data))
        return data

    @staticmethod
    def _digital_signature_headers(key: dict, *, method: str, path: str,
                                   authority: str) -> dict:
        """RFC-9421-Signatur-Header (Ed25519) fuer einen eBay-Request ohne Body."""
        import base64 as _b64

        from cryptography.hazmat.primitives import serialization
        created = int(time.time())
        components = '("x-ebay-signature-key" "@method" "@path" "@authority")'
        params = f"{components};created={created}"
        base = (f'"x-ebay-signature-key": {key["jwe"]}\n'
                f'"@method": {method}\n'
                f'"@path": {path}\n'
                f'"@authority": {authority}\n'
                f'"@signature-params": {params}')
        der = _b64.b64decode(key["private_key"])
        priv = serialization.load_der_private_key(der, password=None)
        sig = _b64.b64encode(priv.sign(base.encode("utf-8"))).decode()
        return {
            "x-ebay-signature-key": key["jwe"],
            "Signature-Input": f"sig1={params}",
            "Signature": f"sig1=:{sig}:",
            "x-ebay-enforce-signature": "true",
        }

    async def get_finance_transactions(self, *, days: int = 90,
                                       max_pages: int = 10,
                                       date_from: Optional[datetime] = None,
                                       date_to: Optional[datetime] = None) -> list[dict]:
        """Finances API getTransactions: echte Geldbewegungen inkl. GEBUEHREN je Order.

        Host ist apiz.ebay.com (nicht api.). Requests werden digital signiert
        (eBay-Pflicht). `date_from/date_to` erlauben historische Fenster (die API
        reicht weiter zurueck als getOrders' 90 Tage). Liefert die rohen
        transaction-Objekte (SALE|REFUND|..., orderId, amount, totalFeeAmount).
        """
        token = await self._get_finances_token()
        key = await self._ensure_signing_key()
        host = ("https://apiz.sandbox.ebay.com" if self.settings.ebay_use_sandbox
                else "https://apiz.ebay.com")
        authority = host.replace("https://", "")
        start = date_from or (datetime.now(timezone.utc) - timedelta(days=max(1, days)))
        rng = start.strftime("%Y-%m-%dT%H:%M:%S.000Z") + ".."
        if date_to is not None:
            rng += date_to.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        base_headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
                        "X-EBAY-C-MARKETPLACE-ID": self.settings.ebay_marketplace_id}
        path = "/sell/finances/v1/transaction"
        out: list[dict] = []
        offset = 0
        total = 0
        for _ in range(max_pages):
            try:
                headers = dict(base_headers)
                headers.update(self._digital_signature_headers(
                    key, method="GET", path=path, authority=authority))
                resp = await self._http().get(
                    f"{host}{path}",
                    params={"filter": f"transactionDate:[{rng}]",
                            "limit": "200", "offset": str(offset)},
                    headers=headers)
                if resp.status_code == 404:
                    break
                resp.raise_for_status()
                data = resp.json() or {}
            except Exception as exc:  # noqa: BLE001
                raise self._translate(exc) from exc
            txs = data.get("transactions") or []
            out.extend(txs)
            total = int(data.get("total") or 0)
            offset += 200
            if not txs or offset >= total:
                break
        else:
            # max_pages aufgebraucht, obwohl eBay noch mehr hat: ein STILLES
            # Abschneiden wuerde zu niedrige Gebuehren liefern und die faelschlich
            # in fee_eur_actual schreiben. Lieber laut scheitern (Regel 14).
            if total and len(out) < total:
                raise PersistentError(
                    f"Finances-Abruf unvollstaendig: {len(out)} von {total} Transaktionen "
                    f"nach {max_pages} Seiten. max_pages erhoehen – mit Teildaten darf "
                    f"keine Gebuehr geschrieben werden.")
        return out

    async def get_payouts(self, *, days: int = 90, max_pages: int = 5) -> list[dict]:
        """Finances API getPayouts: die tatsaechlichen Auszahlungen aufs Bankkonto.

        Fuer den Bank-Abgleich (Baustein 2, 11.08.): jede Auszahlung muss als
        Gutschrift auf dem Kontist-Konto ankommen. Liefert rohe payout-Objekte
        (payoutId, payoutStatus, amount{value}, payoutDate, transactionCount).
        Gleiche Infrastruktur wie get_finance_transactions (apiz + Signatur).
        """
        token = await self._get_finances_token()
        key = await self._ensure_signing_key()
        host = ("https://apiz.sandbox.ebay.com" if self.settings.ebay_use_sandbox
                else "https://apiz.ebay.com")
        authority = host.replace("https://", "")
        start = datetime.now(timezone.utc) - timedelta(days=max(1, days))
        rng = start.strftime("%Y-%m-%dT%H:%M:%S.000Z") + ".."
        base_headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
                        "X-EBAY-C-MARKETPLACE-ID": self.settings.ebay_marketplace_id}
        path = "/sell/finances/v1/payout"
        out: list[dict] = []
        offset = 0
        for _ in range(max_pages):
            try:
                headers = dict(base_headers)
                headers.update(self._digital_signature_headers(
                    key, method="GET", path=path, authority=authority))
                resp = await self._http().get(
                    f"{host}{path}",
                    params={"filter": f"payoutDate:[{rng}]",
                            "limit": "200", "offset": str(offset)},
                    headers=headers)
                if resp.status_code == 404:
                    break
                resp.raise_for_status()
                data = resp.json() or {}
            except Exception as exc:  # noqa: BLE001
                raise self._translate(exc) from exc
            payouts = data.get("payouts") or []
            out.extend(payouts)
            total = int(data.get("total") or 0)
            offset += 200
            if not payouts or offset >= total:
                break
        return out

    async def _auth_headers(self, *, content: bool = False) -> dict:
        token = await self._get_user_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "X-EBAY-C-MARKETPLACE-ID": self.settings.ebay_marketplace_id,
        }
        if content:
            headers["Content-Type"] = "application/json"
            headers["Content-Language"] = self._content_language
        return headers

    # ------------------------------------------------------------------ Bereich 1
    async def create_inventory_item(self, sku: str, *, title: str, description: str,
                                    image_urls: list[str], quantity: int,
                                    aspects: Optional[dict] = None,
                                    brand: Optional[str] = None,
                                    mpn: Optional[str] = None,
                                    ean: Optional[list[str]] = None) -> None:
        """createOrReplaceInventoryItem: nativer Listing-Aufbau (ersetzt AutoDS-Import).

        GTIN-Ausnahme: Dropshipping-Produkte haben i.d.R. keinen Barcode -> ean wird
        auf ['Nicht zutreffend'] gesetzt (deutscher No-GTIN-Marker auf eBay.de;
        Nutzerwunsch: kein englisches 'Does not apply' in den Artikelmerkmalen),
        sonst lehnt eBay mit 'Feld EAN fehlt' ab.
        Marke/MPN werden – falls bekannt – mitgegeben (Pflicht bei gebrandeten Artikeln).
        """
        client = self._http()
        headers = await self._auth_headers(content=True)
        product = {
            "title": title[:80],
            "description": description,
            "imageUrls": [u for u in (image_urls or []) if u][:12],
            "aspects": aspects or {},
            "ean": ean or ["Nicht zutreffend"],
        }
        if brand:
            product["brand"] = brand
        # MPN nur sinnvoll bei gebrandeten Artikeln; markenlos -> 'Nicht zutreffend'
        product["mpn"] = mpn or "Nicht zutreffend"
        body = {
            "availability": {"shipToLocationAvailability": {"quantity": max(0, int(quantity))}},
            "condition": "NEW",
            "product": product,
        }
        await self._send(client.put, f"{self._inv}/inventory_item/{sku}", headers, body)

    async def create_inventory_item_group(self, group_key: str, *, title: str,
                                          description: str, image_urls: list[str],
                                          variant_skus: list[str],
                                          specifications: list[dict],
                                          image_varies_by: Optional[list[str]] = None,
                                          aspects: Optional[dict] = None) -> None:
        """createOrReplaceInventoryItemGroup: Varianten-SKUs zu EINEM Listing buendeln.

        `specifications` = [{"name": Achse, "values": [alle Werte]}] – muss EXAKT den
        product.aspects der einzelnen Varianten-Items entsprechen (Gross/Klein/Einheit).
        """
        varies_by: dict = {"specifications": specifications}
        if image_varies_by:
            varies_by["aspectsImageVariesBy"] = image_varies_by
        body = {
            "title": title[:80],
            "description": description or title,
            "imageUrls": [u for u in (image_urls or []) if u][:12],
            "variantSKUs": variant_skus,
            "variesBy": varies_by,
        }
        if aspects:
            body["aspects"] = aspects
        headers = await self._auth_headers(content=True)
        await self._send(self._http().put,
                         f"{self._inv}/inventory_item_group/{group_key}", headers, body)

    async def publish_offer_by_inventory_item_group(self, group_key: str) -> str:
        """publishOfferByInventoryItemGroup -> EIN Multivarianten-Listing (listingId)."""
        headers = await self._auth_headers(content=True)
        body = {"inventoryItemGroupKey": group_key,
                "marketplaceId": self.settings.ebay_marketplace_id}
        try:
            resp = await self._http().post(
                f"{self._inv}/offer/publish_by_inventory_item_group",
                headers=headers, json=body)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        return resp.json().get("listingId", "")

    async def _listing_context(self) -> dict:
        """Policies + Merchant-Location einmal aufloesen und cachen (fuer publishOffer)."""
        if getattr(self, "_ctx_cache", None):
            return self._ctx_cache
        policies = await self.get_business_policies()
        location = await self.ensure_merchant_location(
            key=self.settings.ebay_merchant_location_key,
            postal_code=self.settings.ebay_warehouse_postal,
            country=self.settings.ebay_warehouse_country,
            city=getattr(self.settings, "ebay_warehouse_city", None),
            state=getattr(self.settings, "ebay_warehouse_state", None),
            address_line=getattr(self.settings, "ebay_warehouse_address_line", None),
            name=f"{self.settings.seller_name} Lager",
        )
        self._ctx_cache = {"policies": policies, "location": location}
        return self._ctx_cache

    async def create_offer(self, sku: str, *, price_eur: float, category_id: str,
                           quantity: int, merchant_location_key: Optional[str] = None,
                           listing_policies: Optional[dict] = None,
                           listing_description: Optional[str] = None) -> str:
        """createOffer: Offer zur SKU anlegen -> offerId (mit Policies/Location fuer Publish)."""
        client = self._http()
        headers = await self._auth_headers(content=True)
        # Policies/Location aufloesen, falls nicht uebergeben (noetig fuer publishOffer).
        if listing_policies is None or merchant_location_key is None:
            ctx = await self._listing_context()
            listing_policies = listing_policies or ctx["policies"]
            merchant_location_key = merchant_location_key or ctx["location"]
        body = {
            "sku": sku,
            "marketplaceId": self.settings.ebay_marketplace_id,
            "format": "FIXED_PRICE",
            "availableQuantity": max(0, int(quantity)),
            "categoryId": category_id,
            "pricingSummary": {
                "price": {"value": f"{float(price_eur):.2f}", "currency": self._currency}
            },
        }
        if listing_description:
            body["listingDescription"] = listing_description[:500000]
        if merchant_location_key:
            body["merchantLocationKey"] = merchant_location_key
        pol = {k: v for k, v in {
            "paymentPolicyId": (listing_policies or {}).get("payment"),
            "fulfillmentPolicyId": (listing_policies or {}).get("fulfillment"),
            "returnPolicyId": (listing_policies or {}).get("return"),
        }.items() if v}
        if pol:
            body["listingPolicies"] = pol
        try:
            resp = await client.post(f"{self._inv}/offer", headers=headers, json=body)
            resp.raise_for_status()
            return resp.json().get("offerId", "")
        except Exception as exc:  # noqa: BLE001
            # Idempotenz: Ein frueherer Versuch hat das Offer schon angelegt
            # ("Preisangebot-Entitaet existiert bereits") -> vorhandenes Offer
            # holen, mit den aktuellen Werten aktualisieren und dessen ID zurueckgeben.
            if self._is_offer_exists(exc):
                # eBay nennt die existierende ID in der 25002-Antwort gleich mit —
                # robuster als die SKU-Suche (die z. B. bei Marketplace-/Paging-
                # Eigenheiten leer ausgehen kann; Vorfall golive 08/2026).
                offer_id = self._offer_id_from_error(exc)
                if not offer_id:
                    existing = await self._first_offer_for_sku(sku)
                    offer_id = (existing or {}).get("offerId")
                if offer_id:
                    await self._send(client.put, f"{self._inv}/offer/{offer_id}",
                                     headers, self._sanitize_offer(body))
                    return offer_id
            raise self._translate(exc) from exc

    @staticmethod
    def _offer_id_from_error(exc: Exception) -> Optional[str]:
        """offerId aus einer eBay-Fehlerantwort ziehen (errors[].parameters[offerId])."""
        resp = getattr(exc, "response", None)
        if resp is None:
            return None
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001 – kein/kaputtes JSON -> kein Treffer
            return None
        for err in (data.get("errors") or []) if isinstance(data, dict) else []:
            for p in (err.get("parameters") or []):
                if p.get("name") == "offerId" and p.get("value"):
                    return str(p["value"])
        return None

    @staticmethod
    def _is_offer_exists(exc: Exception) -> bool:
        """True, wenn der eBay-Fehler ein bereits existierendes Offer signalisiert."""
        resp = getattr(exc, "response", None)
        txt = (getattr(resp, "text", "") or "").lower() if resp is not None else ""
        return "existiert bereits" in txt or "already exist" in txt

    async def bulk_update_price(self, updates: list[dict]) -> None:
        """bulkUpdatePriceQuantity: Preis/Menge je SKU aendern OHNE Voll-Replace.

        `updates` = [{"sku", "offer_id", "price_eur"?, "quantity"?}]. Loest keine
        komplette Listing-Revalidierung aus (wichtig bei Varianten-Listings).
        """
        reqs = []
        for u in updates:
            offer: dict = {"offerId": u["offer_id"]}
            if u.get("price_eur") is not None:
                offer["price"] = {"value": f"{float(u['price_eur']):.2f}",
                                  "currency": self._currency}
            if u.get("quantity") is not None:
                offer["availableQuantity"] = int(u["quantity"])
            reqs.append({"sku": u["sku"], "offers": [offer]})
        if not reqs:
            return
        headers = await self._auth_headers(content=True)
        # eBay bulkUpdatePriceQuantity erlaubt MAX. 25 SKUs pro Request (errorId 25712).
        # Multivarianten-Listings haben oft mehr (z.B. Poster mit 72 Varianten) -> chunken,
        # sonst schlaegt der komplette Mengen-/Preis-Push (u.a. OOS -> Menge 0) fehl.
        for i in range(0, len(reqs), self._BULK_MAX):
            batch = reqs[i:i + self._BULK_MAX]
            try:
                resp = await self._http().post(
                    f"{self._inv}/bulk_update_price_quantity",
                    headers=headers, json={"requests": batch})
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                raise self._translate(exc) from exc
            # bulkUpdatePriceQuantity liefert HTTP 200 auch, wenn EINZELNE SKUs scheitern
            # (responses[].statusCode 4xx + errors). Die MUESSEN gemeldet werden – sonst gilt ein
            # abgelehnter Preis faelschlich als gesetzt und wird als Override persistiert (Scheinmarge).
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001 – kein JSON -> als erfolgreich behandeln (HTTP 2xx)
                body = {}
            bad = [r for r in (body.get("responses") or [])
                   if isinstance(r, dict) and int(r.get("statusCode") or 200) >= 300]
            if bad:
                msg = "; ".join(
                    f"{r.get('sku')}: " + "/".join(str(e.get("message") or e.get("errorId"))
                                                    for e in (r.get("errors") or []))[:120]
                    for r in bad)[:300]
                raise PersistentError(f"bulkUpdatePriceQuantity je SKU abgelehnt: {msg}")

    async def withdraw_offer(self, offer_id: str) -> None:
        """withdrawOffer: beendet das aktive Listing zu diesem Offer (Offer bleibt unpublished)."""
        headers = await self._auth_headers(content=True)
        try:
            resp = await self._http().post(
                f"{self._inv}/offer/{offer_id}/withdraw", headers=headers)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc

    async def withdraw_offer_by_group(self, group_key: str) -> None:
        """withdrawOfferByInventoryItemGroup: Multivarianten-Listing komplett beenden."""
        headers = await self._auth_headers(content=True)
        body = {"inventoryItemGroupKey": group_key,
                "marketplaceId": self.settings.ebay_marketplace_id}
        try:
            resp = await self._http().post(
                f"{self._inv}/offer/withdraw_by_inventory_item_group",
                headers=headers, json=body)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc

    async def upload_image(self, pfad) -> str:
        """Ein Bild zu eBay hochladen (Media API createImageFromFile) -> EPS-Bildadresse.

        eBay nimmt in ``product.imageUrls`` nur Adressen, keine Dateien. Die eigenen
        Motive liegen aber auf der Platte - ohne diesen Schritt gibt es kein Angebot
        mit Bild.

        Laeuft durch ``_http()`` und damit durch die Schreibsperre: Im Probebetrieb
        geht auch kein Bild raus. Die Sperre wird bewusst NICHT in einen
        TransientError umgedeutet - sonst hielte ein Aufrufer sie fuer einen
        Netzaussetzer und versuchte es immer wieder.
        """
        from pathlib import Path

        datei = Path(pfad)
        host = ("https://apim.sandbox.ebay.com" if self.settings.ebay_use_sandbox
                else "https://apim.ebay.com")
        url = f"{host}/commerce/media/v1_beta/image/create_image_from_file"
        typ = {".png": "image/png", ".jpg": "image/jpeg",
               ".jpeg": "image/jpeg"}.get(datei.suffix.lower(), "application/octet-stream")
        headers = await self._auth_headers()   # KEIN json-Content-Type: httpx setzt multipart
        try:
            resp = await self._http().post(
                url, headers=headers, files={"image": (datei.name, datei.read_bytes(), typ)})
            resp.raise_for_status()
            daten = resp.json() if resp.content else {}
            adresse = (daten or {}).get("imageUrl")
            # Laut Doku steht die Adresse im Rueckgabetext; faellt der leer aus,
            # verweist der Location-Kopf auf das angelegte Bild.
            if not adresse and resp.headers.get("location"):
                abruf = await self._http().get(resp.headers["location"], headers=headers)
                abruf.raise_for_status()
                adresse = (abruf.json() or {}).get("imageUrl")
        except httpx.HTTPError as exc:
            raise self._translate(exc) from exc
        if not adresse:
            raise PersistentError(f"eBay hat fuer {datei.name} keine Bildadresse geliefert.")
        return adresse

    async def delete_inventory_item(self, sku: str) -> None:
        """deleteInventoryItem: verwaistes Inventory-Item entfernen (Rollback)."""
        try:
            resp = await self._http().delete(
                f"{self._inv}/inventory_item/{sku}", headers=await self._auth_headers()
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc

    async def delete_inventory_item_group(self, group_key: str) -> None:
        """deleteInventoryItemGroup: die Variantengruppe nach dem Beenden entfernen."""
        try:
            resp = await self._http().delete(
                f"{self._inv}/inventory_item_group/{group_key}", headers=await self._auth_headers())
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc

    async def erstes_angebot_zu_sku(self, sku: str) -> Optional[dict]:
        return await self._first_offer_for_sku(sku)

    async def publish_listing(self, draft_id: str, *, title: str, category_id: str) -> str:
        """Offer publizieren -> oeffentliche listingId. `draft_id` = eBay offerId.

        (Hinweis: AutoDS erzeugt das Offer; dessen offerId muss als draft_id ankommen.
        Bei AutoDS-verwalteten Drafts erfolgt das Publish ggf. ueber AutoDS, nicht hier.)
        """
        headers = await self._auth_headers(content=True)
        url = f"{self._inv}/offer/{draft_id}/publish"
        try:
            resp = await self._http().post(url, headers=headers)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        return resp.json().get("listingId", "")

    async def update_inventory(self, item_id: str, *, title: Optional[str] = None,
                               category_id: Optional[str] = None,
                               price_eur: Optional[float] = None,
                               quantity: Optional[int] = None) -> None:
        """Aktives Listing aktualisieren. `item_id` wird als SKU interpretiert.

        Titel -> createOrReplaceInventoryItem (read-merge-write);
        Kategorie/Preis/Menge -> updateOffer (read-merge-write) auf dem ersten Offer der SKU.
        """
        sku = str(item_id)
        client = self._http()
        headers = await self._auth_headers(content=True)

        if title is not None:
            item = await self._get_json(f"{self._inv}/inventory_item/{sku}") or {}
            item.setdefault("product", {})["title"] = title[:80]
            await self._send(client.put, f"{self._inv}/inventory_item/{sku}", headers, item)

        if category_id is not None or price_eur is not None or quantity is not None:
            offer = await self._first_offer_for_sku(sku)
            if offer is None:
                raise PersistentError(f"Kein Offer fuer SKU {sku} gefunden")
            offer_id = offer["offerId"]
            patch = self._sanitize_offer(offer)
            if category_id is not None:
                patch["categoryId"] = category_id
            if quantity is not None:
                patch["availableQuantity"] = quantity
            if price_eur is not None:
                patch.setdefault("pricingSummary", {})["price"] = {
                    "value": f"{float(price_eur):.2f}", "currency": self._currency,
                }
            await self._send(client.put, f"{self._inv}/offer/{offer_id}", headers, patch)

    async def _first_offer_for_sku(self, sku: str) -> Optional[dict]:
        """getOffers?sku=... -> erstes Offer (enthaelt offerId)."""
        data = await self._get_json(
            f"{self._inv}/offer",
            params={"sku": sku, "marketplace_id": self.settings.ebay_marketplace_id},
        )
        offers = (data or {}).get("offers") or []
        return offers[0] if offers else None

    @staticmethod
    def _sanitize_offer(offer: dict) -> dict:
        """updateOffer ist voll-ersetzend; read-only/Pfad-Felder vor dem PUT entfernen."""
        drop = {"offerId", "listing", "status", "sku", "marketplaceId", "format"}
        patch = {k: v for k, v in offer.items() if k not in drop}
        # marketplaceId/format sind im Body erlaubt, muessen aber gueltig sein -> wieder setzen
        patch["marketplaceId"] = offer.get("marketplaceId")
        patch["format"] = offer.get("format", "FIXED_PRICE")
        return patch

    async def update_offer(self, offer_id: str, body: dict) -> None:
        """updateOffer (PUT, voll-ersetzend) — Body vorher per _sanitize_offer bereinigen."""
        client = self._http()
        headers = await self._auth_headers(content=True)
        await self._send(client.put, f"{self._inv}/offer/{offer_id}", headers, body)

    # ------------------------------------------------------------------ Bereich 4
    async def get_listing_analytics(self, item_id: str, *, days: int = 7) -> EbayAnalytics:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        date_range = f"[{start.strftime('%Y%m%d')}..{end.strftime('%Y%m%d')}]"
        params = {
            "dimension": "LISTING",
            "filter": f"listing_ids:{{{item_id}}},date_range:{date_range}",
            "metric": "LISTING_IMPRESSION_TOTAL,LISTING_VIEWS_TOTAL,CLICK_THROUGH_RATE",
        }
        data = await self._get_json(self._analytics, params=params,
                                    headers=await self._auth_headers())
        return self._parse_traffic_report(data or {}, str(item_id))

    @staticmethod
    def _parse_traffic_report(payload: dict, listing_id: str) -> EbayAnalytics:
        """Erste record-Zeile in EbayAnalytics ueberfuehren.

        clicks = LISTING_VIEWS_TOTAL (Naeherung; echte Klickzahlen liefert eBay nicht).
        """
        metrics = [m.get("key") for m in payload.get("header", {}).get("metrics", [])]
        records = payload.get("records") or []
        impressions = views = 0
        if records:
            values = records[0].get("metricValues", [])
            by_key = {metrics[i]: values[i].get("value") for i in range(min(len(metrics), len(values)))}
            impressions = int(float(by_key.get("LISTING_IMPRESSION_TOTAL") or 0))
            views = int(float(by_key.get("LISTING_VIEWS_TOTAL") or 0))
        return EbayAnalytics(listing_id=listing_id, impressions=impressions, clicks=views)

    async def get_all_listing_analytics(self, item_ids: list[str] | None = None,
                                        *, days: int = 30) -> dict:
        """Traffic-Kennzahlen fuer die uebergebenen Listings – EXPLIZIT per
        ``listing_ids`` (200 je Call, s. get_traffic_batch). Rueckgabe:
        {item_id: EbayAnalytics}. Ohne item_ids leer.

        Frueher lief das ueber EINEN ungefilterten Report – das hatte ZWEI Bugs,
        die die Klick-/Impressions-Kennzahlen ruinierten (Optimierungs-Tab):
          * eBay deckelt den ungefilterten Report hart auf 200 Zeilen und ignoriert
            ``offset``. Bei 396 aktiven Listings fielen ~196 heraus und wurden in
            refresh_click_data faelschlich auf 0 gesetzt (auch heute verkaufte).
          * end=heute -> je nach eBay-Zeitzone (Pacific) HTTP 400 „Datum darf nicht
            in der Zukunft liegen" -> der ganze Refresh scheiterte (report_size=0).
        get_traffic_batch fragt gezielt per ID ab (kein 200-Limit) und endet
        gestern (end=now-1d) – beides zusammen behebt beide Fehler.
        """
        if not item_ids:
            return {}
        raw = await self.get_traffic_batch([str(i) for i in item_ids], days=days)
        return {lid: EbayAnalytics(listing_id=lid, impressions=v["impressions"],
                                   clicks=v["views"]) for lid, v in raw.items()}

    # ------------------------------------------------------------------ Bereich 2/3
    async def get_order(self, ebay_order_id: str) -> EbayOrder:
        data = await self._get_json(f"{self._ful}/order/{ebay_order_id}",
                                    headers=await self._auth_headers())
        return self._parse_order(data or {})

    async def get_order_cancel_state(self, ebay_order_id: str) -> str | None:
        """Live-Stornostatus einer Order (cancelStatus.cancelState).

        NONE_REQUESTED | IN_PROGRESS | CANCELED – Geld-Guard direkt vor jedem
        AliExpress-Einkauf (nie fuer stornierte Bestellungen einkaufen).
        """
        data = await self._get_json(f"{self._ful}/order/{ebay_order_id}",
                                    headers=await self._auth_headers())
        cs = (data or {}).get("cancelStatus") or {}
        return cs.get("cancelState") or cs.get("state")

    @staticmethod
    def _parse_order(payload: dict) -> EbayOrder:
        total = (payload.get("pricingSummary") or {}).get("total") or {}
        tx = payload.get("salesRecordReference") or payload.get("legacyOrderId") or payload.get("orderId", "")
        return EbayOrder(
            ebay_order_id=payload.get("orderId", ""),
            transaction_id=str(tx),
            invoice_amount=float(total.get("value") or 0.0),
            currency=total.get("currency", "EUR"),
        )

    async def list_recent_orders(self, *, since: datetime, limit: int = 50) -> list[dict]:
        """Bonus/realer Weg: neue Verkaeufe per Polling von getOrders erkennen.

        Es gibt kein generell verfuegbares Push-Topic fuer neue Bestellungen (Spec-Review).
        `since` als untere creationdate-Grenze (max. 90 Tage zurueck unterstuetzt).
        """
        iso = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        params = {"filter": f"creationdate:[{iso}..]", "limit": str(limit), "offset": "0"}
        data = await self._get_json(f"{self._ful}/order", params=params,
                                    headers=await self._auth_headers())
        return (data or {}).get("orders") or []

    async def list_all_orders(self, *, since: datetime, max_orders: int = 500) -> list[dict]:
        """Alle Bestellungen seit `since` per getOrders-Pagination (bis max_orders)."""
        iso = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        out: list[dict] = []
        offset = 0
        page_size = 100
        while len(out) < max_orders:
            data = await self._get_json(
                f"{self._ful}/order",
                params={"filter": f"creationdate:[{iso}..]", "limit": str(page_size),
                        "offset": str(offset)},
                headers=await self._auth_headers(),
            )
            orders = (data or {}).get("orders") or []
            out.extend(orders)
            total = (data or {}).get("total") or 0
            offset += page_size
            if not orders or offset >= total:
                break
        return out[:max_orders]

    async def create_shipping_fulfillment(self, ebay_order_id: str, *,
                                          tracking_number: str,
                                          line_items: list[dict],
                                          carrier_code: Optional[str] = None,
                                          shipped_date: Optional[str] = None) -> str:
        """createShippingFulfillment: Sendungsnummer an eBay melden -> Order gilt als versandt.

        POST /sell/fulfillment/v1/order/{orderId}/shipping_fulfillment (Erfolg 201,
        fulfillmentId im Location-Header). `line_items` = [{"lineItemId","quantity"}].
        """
        headers = await self._auth_headers(content=True)
        body: dict = {
            "lineItems": [{"lineItemId": li["lineItemId"],
                           "quantity": int(li.get("quantity") or 1)} for li in line_items],
            "shippingCarrierCode": carrier_code or self.settings.ebay_shipping_carrier_code,
            "trackingNumber": tracking_number,
        }
        if shipped_date:
            body["shippedDate"] = shipped_date
        try:
            resp = await self._http().post(
                f"{self._ful}/order/{ebay_order_id}/shipping_fulfillment",
                headers=headers, json=body)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        # fulfillmentId steht im Location-Header (…/shipping_fulfillment/{id})
        loc = resp.headers.get("location") or resp.headers.get("Location") or ""
        return loc.rstrip("/").rsplit("/", 1)[-1] if loc else ""

    async def get_shipping_fulfillments(self, ebay_order_id: str) -> list[dict]:
        """Die bei eBay hinterlegten Sendungen einer Order LESEN – z.B. wenn der Nutzer selbst
        verschickt und die Sendungsnummer direkt auf eBay eingetragen hat. Damit laesst sich
        die Nummer ins eigene System uebernehmen. GET /order/{id}/shipping_fulfillment.
        Rueckgabe: [{tracking_number, carrier, shipped_date}]."""
        data = await self._get_json(f"{self._ful}/order/{ebay_order_id}/shipping_fulfillment")
        out = []
        for f in ((data or {}).get("fulfillments") or []):
            tn = f.get("shipmentTrackingNumber")
            if tn:
                out.append({"tracking_number": str(tn),
                            "carrier": f.get("shippingCarrierCode"),
                            "shipped_date": f.get("shippedDate")})
        return out

    # ------------------------------------------------------------------ Trading API (aktive Listings)
    _SITE_ID = {"EBAY_DE": "77", "EBAY_AT": "16", "EBAY_US": "0", "EBAY_GB": "3"}

    async def _trading_call(self, call_name: str, body_xml: str) -> str:
        """Trading-API-Call mit striktem Ack-Check (Failure UND PartialFailure = Fehler).

        PartialFailure hiess frueher stiller Datenverlust: eBay lehnt die Aenderung ab,
        der alte Code sah nur '<Ack>Failure</Ack>' und meldete Erfolg.
        """
        token = await self._get_user_token()
        site = self._SITE_ID.get(self.settings.ebay_marketplace_id, "77")
        headers = {
            "X-EBAY-API-CALL-NAME": call_name,
            "X-EBAY-API-SITEID": site,
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1155",
            "X-EBAY-API-IAF-TOKEN": token,
            "Content-Type": "text/xml",
        }
        try:
            resp = await self._http().post(f"{self._host}/ws/api.dll",
                                           headers=headers, content=body_xml.encode("utf-8"))
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        text = resp.text
        import re as _re
        m_ack = _re.search(r"<Ack>(.*?)</Ack>", text)
        ack = m_ack.group(1) if m_ack else ""
        if ack.lower() not in ("success", "warning"):
            m = _re.search(r"<LongMessage>(.*?)</LongMessage>", text, _re.S)
            raise PersistentError(f"{call_name} ({ack or 'kein Ack'}): "
                                  f"{(m.group(1) if m else text)[:250]}")
        return text

    async def get_item_price_info(self, item_id: str) -> dict:
        """GetItem: aktueller Preis + Variationen (SKU/Preis) eines Live-Listings.

        Grundlage fuer (a) Varianten-faehiges Preis-Revise und (b) die READ-BACK-
        Verifikation nach jedem Preis-Push (stimmt der Preis WIRKLICH auf eBay?).
        """
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f'<ItemID>{item_id}</ItemID><IncludeItemSpecifics>false</IncludeItemSpecifics>'
            '</GetItemRequest>'
        )
        text = await self._trading_call("GetItem", body)
        import re as _re
        import xml.etree.ElementTree as ET
        ns = {"e": "urn:ebay:apis:eBLBaseComponents"}
        root = ET.fromstring(text)
        item = root.find("e:Item", ns)
        cur = item.findtext("e:SellingStatus/e:CurrentPrice", namespaces=ns) if item is not None else None
        variations = []
        gallery, main_img, pic_axis, pic_by_value = [], None, None, {}
        if item is not None:
            gallery = [u.text for u in item.findall("e:PictureDetails/e:PictureURL", ns) if u.text]
            main_img = gallery[0] if gallery else None
            # ECHTE Varianten-Bilder: Variations/Pictures ist nach EINER Bild-Achse gruppiert
            # (VariationSpecificName); je Achsen-Wert eine Bild-URL. Zuordnung Variation->Bild ueber
            # den Wert dieser Achse. So zeigt der Preis-Dialog die Bilder DES eBay-Listings.
            pics = item.find("e:Variations/e:Pictures", ns)
            if pics is not None:
                pic_axis = pics.findtext("e:VariationSpecificName", namespaces=ns)
                for ps in pics.findall("e:VariationSpecificPictureSet", ns):
                    val = ps.findtext("e:VariationSpecificValue", namespaces=ns)
                    url = ps.findtext("e:PictureURL", namespaces=ns)
                    if val and url:
                        pic_by_value.setdefault(val, url)
            for v in item.findall("e:Variations/e:Variation", ns):
                specs = [(nv.findtext("e:Name", namespaces=ns) or "",
                          nv.findtext("e:Value", namespaces=ns) or "")
                         for nv in v.findall("e:VariationSpecifics/e:NameValueList", ns)]
                axval = next((val for n, val in specs if n == pic_axis), None) if pic_axis else None
                variations.append({
                    "sku": v.findtext("e:SKU", namespaces=ns),
                    "price": float(v.findtext("e:StartPrice", namespaces=ns) or 0),
                    "quantity": v.findtext("e:Quantity", namespaces=ns),
                    "specifics": specs,
                    "image": (pic_by_value.get(axval) if axval else None) or main_img,
                })
        return {"item_id": str(item_id),
                "current_price": float(cur) if cur else None,
                "title": (item.findtext("e:Title", namespaces=ns) if item is not None else None),
                "gallery": gallery, "variations": variations}

    async def republish_offer(self, offer_id: str) -> str:
        """publishOffer auf ein BEREITS publiziertes Offer: uebernimmt Inventory-/Offer-Aenderungen
        (Titel/Beschreibung/Merkmale) ins LIVE-Listing. Ohne Republish bleiben Inventory-Writes
        unsichtbar (Fund 29.07.: eBay zeigte weiter den alten Titel)."""
        headers = await self._auth_headers(content=True)
        try:
            resp = await self._http().post(f"{self._inv}/offer/{offer_id}/publish", headers=headers)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        return resp.json().get("listingId", "")

    async def get_item_pictures(self, item_id: str) -> list[str]:
        """GetItem: ALLE Bild-URLs eines KLASSISCHEN (Trading-API) Listings, Titelbild zuerst.
        PictureDetails/PictureURL (Hauptgalerie) + Varianten-Bilder dahinter. [] bei Fehler."""
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f'<ItemID>{item_id}</ItemID><IncludeItemSpecifics>false</IncludeItemSpecifics>'
            '</GetItemRequest>'
        )
        text = await self._trading_call("GetItem", body)
        import xml.etree.ElementTree as ET
        ns = {"e": "urn:ebay:apis:eBLBaseComponents"}
        try:
            item = ET.fromstring(text).find("e:Item", ns)
        except Exception:  # noqa: BLE001
            return []
        if item is None:
            return []
        urls: list[str] = []
        seen: set = set()

        def _add(u):
            u = (u or "").strip()
            if u and u not in seen:
                seen.add(u)
                urls.append(u)

        for pu in item.findall("e:PictureDetails/e:PictureURL", ns):
            _add(pu.text)
        for pu in item.findall("e:Variations/e:Pictures/e:VariationSpecificPictureSet/e:PictureURL", ns):
            _add(pu.text)
        return urls

    async def get_item_variation_pictures(self, item_id: str) -> dict:
        """GetItem: Varianten-Bilder eines KLASSISCHEN (Trading) Listings.

        Rueckgabe ``{axis_name, values: {variantenwert: bild_url}}`` aus
        Variations/Pictures/VariationSpecificPictureSet – das sind exakt die Bilder,
        die der Kaeufer auf eBay je Variante sieht (Orders-Vorschau)."""
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f'<ItemID>{item_id}</ItemID><IncludeItemSpecifics>false</IncludeItemSpecifics>'
            '</GetItemRequest>'
        )
        text = await self._trading_call("GetItem", body)
        import xml.etree.ElementTree as ET
        ns = {"e": "urn:ebay:apis:eBLBaseComponents"}
        try:
            item = ET.fromstring(text).find("e:Item", ns)
        except Exception:  # noqa: BLE001
            return {"axis_name": None, "values": {}}
        if item is None:
            return {"axis_name": None, "values": {}}
        pics = item.find("e:Variations/e:Pictures", ns)
        if pics is None:
            return {"axis_name": None, "values": {}}
        axis = pics.findtext("e:VariationSpecificName", namespaces=ns)
        values: dict[str, str] = {}
        for ps in pics.findall("e:VariationSpecificPictureSet", ns):
            val = ps.findtext("e:VariationSpecificValue", namespaces=ns)
            url = ps.findtext("e:PictureURL", namespaces=ns)
            if val and url:
                values[val] = url
        return {"axis_name": axis, "values": values}

    async def revise_item_pictures(self, item_id: str, image_urls: list[str]) -> None:
        """PictureDetails eines KLASSISCHEN Listings ersetzen – erstes Bild = Titelbild.
        Aendert NUR die Galerie; Varianten-spezifische Bilder bleiben unberuehrt."""
        import html as _html
        urls = [u for u in (image_urls or []) if u][:24]
        if not urls:
            return
        pics = "".join(f"<PictureURL>{_html.escape(u)}</PictureURL>" for u in urls)
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f'<Item><ItemID>{item_id}</ItemID>'
            f'<PictureDetails>{pics}</PictureDetails></Item>'
            '</ReviseFixedPriceItemRequest>'
        )
        await self._trading_call("ReviseFixedPriceItem", body)

    async def get_item_shipping_profile(self, item_id: str) -> dict:
        """GetItem: Versand-Policy (Business-Policy-Profil) eines KLASSISCHEN Listings.

        Fuer den Nur-DE-Umbau (19.08.): Klassik-Listings haben kein Inventory-Offer,
        tragen ihre Versand-Policy aber als SellerProfiles/SellerShippingProfile."""
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f'<ItemID>{item_id}</ItemID><IncludeItemSpecifics>false</IncludeItemSpecifics>'
            '</GetItemRequest>'
        )
        text = await self._trading_call("GetItem", body)
        import re as _re
        mid = _re.search(r"<ShippingProfileID>(\d+)</ShippingProfileID>", text)
        mname = _re.search(r"<ShippingProfileName>(.*?)</ShippingProfileName>", text, _re.S)
        return {"profile_id": (mid.group(1) if mid else None),
                "profile_name": (mname.group(1) if mname else None)}

    async def revise_item_shipping_profile(self, item_id: str, profile_id: str) -> None:
        """ReviseFixedPriceItem: KLASSISCHEM Listing eine andere Versand-Policy zuweisen.

        Aendert NUR das Versandprofil (SellerProfiles/SellerShippingProfile) —
        Preis, Menge und Varianten bleiben unberuehrt."""
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f'<Item><ItemID>{item_id}</ItemID>'
            '<SellerProfiles><SellerShippingProfile>'
            f'<ShippingProfileID>{profile_id}</ShippingProfileID>'
            '</SellerShippingProfile></SellerProfiles></Item>'
            '</ReviseFixedPriceItemRequest>'
        )
        await self._trading_call("ReviseFixedPriceItem", body)

    async def revise_item_price_smart(self, item_id: str, price_eur: float) -> dict:
        """Preis eines KLASSISCHEN Listings aendern – Varianten-faehig.

        Einzel-Listing -> ReviseInventoryStatus. MULTIVARIANTEN-Listing (eBay-Fehler
        21916736 bei ItemID-only) -> ReviseFixedPriceItem mit StartPrice je Variation
        (alle auf den Zielpreis). Rueckgabe: {variations: n}.
        """
        info = await self.get_item_price_info(item_id)
        p = f"{float(price_eur):.2f}"
        if not info["variations"]:
            body = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<ReviseInventoryStatusRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
                f'<InventoryStatus><ItemID>{item_id}</ItemID>'
                f'<StartPrice>{p}</StartPrice></InventoryStatus>'
                '</ReviseInventoryStatusRequest>'
            )
            await self._trading_call("ReviseInventoryStatus", body)
            return {"variations": 0}
        import html as _html
        var_xml = ""
        written = 0
        for v in info["variations"]:
            # MENGE MITSENDEN ist PFLICHT: fehlt <Quantity>, setzt eBay die Variation auf 0 (und
            # loescht nie-verkaufte Variationen). Ist die Menge unbekannt, wird die Variation GAR NICHT
            # geschrieben (bleibt unveraendert) – NIE ohne Menge senden.
            qty = None
            try:
                if v.get("quantity") is not None:
                    qty = int(str(v["quantity"]).strip())
            except (TypeError, ValueError):
                qty = None
            if qty is None:
                continue
            parts = []
            if v.get("sku"):
                parts.append(f"<SKU>{_html.escape(str(v['sku']))}</SKU>")
            if v.get("specifics"):
                nvl = "".join(f"<NameValueList><Name>{_html.escape(str(n))}</Name>"
                              f"<Value>{_html.escape(str(val))}</Value></NameValueList>"
                              for n, val in v["specifics"])
                parts.append(f"<VariationSpecifics>{nvl}</VariationSpecifics>")
            if not parts:
                continue
            var_xml += (f"<Variation>{''.join(parts)}"
                        f"<StartPrice>{p}</StartPrice><Quantity>{qty}</Quantity></Variation>")
            written += 1
        if not var_xml:
            raise PersistentError("Keine Variation mit bekannter Menge – Preis nicht gesetzt "
                                  "(Bestand wuerde sonst auf 0 fallen).")
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f'<Item><ItemID>{item_id}</ItemID><Variations>{var_xml}</Variations></Item>'
            '</ReviseFixedPriceItemRequest>'
        )
        await self._trading_call("ReviseFixedPriceItem", body)
        return {"variations": written}

    async def revise_variation_prices(self, item_id: str, variations: list[dict]) -> int:
        """ReviseFixedPriceItem: setzt je Variation IHREN EIGENEN StartPrice in EINEM Call.

        ``variations`` = [{"specifics": [(name, value), …], "sku": eBay-SKU|None,
        "quantity": int|None, "price_eur": float}] – jede Variation wird ueber ihre ECHTEN
        eBay-Merkmale (VariationSpecifics aus GetItem) identifiziert; die eBay-SKU wird – falls
        vorhanden – MITGESENDET (sonst wuerde eBay sie verwerfen). NUR die uebergebenen Variationen
        werden geaendert; alle anderen bleiben unveraendert (eBay laesst nicht genannte Variationen
        unberuehrt). Anders als revise_item_price_smart werden NICHT alle Variationen auf EINEN Preis
        gesetzt – jede behaelt ihren eigenen.

        WICHTIG: eBay setzt die Menge einer Variation auf 0 (und loescht nie-verkaufte Variationen),
        wenn ``Quantity`` in der Variation FEHLT. Darum wird die aktuelle Menge je Variation MIT
        gesendet; eine Variation OHNE bekannte Menge wird NICHT geschrieben (kein Bestands-Verlust).
        Rueckgabe: Anzahl geschriebener Variationen.
        """
        import html as _html
        if not variations:
            return 0
        var_xml = ""
        written = 0
        for v in variations:
            specifics = v.get("specifics") or []
            qty = v.get("quantity")
            if qty is None:
                continue   # ohne Menge NICHT schreiben – sonst nullt eBay den Bestand
            parts = []
            if v.get("sku"):
                parts.append(f"<SKU>{_html.escape(str(v['sku']))}</SKU>")
            if specifics:
                nvl = "".join(f"<NameValueList><Name>{_html.escape(str(n))}</Name>"
                              f"<Value>{_html.escape(str(val))}</Value></NameValueList>"
                              for n, val in specifics)
                parts.append(f"<VariationSpecifics>{nvl}</VariationSpecifics>")
            if not parts:
                continue   # ohne Identifikator NICHT schreiben (kein Blind-Flatten)
            p = f"{float(v['price_eur']):.2f}"
            var_xml += (f"<Variation>{''.join(parts)}"
                        f"<StartPrice>{p}</StartPrice><Quantity>{int(qty)}</Quantity></Variation>")
            written += 1
        if not var_xml:
            return 0
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f'<Item><ItemID>{item_id}</ItemID><Variations>{var_xml}</Variations></Item>'
            '</ReviseFixedPriceItemRequest>'
        )
        await self._trading_call("ReviseFixedPriceItem", body)
        return written

    async def revise_single_item_price(self, item_id: str, price_eur: float) -> dict:
        """NUR Einzel-Listing: StartPrice via ReviseInventoryStatus (aendert die Menge NICHT und
        kann NIE mehrere Variationen flach setzen). Bei einem Multivarianten-Listing lehnt eBay
        den ItemID-only-Preis ab (Fehler 21916736) -> _trading_call wirft -> Aufrufer meldet Fehler
        (fail-safe, kein Flatten). Nutzt KEIN GetItem, daher kein Re-Flatten durch Wackel-Antworten."""
        p = f"{float(price_eur):.2f}"
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<ReviseInventoryStatusRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f'<InventoryStatus><ItemID>{item_id}</ItemID>'
            f'<StartPrice>{p}</StartPrice></InventoryStatus>'
            '</ReviseInventoryStatusRequest>'
        )
        await self._trading_call("ReviseInventoryStatus", body)
        return {"variations": 0}

    async def search_competitor_offers(self, keywords: str, *, limit: int = 20) -> list[dict]:
        """MARKTRECHERCHE mit PREISEN: aktive Konkurrenz-Listings via Browse API.

        Dieselbe Suche wie search_competitor_titles, liest aber die bereits in der
        Antwort enthaltenen Preise mit aus. Rueckgabe je Treffer:
        {title, price_eur, currency, url, item_id, condition, sold(=None)}.
        Echte Verkaufszahlen liefert Browse NICHT (nur die gesperrte Insights-API) ->
        sold bleibt None. Best-effort: Fehler -> []. Preise nur in Marktplatz-Waehrung
        (EBAY_DE = EUR); Fremdwaehrung -> price_eur None (nicht raten).
        """
        q = (keywords or "").strip()
        if not q:
            return []
        try:
            tok = await self._get_app_token()
            resp = await self._http().get(
                f"{self._host}/buy/browse/v1/item_summary/search",
                headers={"Authorization": f"Bearer {tok}",
                         "X-EBAY-C-MARKETPLACE-ID": self.settings.ebay_marketplace_id},
                params={"q": q[:100], "limit": min(max(limit, 1), 50),
                        "filter": "buyingOptions:{FIXED_PRICE}"})
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 – Recherche ist best-effort
            logger.warning("competitor search failed", extra={"error": str(exc)[:160]})
            return []
        out: list[dict] = []
        for it in (resp.json().get("itemSummaries") or []):
            title = (it.get("title") or "").strip()
            if not title:
                continue
            price = it.get("price") or {}
            cur = price.get("currency")
            try:
                val = float(price.get("value")) if price.get("value") is not None else None
            except (TypeError, ValueError):
                val = None
            out.append({
                "title": title,
                "price_eur": val if cur == "EUR" else None,
                "currency": cur,
                "url": it.get("itemWebUrl"),
                "item_id": it.get("itemId"),
                "condition": it.get("condition"),
                "sold": None,   # Browse liefert keine Verkaufszahlen
            })
            if len(out) >= limit:
                break
        return out

    async def search_competitor_titles(self, keywords: str, *, limit: int = 12) -> list[str]:
        """MARKTRECHERCHE: Konkurrenz-Titel zu Keywords (duenner Wrapper auf offers)."""
        offers = await self.search_competitor_offers(keywords, limit=limit)
        return [o["title"] for o in offers if o.get("title")][:limit]

    async def update_ad_rate(self, listing_ids: list[str], *, bid_pct: float) -> dict:
        """Anzeigentarif BESTEHENDER Anzeigen anheben/aendern (bulkUpdateAdsBidByListingId).

        promote_listings legt Ads NEU an (409 bei vorhandenen -> Tarif bleibt). Diese
        Methode aendert den Tarif einer schon beworbenen Anzeige wirklich. bid_pct als
        Bruch (0.15 = 15%).
        """
        rate = bid_pct * 100
        campaign = await self.ensure_ad_campaign()
        headers = await self._marketing_headers()
        body = {"requests": [{"listingId": str(lid), "bidPercentage": f"{rate:.1f}"}
                             for lid in listing_ids if lid]}
        try:
            resp = await self._http().post(
                f"{self._host}/sell/marketing/v1/ad_campaign/{campaign}/bulk_update_ads_bid_by_listing_id",
                headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json() or {}
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        updated = failed = 0
        for r in data.get("responses") or []:
            if int(r.get("statusCode") or 0) in (200, 201):
                updated += 1
            else:
                failed += 1
        return {"campaign_id": campaign, "rate_pct": round(rate, 1),
                "updated": updated, "failed": failed}

    async def set_ad_rate(self, listing_ids: list[str], *, bid_pct: float) -> dict:
        """Anzeigentarif setzen – legt an ODER hebt an (create, dann update bei 'already').

        Deckt beide Faelle ab: Listing noch nicht beworben -> create; schon beworben
        (Normalfall, golive setzt 10%) -> update auf den neuen Tarif.
        """
        res = await self.promote_listings(listing_ids, bid_pct=bid_pct)
        if res.get("already"):
            upd = await self.update_ad_rate(listing_ids, bid_pct=bid_pct)
            res["updated"] = upd.get("updated", 0)
        return res

    async def get_ad_rates(self, listing_ids: Optional[list[str]] = None) -> dict[str, float]:
        """Echte Anzeigenraten der Standard-Kampagne lesen -> {listing_id: bid_bruch}.

        Liest die Ads der COST_PER_SALE-Standard-Kampagne (ensure_ad_campaign) und
        gibt je Listing die bidPercentage als BRUCH zurueck ("12.0" -> 0.12). Nur Ads
        mit numerischer bidPercentage werden aufgenommen. `listing_ids` (optional)
        filtert das Ergebnis auf diese Listings.

        Robust: existiert keine Kampagne / fehlt der Marketing-Scope (wie bei
        promote_listings via _get_marketing_token), wird {} zurueckgegeben statt zu
        crashen – der Aufrufer faellt dann auf die Pauschale zurueck.
        """
        wanted = {str(x) for x in listing_ids} if listing_ids else None
        try:
            campaign = await self.ensure_ad_campaign()
            headers = await self._marketing_headers()
        except Exception as exc:  # noqa: BLE001 – kein Scope/keine Kampagne -> leeres Ergebnis
            logger.info("get_ad_rates: keine Kampagne/kein Scope (%s) -> Pauschale", exc)
            return {}

        base = f"{self._host}/sell/marketing/v1/ad_campaign/{campaign}/ad"
        rates: dict[str, float] = {}
        offset, limit = 0, 200
        _MAX_PAGES = 200  # Sicherheitsnetz gegen Endlos-Paginierung (~40k Ads)
        try:
            for _ in range(_MAX_PAGES):
                resp = await self._http().get(
                    base, params={"limit": str(limit), "offset": str(offset)},
                    headers=headers)
                resp.raise_for_status()
                data = resp.json() or {}
                ads = data.get("ads") or []
                for ad in ads:
                    lid = ad.get("listingId")
                    if not lid:
                        continue
                    raw = ad.get("bidPercentage")
                    if raw is None or raw == "":
                        continue
                    try:
                        frac = float(raw) / 100.0
                    except (TypeError, ValueError):
                        continue
                    rates[str(lid)] = frac
                # Paginierung: eBay liefert total (Gesamtzahl) und/oder next (Folge-URL).
                # Weiter, solange noch Ads unter dem gemeldeten total ausstehen ODER
                # eBay ein 'next' angibt; sonst fertig.
                offset += len(ads)
                try:
                    total = int(data.get("total"))
                except (TypeError, ValueError):
                    total = None
                more = bool(data.get("next")) or (total is not None and offset < total)
                if not ads or not more:
                    break
        except Exception as exc:  # noqa: BLE001 – Read-only, nie den Aufrufer haerten
            logger.warning("get_ad_rates: Lesen fehlgeschlagen (%s)", exc)
            return {}

        if wanted is not None:
            return {lid: r for lid, r in rates.items() if lid in wanted}
        return rates

    async def revise_item_title(self, item_id: str, title: str) -> None:
        """Titel eines KLASSISCHEN Fixed-Price-Listings aendern (ReviseFixedPriceItem).

        eBay-Titel sind auf 80 Zeichen begrenzt. Nur der Titel wird angefasst –
        Preis/Varianten/Bestand bleiben unberuehrt (read-merge nicht noetig, da eBay
        bei ReviseFixedPriceItem nur die uebergebenen Felder aendert).
        """
        import html as _html
        safe = _html.escape((title or "").strip()[:80])
        if not safe:
            raise PersistentError("Leerer Titel – Revise abgelehnt")
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f'<Item><ItemID>{item_id}</ItemID><Title>{safe}</Title></Item>'
            '</ReviseFixedPriceItemRequest>'
        )
        await self._trading_call("ReviseFixedPriceItem", body)

    async def raise_item_price_delta(self, item_id: str, delta_eur: float) -> dict:
        """Preis eines KLASSISCHEN Listings um einen BETRAG erhoehen – je Variante!

        Fuer die 3€-Versand-Migration: jede Variation bekommt ihren EIGENEN alten
        Preis + delta (kein Plaetten auf Einheitspreis). Rueckgabe: erwartete
        Zielpreise fuer die Read-back-Verifikation ({sku_oder_index: preis}).
        """
        info = await self.get_item_price_info(item_id)
        if not info["variations"]:
            new_p = round((info["current_price"] or 0) + delta_eur, 2)
            body = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<ReviseInventoryStatusRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
                f'<InventoryStatus><ItemID>{item_id}</ItemID>'
                f'<StartPrice>{new_p:.2f}</StartPrice></InventoryStatus>'
                '</ReviseInventoryStatusRequest>'
            )
            await self._trading_call("ReviseInventoryStatus", body)
            return {"expected": {"_single": new_p}, "old_max": info["current_price"],
                    "new_max": new_p}
        import html as _html
        var_xml = ""
        expected: dict[str, float] = {}
        for i, v in enumerate(info["variations"]):
            new_p = round(v["price"] + delta_eur, 2)
            key = v.get("sku") or f"_idx{i}"
            expected[key] = new_p
            ident = (f"<SKU>{_html.escape(v['sku'])}</SKU>" if v.get("sku") else "")
            if not ident:
                nvl = "".join(f"<NameValueList><Name>{_html.escape(n)}</Name>"
                              f"<Value>{_html.escape(val)}</Value></NameValueList>"
                              for n, val in v["specifics"])
                ident = f"<VariationSpecifics>{nvl}</VariationSpecifics>"
            var_xml += f"<Variation>{ident}<StartPrice>{new_p:.2f}</StartPrice></Variation>"
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f'<Item><ItemID>{item_id}</ItemID><Variations>{var_xml}</Variations></Item>'
            '</ReviseFixedPriceItemRequest>'
        )
        await self._trading_call("ReviseFixedPriceItem", body)
        return {"expected": expected,
                "old_max": max(v["price"] for v in info["variations"]),
                "new_max": max(expected.values())}

    async def verify_item_prices_map(self, item_id: str, expected: dict,
                                     *, retries: int = 1) -> bool:
        """READ-BACK fuer Delta-Aenderungen: stimmt JEDE Variante mit dem Ziel ueberein?"""
        import asyncio as _aio
        for attempt in range(retries + 1):
            info = await self.get_item_price_info(item_id)
            if "_single" in expected:
                ok = (info["current_price"] is not None and not info["variations"]
                      and abs(info["current_price"] - expected["_single"]) < 0.02)
            else:
                by_key = {(v.get("sku") or f"_idx{i}"): v["price"]
                          for i, v in enumerate(info["variations"])}
                ok = (len(by_key) == len(expected)
                      and all(k in by_key and abs(by_key[k] - p) < 0.02
                              for k, p in expected.items()))
            if ok:
                return True
            if attempt < retries:
                await _aio.sleep(2.5)
        return False

    async def verify_item_price(self, item_id: str, expected_eur: float,
                                *, retries: int = 1) -> bool:
        """READ-BACK: stimmt der Live-Preis auf eBay mit dem Zielpreis ueberein?

        Bei Varianten muessen ALLE Variationen auf dem Zielpreis stehen (unser
        Preis-Update setzt sie einheitlich).
        """
        import asyncio as _aio
        for attempt in range(retries + 1):
            info = await self.get_item_price_info(item_id)
            if info["variations"]:
                prices = [v["price"] for v in info["variations"]]
                ok = all(abs(x - float(expected_eur)) < 0.02 for x in prices)
            else:
                ok = (info["current_price"] is not None
                      and abs(info["current_price"] - float(expected_eur)) < 0.02)
            if ok:
                return True
            if attempt < retries:
                await _aio.sleep(2.5)   # eBay-Replikation kurz abwarten
        return False

    async def revise_item_price(self, item_id: str, price_eur: float) -> None:
        """Kompatibilitaets-Wrapper: nur den Preis eines klassischen Listings aendern."""
        await self.revise_item_status(item_id, price_eur=price_eur)

    async def revise_item_status(self, item_id: str, *, price_eur: float | None = None,
                                 quantity: int | None = None) -> None:
        """Trading API ReviseInventoryStatus: Preis und/oder MENGE eines KLASSISCHEN
        Listings aendern.

        Noetig fuer importierte (AutoDS-)Listings, die nicht ueber die Inventory API
        laufen (kein Offer zur SKU vorhanden) – u.a. fuer den Bestands-Sync
        (Lieferant ausverkauft -> Menge 0, wieder lieferbar -> Menge zurueck).

        Grenze: klassische MULTIVARIANTEN-Listings verlangen die Variations-SKU, die
        uns fuer importierte Listings fehlt -> eBay antwortet mit Failure, der Aufrufer
        (``_safe_ebay_update``) loggt und setzt monitor_status='error' (kein Silent-Fail).
        """
        if price_eur is None and quantity is None:
            return
        token = await self._get_user_token()
        site = self._SITE_ID.get(self.settings.ebay_marketplace_id, "77")
        fields = f"<ItemID>{item_id}</ItemID>"
        if price_eur is not None:
            fields += f"<StartPrice>{float(price_eur):.2f}</StartPrice>"
        if quantity is not None:
            fields += f"<Quantity>{int(quantity)}</Quantity>"
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<ReviseInventoryStatusRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f'<InventoryStatus>{fields}</InventoryStatus>'
            '</ReviseInventoryStatusRequest>'
        )
        headers = {
            "X-EBAY-API-CALL-NAME": "ReviseInventoryStatus",
            "X-EBAY-API-SITEID": site,
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1155",
            "X-EBAY-API-IAF-TOKEN": token,
            "Content-Type": "text/xml",
        }
        try:
            resp = await self._http().post(f"{self._host}/ws/api.dll",
                                           headers=headers, content=body.encode("utf-8"))
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        text = resp.text
        if "<Ack>Failure</Ack>" in text:
            import re as _re
            m = _re.search(r"<LongMessage>(.*?)</LongMessage>", text, _re.S)
            raise PersistentError(f"ReviseInventoryStatus: {(m.group(1) if m else text)[:200]}")

    async def revise_variation_quantities(self, item_id: str,
                                          updates: list[tuple[str, int]]) -> None:
        """ReviseInventoryStatus je VARIATION (ItemID+SKU): Mengen klassischer
        Multivarianten-Listings setzen (max. 4 Eintraege je Request -> Chunking).

        Noetig fuer importierte Listings ohne Inventory-Offers (Listing 200:
        die Ausweich-Reaktivierung einer Variante erreichte eBay nie)."""
        from xml.sax.saxutils import escape as _esc
        if not updates:
            return
        token = await self._get_user_token()
        site = self._SITE_ID.get(self.settings.ebay_marketplace_id, "77")
        headers = {
            "X-EBAY-API-CALL-NAME": "ReviseInventoryStatus",
            "X-EBAY-API-SITEID": site,
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1155",
            "X-EBAY-API-IAF-TOKEN": token,
            "Content-Type": "text/xml",
        }
        for i in range(0, len(updates), 4):
            inner = "".join(
                f"<InventoryStatus><ItemID>{item_id}</ItemID>"
                f"<SKU>{_esc(str(sku))}</SKU><Quantity>{int(q)}</Quantity>"
                "</InventoryStatus>"
                for sku, q in updates[i:i + 4])
            body = ('<?xml version="1.0" encoding="utf-8"?>'
                    '<ReviseInventoryStatusRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
                    + inner + '</ReviseInventoryStatusRequest>')
            try:
                resp = await self._http().post(f"{self._host}/ws/api.dll",
                                               headers=headers,
                                               content=body.encode("utf-8"))
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                raise self._translate(exc) from exc
            text = resp.text
            if "<Ack>Failure</Ack>" in text:
                import re as _re
                m = _re.search(r"<LongMessage>(.*?)</LongMessage>", text, _re.S)
                raise PersistentError(
                    f"ReviseInventoryStatus(Variation): {(m.group(1) if m else text)[:200]}")

    async def revise_variation_quantities_by_specifics(
            self, item_id: str,
            updates: list[tuple[list[tuple[str, str]], int]]) -> None:
        """ReviseFixedPriceItem: Mengen je VARIATION ueber die Merkmalspaare setzen.

        Noetig fuer importierte Klassik-Listings, deren Variationen KEINE SKUs tragen
        (Listing 200: ReviseInventoryStatus verlangte SKUs und wurde live abgelehnt)."""
        from xml.sax.saxutils import escape as _esc
        if not updates:
            return
        token = await self._get_user_token()
        site = self._SITE_ID.get(self.settings.ebay_marketplace_id, "77")
        variations = "".join(
            "<Variation>"
            f"<Quantity>{int(q)}</Quantity>"
            "<VariationSpecifics>"
            + "".join(f"<NameValueList><Name>{_esc(str(n))}</Name>"
                      f"<Value>{_esc(str(v))}</Value></NameValueList>"
                      for n, v in specs)
            + "</VariationSpecifics></Variation>"
            for specs, q in updates)
        body = ('<?xml version="1.0" encoding="utf-8"?>'
                '<ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
                f'<Item><ItemID>{item_id}</ItemID>'
                f'<Variations>{variations}</Variations></Item>'
                '</ReviseFixedPriceItemRequest>')
        headers = {
            "X-EBAY-API-CALL-NAME": "ReviseFixedPriceItem",
            "X-EBAY-API-SITEID": site,
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1155",
            "X-EBAY-API-IAF-TOKEN": token,
            "Content-Type": "text/xml",
        }
        try:
            resp = await self._http().post(f"{self._host}/ws/api.dll",
                                           headers=headers,
                                           content=body.encode("utf-8"))
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        text = resp.text
        if "<Ack>Failure</Ack>" in text:
            import re as _re
            m = _re.search(r"<LongMessage>(.*?)</LongMessage>", text, _re.S)
            raise PersistentError(
                f"ReviseFixedPriceItem(Variation): {(m.group(1) if m else text)[:200]}")

    async def get_traffic_batch(self, item_ids: list[str], *, days: int = 30) -> dict[str, dict]:
        """Analytics Traffic-Report fuer viele Listings (200 je Call, dimension=LISTING).

        Rueckgabe: {item_id: {views, impressions}} – Aufrufe/Impressionen der letzten
        ``days`` Tage. Fehlende Listings (0 Traffic) fehlen im Ergebnis.
        """
        end = datetime.now(timezone.utc) - timedelta(days=1)   # heute ist oft noch leer
        start = end - timedelta(days=days)
        date_range = f"[{start.strftime('%Y%m%d')}..{end.strftime('%Y%m%d')}]"
        out: dict[str, dict] = {}
        ids = [str(i) for i in item_ids if i]
        for ofs in range(0, len(ids), 200):
            chunk = ids[ofs:ofs + 200]
            params = {
                "dimension": "LISTING",
                "filter": f"listing_ids:{{{'|'.join(chunk)}}},date_range:{date_range}",
                "metric": "LISTING_VIEWS_TOTAL,LISTING_IMPRESSION_TOTAL",
            }
            data = await self._get_json(self._analytics, params=params,
                                        headers=await self._auth_headers()) or {}
            metrics = [m.get("key") for m in (data.get("header") or {}).get("metrics") or []]
            for rec in data.get("records") or []:
                dims = rec.get("dimensionValues") or []
                lid = str((dims[0] or {}).get("value") or "") if dims else ""
                if not lid:
                    continue
                vals = rec.get("metricValues") or []
                by_key = {metrics[i]: (vals[i] or {}).get("value")
                          for i in range(min(len(metrics), len(vals)))}
                out[lid] = {
                    "views": int(float(by_key.get("LISTING_VIEWS_TOTAL") or 0)),
                    "impressions": int(float(by_key.get("LISTING_IMPRESSION_TOTAL") or 0)),
                }
        return out

    async def end_item(self, item_id: str, *, reason: str = "NotAvailable") -> None:
        """Trading API EndItem: klassisches Listing endgueltig beenden (delisten)."""
        token = await self._get_user_token()
        site = self._SITE_ID.get(self.settings.ebay_marketplace_id, "77")
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<EndItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f'<ItemID>{item_id}</ItemID><EndingReason>{reason}</EndingReason>'
            '</EndItemRequest>'
        )
        headers = {
            "X-EBAY-API-CALL-NAME": "EndItem",
            "X-EBAY-API-SITEID": site,
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1155",
            "X-EBAY-API-IAF-TOKEN": token,
            "Content-Type": "text/xml",
        }
        try:
            resp = await self._http().post(f"{self._host}/ws/api.dll",
                                           headers=headers, content=body.encode("utf-8"))
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        text = resp.text
        if "<Ack>Failure</Ack>" in text:
            import re as _re
            m = _re.search(r"<LongMessage>(.*?)</LongMessage>", text, _re.S)
            raise PersistentError(f"EndItem: {(m.group(1) if m else text)[:200]}")

    async def get_item_category(self, item_id: str):
        """Trading API GetItem: PrimaryCategory (ID + Name) eines Listings. GetMyeBaySelling
        liefert die Kategorie nicht mit – daher je Artikel dieser gezielte Call."""
        token = await self._get_user_token()
        site = self._SITE_ID.get(self.settings.ebay_marketplace_id, "77")
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f'<ItemID>{item_id}</ItemID>'
            '<OutputSelector>Item.PrimaryCategory</OutputSelector>'
            '</GetItemRequest>'
        )
        headers = {
            "X-EBAY-API-CALL-NAME": "GetItem",
            "X-EBAY-API-SITEID": site,
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1155",
            "X-EBAY-API-IAF-TOKEN": token,
            "Content-Type": "text/xml",
        }
        try:
            resp = await self._http().post(f"{self._host}/ws/api.dll",
                                           headers=headers, content=body.encode("utf-8"))
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        import xml.etree.ElementTree as ET
        ns = {"e": "urn:ebay:apis:eBLBaseComponents"}
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError:
            return None, None
        return (root.findtext("e:Item/e:PrimaryCategory/e:CategoryID", namespaces=ns),
                root.findtext("e:Item/e:PrimaryCategory/e:CategoryName", namespaces=ns))

    async def get_item_shipping_profile(self, item_id: str) -> dict:
        """Trading GetItem: Versand-Policy (SellerProfiles) eines Live-Listings lesen."""
        token = await self._get_user_token()
        site = self._SITE_ID.get(self.settings.ebay_marketplace_id, "77")
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f'<ItemID>{item_id}</ItemID>'
            '<OutputSelector>Item.SellerProfiles</OutputSelector>'
            '</GetItemRequest>'
        )
        headers = {
            "X-EBAY-API-CALL-NAME": "GetItem",
            "X-EBAY-API-SITEID": site,
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1155",
            "X-EBAY-API-IAF-TOKEN": token,
            "Content-Type": "text/xml",
        }
        try:
            resp = await self._http().post(f"{self._host}/ws/api.dll",
                                           headers=headers, content=body.encode("utf-8"))
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        text = resp.text
        if "<Ack>Failure</Ack>" in text:
            import re as _re
            m = _re.search(r"<LongMessage>(.*?)</LongMessage>", text, _re.S)
            raise PersistentError(f"GetItem: {(m.group(1) if m else text)[:200]}")
        import re as _re
        pid = _re.search(r"<ShippingProfileID>(\d+)</ShippingProfileID>", text)
        pname = _re.search(r"<ShippingProfileName>(.*?)</ShippingProfileName>", text, _re.S)
        return {"profile_id": pid.group(1) if pid else None,
                "profile_name": (pname.group(1) if pname else None)}

    async def revise_item_shipping_profile(self, item_id: str, profile_id: str) -> None:
        """Trading ReviseFixedPriceItem: Versand-Policy eines Live-Listings wechseln
        (z.B. 3€-Zuschlag -> Kostenloser Versand), ohne sonst etwas anzufassen."""
        token = await self._get_user_token()
        site = self._SITE_ID.get(self.settings.ebay_marketplace_id, "77")
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f'<Item><ItemID>{item_id}</ItemID>'
            '<SellerProfiles><SellerShippingProfile>'
            f'<ShippingProfileID>{profile_id}</ShippingProfileID>'
            '</SellerShippingProfile></SellerProfiles></Item>'
            '</ReviseFixedPriceItemRequest>'
        )
        headers = {
            "X-EBAY-API-CALL-NAME": "ReviseFixedPriceItem",
            "X-EBAY-API-SITEID": site,
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1155",
            "X-EBAY-API-IAF-TOKEN": token,
            "Content-Type": "text/xml",
        }
        try:
            resp = await self._http().post(f"{self._host}/ws/api.dll",
                                           headers=headers, content=body.encode("utf-8"))
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        text = resp.text
        if "<Ack>Failure</Ack>" in text:
            import re as _re
            m = _re.search(r"<LongMessage>(.*?)</LongMessage>", text, _re.S)
            raise PersistentError(f"ReviseFixedPriceItem: {(m.group(1) if m else text)[:200]}")

    async def list_fulfillment_policies(self) -> list[dict]:
        """Alle Versand-Policies inkl. Kosten je Service (fuer den 3€-Zuschlag-Check)."""
        data = await self._get_json(f"{self._account}/fulfillment_policy",
                                    params={"marketplace_id": self.settings.ebay_marketplace_id})
        out = []
        for p in (data or {}).get("fulfillmentPolicies") or []:
            services = []
            for opt in p.get("shippingOptions") or []:
                for svc in opt.get("shippingServices") or []:
                    services.append({
                        "cost": float((svc.get("shippingCost") or {}).get("value") or 0),
                        "free": bool(svc.get("freeShipping")),
                        "type": opt.get("optionType"),
                    })
            out.append({"policy_id": p.get("fulfillmentPolicyId"), "name": p.get("name"),
                        "services": services,
                        "has_surcharge": any(s["cost"] > 0 and not s["free"]
                                             and s["type"] != "INTERNATIONAL" for s in services)})
        return out

    async def make_fulfillment_policy_free(self, policy_id: str) -> dict:
        """Versand-Policy auf KOSTENLOS stellen (Zuschlag entfernen) – wirkt sofort auf
        ALLE Listings, die diese Policy nutzen (kein Einzel-Revise je Artikel noetig).
        """
        data = await self._get_json(f"{self._account}/fulfillment_policy/{policy_id}")
        if not data:
            raise PersistentError(f"Versand-Policy {policy_id} nicht gefunden")
        zeroed = 0
        for opt in data.get("shippingOptions") or []:
            domestic = (opt.get("optionType") or "").upper() != "INTERNATIONAL"
            if not domestic:
                continue   # Auslandsversand kostet real Geld – NICHT auf 0 stellen
            for i, svc in enumerate(opt.get("shippingServices") or []):
                cost = (svc.get("shippingCost") or {}).get("value")
                if cost is not None and float(cost) != 0.0:
                    zeroed += 1
                svc["shippingCost"] = {"value": "0.00", "currency": self._currency}
                if svc.get("additionalShippingCost") is not None:
                    svc["additionalShippingCost"] = {"value": "0.00", "currency": self._currency}
                if i == 0:
                    svc["freeShipping"] = True   # eBay-Badge "Kostenloser Versand"
        body = {k: v for k, v in data.items() if k != "fulfillmentPolicyId"}
        headers = await self._auth_headers()
        headers["Content-Type"] = "application/json"
        await self._send(self._http().put,
                         f"{self._account}/fulfillment_policy/{policy_id}", headers, body)
        return {"policy_id": policy_id, "name": data.get("name"), "services_zeroed": zeroed}

    async def create_fulfillment_policy(self, body: dict) -> str:
        """Neue Versand-Policy anlegen (POST) -> fulfillmentPolicyId.

        Fuer die Laender-Ausschluss-Varianten (16.08.): bestehende Policies werden
        NIE geaendert (sie wirken auf ALLE zugeordneten Listings) — je Ausschluss-
        Muster wird eine KOPIE mit erweitertem regionExcluded neu angelegt."""
        headers = await self._auth_headers()
        headers["Content-Type"] = "application/json"
        try:
            resp = await self._http().post(f"{self._account}/fulfillment_policy",
                                           headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        pid = str((data or {}).get("fulfillmentPolicyId") or "")
        if not pid:
            raise PersistentError("createFulfillmentPolicy: Antwort ohne fulfillmentPolicyId")
        return pid

    async def get_active_listings(self, *, max_items: int = 10000) -> list[dict]:
        """Aktive Angebote via Trading-API GetMyeBaySelling (auch AutoDS-/Legacy-Listings).

        Paginiert VOLLSTAENDIG bis TotalNumberOfPages. ``max_items`` ist nur eine grosszuegige
        Sicherheits-Obergrenze gegen eine Endlosschleife, NICHT der eigentliche Limiter:
        frueher kappte der Default 500 (via ``while len(out) < max_items``) einen 615-Artikel-Shop
        auf 500 -> die 115 nicht geholten, aber LIVE Listings fielen im Import-Reconcile
        faelschlich auf ``ended`` (Vorfall 17.07.). Nutzt den OAuth-User-Token als IAF-Token; die
        Sell-Inventory-API zeigt nur selbst erstellte Items – die Shop-Listings liefert nur Trading.
        """
        token = await self._get_user_token()
        site = self._SITE_ID.get(self.settings.ebay_marketplace_id, "77")
        out: list[dict] = []
        page = 1
        total_pages = 1
        complete = False
        while True:
            body = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<GetMyeBaySellingRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
                '<ActiveList><Include>true</Include>'
                f'<Pagination><EntriesPerPage>200</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>'
                '</ActiveList></GetMyeBaySellingRequest>'
            )
            headers = {
                "X-EBAY-API-CALL-NAME": "GetMyeBaySelling",
                "X-EBAY-API-SITEID": site,
                "X-EBAY-API-COMPATIBILITY-LEVEL": "1155",
                "X-EBAY-API-IAF-TOKEN": token,
                "Content-Type": "text/xml",
            }
            try:
                resp = await self._http().post(f"{self._host}/ws/api.dll",
                                               headers=headers, content=body.encode("utf-8"))
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                raise self._translate(exc) from exc
            items, page_total, error = self._parse_active_list(resp.text)
            if error:
                raise PersistentError(f"Trading-API GetMyeBaySelling: {error}")
            out.extend(items)
            if page_total is None:
                # Degeneriertes Ergebnis OHNE verwertbares PaginationResult -> Seitenumfang unbekannt.
                # Diese Seite noch mitnehmen, aber den Fetch NIE als vollstaendig werten – auch auf
                # SEITE 1, wo es keinen frueheren total_pages-Anker gibt, gegen den Stickiness
                # schuetzen koennte (residual-Fund 17.07.). -> Reconcile pausiert.
                complete = False
                break
            # total_pages STICKY (nie zuruecksetzen): eine spaetere Seite darf den bereits von
            # Seite 1 bekannten Seitenumfang nicht unterschreiten – sonst wuerde `page >= total_pages`
            # faelschlich True und der unvollstaendige Fetch als vollstaendig markiert.
            total_pages = max(total_pages, page_total)
            if not items:
                # Leere Seite: nur vollstaendig, wenn wir wirklich auf/hinter der letzten Seite sind
                # (leerer Shop). Eine leere ZWISCHENseite -> unvollstaendig, Reconcile pausiert.
                complete = page >= total_pages
                break
            if page >= total_pages:
                complete = True          # sauber bis zur letzten Seite paginiert
                break
            if len(out) >= max_items:
                complete = False         # Sicherheits-Obergrenze vor letzter Seite -> gekappt
                break
            page += 1
        # VOLLSTAENDIGKEITS-SIGNAL fuer den Import-Reconcile: NUR ein bis zur letzten Seite
        # komplett paginierter Fetch darf fehlende Listings auf 'ended' setzen. Ein gekappter
        # oder auf einer leeren Zwischenseite abgebrochener Fetch ist unvollstaendig -> der
        # Reconcile pausiert, statt live Listings faelschlich zu beenden (Ursache 615->500).
        self._last_active_fetch_complete = complete
        return out[:max_items]

    @staticmethod
    def _parse_active_list(xml_text: str):
        """GetMyeBaySelling-XML -> (items, total_pages_or_None, error_or_None).

        total_pages ist None, wenn die Antwort kein verwertbares PaginationResult hatte -> der
        Aufrufer darf den Fetch dann NICHT als vollstaendig werten (statt 1 anzunehmen).
        """
        import xml.etree.ElementTree as ET

        ns = {"e": "urn:ebay:apis:eBLBaseComponents"}
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            return [], 1, f"XML-Parse-Fehler: {exc}"
        ack = root.findtext("e:Ack", namespaces=ns) or ""
        if ack.lower() == "failure":
            msg = root.findtext("e:Errors/e:LongMessage", namespaces=ns) \
                or root.findtext("e:Errors/e:ShortMessage", namespaces=ns) or "unbekannter Fehler"
            return [], 1, msg
        active = root.find("e:ActiveList", ns)
        if active is None:
            return [], None, None              # kein ActiveList -> Seitenumfang UNBEKANNT (None)
        # total_pages == None bedeutet: PaginationResult fehlte oder war unlesbar -> die Antwort ist
        # degeneriert, ihr Seitenumfang darf NICHT als "1" (und damit als vollstaendig) angenommen
        # werden. Nur eine echte, geparste Zahl ist vertrauenswuerdig.
        total_pages = None
        pag = active.find("e:PaginationResult", ns)
        if pag is not None:
            raw = pag.findtext("e:TotalNumberOfPages", namespaces=ns)
            if raw not in (None, ""):
                try:
                    total_pages = int(raw)
                except ValueError:
                    total_pages = None
        items = []
        for it in active.findall("e:ItemArray/e:Item", ns):
            price_el = it.find("e:SellingStatus/e:CurrentPrice", ns)
            price = price_el.text if price_el is not None else None
            currency = price_el.get("currencyID") if price_el is not None else None
            items.append({
                "item_id": it.findtext("e:ItemID", namespaces=ns),
                "title": it.findtext("e:Title", namespaces=ns),
                "sku": it.findtext("e:SKU", namespaces=ns),
                "price": price,
                "currency": currency,
                "quantity": it.findtext("e:Quantity", namespaces=ns),
                "quantity_sold": it.findtext("e:SellingStatus/e:QuantitySold", namespaces=ns),
                "category_id": it.findtext("e:PrimaryCategory/e:CategoryID", namespaces=ns),
                "category_name": it.findtext("e:PrimaryCategory/e:CategoryName", namespaces=ns),
                "view_url": it.findtext("e:ListingDetails/e:ViewItemURL", namespaces=ns),
                "gallery_url": it.findtext("e:PictureDetails/e:GalleryURL", namespaces=ns),
                # Echtes eBay-Einstelldatum ("seit wann online").
                "start_time": it.findtext("e:ListingDetails/e:StartTime", namespaces=ns),
            })
        return items, total_pages, None

    # ------------------------------------------------------------------ Account API (Policies + Location)
    async def get_business_policies(self) -> dict:
        """Liest die vorhandenen Business-Policy-IDs (erste je Typ) fuer den Marktplatz.

        Gibt {"payment": id|None, "fulfillment": id|None, "return": id|None} zurueck.
        Braucht den OAuth-Scope `sell.account`.
        """
        mp = self.settings.ebay_marketplace_id
        s = self.settings
        overrides = {"payment": s.ebay_payment_policy_id, "fulfillment": s.ebay_fulfillment_policy_id,
                     "return": s.ebay_return_policy_id}
        out = {"payment": None, "fulfillment": None, "return": None}
        for key, path, id_field, list_field in [
            ("payment", "payment_policy", "paymentPolicyId", "paymentPolicies"),
            ("fulfillment", "fulfillment_policy", "fulfillmentPolicyId", "fulfillmentPolicies"),
            ("return", "return_policy", "returnPolicyId", "returnPolicies"),
        ]:
            if overrides[key]:
                out[key] = overrides[key]
                continue
            data = await self._get_json(f"{self._account}/{path}",
                                        params={"marketplace_id": mp},
                                        headers=await self._auth_headers())
            policies = (data or {}).get(list_field) or []
            if not policies:
                continue
            chosen = policies[0]
            if key == "fulfillment":
                # Standard bevorzugt: KOSTENLOSER Versand (freeShipping in einer Versandoption).
                for pol in policies:
                    opts = pol.get("shippingOptions") or []
                    services = [svc for o in opts for svc in (o.get("shippingServices") or [])]
                    if any(svc.get("freeShipping") for svc in services):
                        chosen = pol
                        break
            out[key] = chosen.get(id_field)
        return out

    async def get_inventory_locations(self) -> list[dict]:
        """getInventoryLocations -> Liste vorhandener Merchant-Locations."""
        data = await self._get_json(f"{self._inv}/location", params={"limit": "100"})
        return (data or {}).get("locations") or []

    async def get_inventory_item(self, sku: str) -> Optional[dict]:
        """getInventoryItem -> Item inkl. product.aspects (fuer den Live-Achsennamen-Abgleich).

        Massgeblich fuer Multivarianten-Reparaturen: die Aspekt-KEYS der Items sind die
        Achsennamen, gegen die eBay die Variationen matcht (siehe golive_service._live_axis_map).
        """
        return await self._get_json(f"{self._inv}/inventory_item/{sku}")

    async def get_inventory_item_group(self, group_key: str) -> Optional[dict]:
        """getInventoryItemGroup -> Gruppe inkl. variantSKUs (fuer Multivarianten-Updates)."""
        return await self._get_json(f"{self._inv}/inventory_item_group/{group_key}")

    async def update_inventory_item_group_fields(self, group_key: str, *,
                                                 title: Optional[str] = None,
                                                 description: Optional[str] = None) -> None:
        """Titel/Beschreibung einer Inventory-Item-Group aendern (read-merge-write).

        WICHTIG: Bei Multivarianten-Listings rendert eBay die BESCHREIBUNG DER GRUPPE –
        Aenderungen an Einzel-Items/Offers reichen dort nicht.
        """
        grp = await self.get_inventory_item_group(group_key)
        if not grp:
            raise PersistentError(f"Inventory-Item-Group {group_key} nicht gefunden")
        keep = ("title", "description", "subtitle", "imageUrls", "variantSKUs",
                "variesBy", "aspects", "videoIds")
        body = {k: v for k, v in grp.items() if k in keep and v is not None}
        if title is not None:
            body["title"] = title[:80]
        if description is not None:
            body["description"] = description
        await self._send(self._http().put, f"{self._inv}/inventory_item_group/{group_key}",
                         await self._auth_headers(content=True), body)

    async def update_offer_fields(self, offer_id: str, offer: dict, *,
                                  price_eur: Optional[float] = None,
                                  listing_description: Optional[str] = None) -> None:
        """updateOffer: Preis und/oder Beschreibung eines (aktiven) Offers aendern."""
        patch = self._sanitize_offer(offer)
        if price_eur is not None:
            patch["pricingSummary"] = {"price": {"value": f"{float(price_eur):.2f}",
                                                 "currency": self._currency}}
        if listing_description is not None:
            patch["listingDescription"] = listing_description[:500000]
        await self._send(self._http().put, f"{self._inv}/offer/{offer_id}",
                         await self._auth_headers(content=True), patch)

    async def update_inventory_item_fields(self, sku: str, *, title: Optional[str] = None,
                                           description: Optional[str] = None,
                                           aspects: Optional[dict] = None,
                                           image_urls: Optional[list] = None) -> None:
        """createOrReplaceInventoryItem (read-merge-write): Titel/Beschreibung/Merkmale/Bilder."""
        item = await self._get_json(f"{self._inv}/inventory_item/{sku}") or {}
        prod = item.setdefault("product", {})
        if title is not None:
            prod["title"] = title[:80]
        if description is not None:
            prod["description"] = description
        if aspects:
            merged = dict(prod.get("aspects") or {})
            merged.update({k: (v if isinstance(v, list) else [v]) for k, v in aspects.items()})
            prod["aspects"] = merged
        if image_urls is not None:
            prod["imageUrls"] = [u for u in image_urls if u][:12]
        # createOrReplace akzeptiert NUR diese Top-Level-Felder. Das rohe getInventoryItem-
        # Response echot zusaetzlich sku/locale/groupIds zurueck -> eBay 500 (25001,
        # "Interner Core Inventory Service-Fehler"). Darum minimalen, sauberen Body bauen
        # (wie create_inventory_item) und Bestand/Gewicht unveraendert uebernehmen.
        body: dict = {"condition": item.get("condition") or "NEW", "product": prod}
        if item.get("availability") is not None:
            body["availability"] = item["availability"]
        if item.get("packageWeightAndSize") is not None:
            body["packageWeightAndSize"] = item["packageWeightAndSize"]
        await self._send(self._http().put, f"{self._inv}/inventory_item/{sku}",
                         await self._auth_headers(content=True), body)

    # ------------------------------------------------------------------ Taxonomy (Kategorie)
    async def _get_app_token(self) -> str:
        """Application-Token (client_credentials) – fuer die Taxonomy-API noetig."""
        if getattr(self, "_app_token", None) and time.monotonic() < getattr(self, "_app_token_exp", 0):
            return self._app_token
        resp = await self._http().post(
            self._token_url,
            headers={"Authorization": self._basic_auth(),
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": _SCOPE_APP},
        )
        resp.raise_for_status()
        body = resp.json()
        self._app_token = body["access_token"]
        self._app_token_exp = time.monotonic() + int(body.get("expires_in") or 7200) - self._TOKEN_BUFFER_S
        return self._app_token

    async def _taxonomy_headers(self) -> dict:
        return {"Authorization": f"Bearer {await self._get_app_token()}", "Accept": "application/json"}

    async def _category_tree_id(self) -> str:
        if getattr(self, "_tree_id", None):
            return self._tree_id
        data = await self._get_json(
            f"{self._host}/commerce/taxonomy/v1/get_default_category_tree_id",
            params={"marketplace_id": self.settings.ebay_marketplace_id},
            headers=await self._taxonomy_headers(),
        )
        self._tree_id = (data or {}).get("categoryTreeId") or "77"
        return self._tree_id

    async def get_required_aspects(self, category_id: str) -> list[dict]:
        """getItemAspectsForCategory -> [{name, required, values[]}] (Pflicht-Merkmale)."""
        try:
            tree = await self._category_tree_id()
            data = await self._get_json(
                f"{self._host}/commerce/taxonomy/v1/category_tree/{tree}/get_item_aspects_for_category",
                params={"category_id": category_id},
                headers=await self._taxonomy_headers(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("aspect lookup failed", extra={"error": str(exc)})
            return []
        out = []
        for a in (data or {}).get("aspects") or []:
            con = a.get("aspectConstraint") or {}
            vals = [v.get("localizedValue") for v in (a.get("aspectValues") or [])
                    if v.get("localizedValue")]
            out.append({
                "name": a.get("localizedAspectName"),
                "required": bool(con.get("aspectRequired")),
                # Nur diese Merkmale darf ein Multivarianten-Listing als Achse
                # nutzen (sonst eBay 25002 "kein zulaessiges Variantenmerkmal").
                "variation": bool(con.get("aspectEnabledForVariations")),
                "values": vals,
            })
        return out

    async def build_aspects(self, category_id: str, base: dict | None = None) -> dict:
        """Fuellt fehlende Pflicht-Merkmale einer Kategorie (erster erlaubter Wert / 'Sonstige')."""
        aspects = {k: (v if isinstance(v, list) else [v]) for k, v in (base or {}).items()}
        for a in await self.get_required_aspects(category_id):
            if a["required"] and a["name"] and a["name"] not in aspects:
                aspects[a["name"]] = [a["values"][0] if a["values"] else "Sonstige"]
        return aspects

    async def suggest_categories(self, title: str, *, limit: int = 4) -> list[str]:
        """getCategorySuggestions -> Kategorie-IDs in Vorschlagsreihenfolge (fuer Fallbacks)."""
        try:
            tree = await self._category_tree_id()
            data = await self._get_json(
                f"{self._host}/commerce/taxonomy/v1/category_tree/{tree}/get_category_suggestions",
                params={"q": (title or "")[:100]},
                headers=await self._taxonomy_headers(),
            )
            out = []
            for s in (data or {}).get("categorySuggestions") or []:
                cid = (s.get("category") or {}).get("categoryId")
                if cid and cid not in out:
                    out.append(cid)
                if len(out) >= limit:
                    break
            return out
        except Exception as exc:  # noqa: BLE001 – Kategorie-Vorschlag darf nicht hart brechen
            logger.warning("category suggestion failed", extra={"error": str(exc)})
            return []

    async def suggest_category(self, title: str) -> Optional[str]:
        """Beste Kategorie-ID zum Titel (erster Vorschlag), sonst None."""
        cats = await self.suggest_categories(title, limit=1)
        return cats[0] if cats else None

    async def suggest_categories_mit_namen(self, title: str, *, limit: int = 6) -> list[dict]:
        """Wie suggest_categories, aber mit Klartext: [{id, name, pfad}].

        Warum eine zweite Methode statt einer Erweiterung: suggest_categories gibt
        eine reine ID-Liste zurueck und wird an mehreren Stellen so verwendet
        (golive-Kandidatenkette, native_listing). Deren Form zu aendern hiesse,
        alle Aufrufer anzufassen fuer etwas, das nur die Oberflaeche braucht.

        eBay liefert im selben Aufruf schon den Namen und die Ahnenkette mit -
        ``suggest_categories`` wirft beides bloss weg. Fuer eine Auswahlliste ist
        genau das noetig: "Herren-T-Shirts" laesst sich beurteilen, "15687" nicht.
        Der Pfad entscheidet die Faelle, in denen derselbe Name zweimal vorkommt.
        """
        try:
            tree = await self._category_tree_id()
            data = await self._get_json(
                f"{self._host}/commerce/taxonomy/v1/category_tree/{tree}/get_category_suggestions",
                params={"q": (title or "")[:100]},
                headers=await self._taxonomy_headers(),
            )
        except Exception as exc:  # noqa: BLE001 – ein Vorschlag darf nie hart brechen
            logger.warning("category suggestion (named) failed", extra={"error": str(exc)})
            return []

        aus: list[dict] = []
        gesehen: set[str] = set()
        for s in (data or {}).get("categorySuggestions") or []:
            kat = s.get("category") or {}
            kid = kat.get("categoryId")
            if not kid or kid in gesehen:
                continue
            gesehen.add(kid)
            # Die Ahnenkette kommt von unten nach oben - umgedreht liest sie sich
            # wie ein Pfad. Fehlt sie, bleibt der Pfad leer statt geraten.
            ahnen = [a.get("categoryName") for a in
                     reversed(s.get("categoryTreeNodeAncestors") or [])
                     if a.get("categoryName")]
            aus.append({
                "id": kid,
                "name": kat.get("categoryName") or "",
                "pfad": " > ".join(ahnen),
            })
            if len(aus) >= limit:
                break
        return aus

    @staticmethod
    def _location_address(postal_code: str, country: str, city: Optional[str],
                          state: Optional[str], address_line: Optional[str] = None) -> dict:
        """Vollstaendiger Adress-Body fuer eine WAREHOUSE-Location (Felder optional)."""
        address = {"postalCode": postal_code, "country": country}
        if address_line:
            address["addressLine1"] = address_line
        if city:
            address["city"] = city
        if state:
            address["stateOrProvince"] = state
        return address

    async def ensure_merchant_location(self, *, key: str, postal_code: str,
                                       country: str, name: str,
                                       city: Optional[str] = None,
                                       state: Optional[str] = None,
                                       address_line: Optional[str] = None) -> str:
        """Stellt sicher, dass eine WAREHOUSE-Location `key` mit KORREKTER Adresse existiert.

        Existiert sie bereits, aber mit abweichender PLZ/Stadt/Strasse, wird die Adresse
        aktualisiert – so folgt die 'Versand aus'-Angabe immer der Konfiguration.
        """
        address = self._location_address(postal_code, country, city, state, address_line)
        existing = await self.get_inventory_locations()
        match = next((l for l in existing if l.get("merchantLocationKey") == key), None)
        if match is not None:
            cur = (match.get("location") or {}).get("address") or {}
            needs = (cur.get("postalCode") or "") != postal_code or (
                bool(city) and (cur.get("city") or "") != city) or (
                bool(address_line) and (cur.get("addressLine1") or "") != address_line)
            if needs:
                await self.update_inventory_location(
                    key, postal_code=postal_code, country=country, city=city,
                    state=state, address_line=address_line)
            return key
        body = {
            "location": {"address": address},
            "locationTypes": ["WAREHOUSE"],
            "name": name,
            "merchantLocationStatus": "ENABLED",
        }
        headers = await self._auth_headers(content=True)
        try:
            resp = await self._http().post(f"{self._inv}/location/{key}",
                                           headers=headers, json=body)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        return key

    async def update_inventory_location(self, key: str, *, postal_code: str,
                                        country: str, city: Optional[str] = None,
                                        state: Optional[str] = None,
                                        address_line: Optional[str] = None) -> None:
        """updateInventoryLocation: Adresse einer bestehenden WAREHOUSE-Location aendern.

        POST /location/{key}/update_location_details (voll-ersetzend, Erfolg = 204).
        Bei WAREHOUSE sind alle Adressfelder inkl. Land aenderbar.
        """
        body = {"location": {"address": self._location_address(
            postal_code, country, city, state, address_line)}}
        headers = await self._auth_headers(content=True)
        try:
            resp = await self._http().post(
                f"{self._inv}/location/{key}/update_location_details",
                headers=headers, json=body)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc

    async def refresh_listing_location(self, sku: str, *,
                                       merchant_location_key: Optional[str] = None) -> str:
        """Zwingt ein AKTIVES Listing, die (geaenderte) Ship-from-Location neu zu rendern.

        Ein Adress-Update der Location propagiert nicht automatisch auf laufende
        Angebote – daher das Offer der SKU per updateOffer (voll-ersetzend) neu
        speichern. Gibt die offerId zurueck.
        """
        offer = await self._first_offer_for_sku(sku)
        if offer is None:
            raise PersistentError(f"Kein Offer fuer SKU {sku} gefunden")
        offer_id = offer["offerId"]
        patch = self._sanitize_offer(offer)
        patch["merchantLocationKey"] = merchant_location_key or self.settings.ebay_merchant_location_key
        await self._send(self._http().put, f"{self._inv}/offer/{offer_id}",
                         await self._auth_headers(content=True), patch)
        return offer_id

    # ------------------------------------------------------------------ HTTP-Helfer
    async def _get_json(self, url: str, *, params: dict | None = None,
                        headers: dict | None = None) -> Optional[dict]:
        try:
            resp = await self._http().get(url, params=params,
                                          headers=headers or await self._auth_headers())
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        return resp.json() if resp.content else {}

    async def _send(self, method, url: str, headers: dict, body: dict) -> None:
        try:
            resp = await method(url, headers=headers, json=body)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc

    # ------------------------------------------------------------------ Notification-Signatur
    def verify_ipn_signature(self, raw_body: bytes, signature: str) -> bool:
        """Verifiziert die MODERNE eBay-Notification-Signatur (ECDSA/SHA1).

        Header `x-ebay-signature` = base64(JSON{alg,kid,signature,digest}); der Public Key
        kommt aus getPublicKey({kid}) und wird ~1h gecacht. Verifiziert wird gegen den
        ROHEN Request-Body (nicht re-serialisieren).
        """
        decoded = self._decode_signature_header(signature)
        if decoded is None:
            return False
        kid, der_sig = decoded
        pem = self._fetch_public_key_pem(kid)
        if not pem:
            return False
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.hazmat.primitives.serialization import load_pem_public_key

            public_key = load_pem_public_key(pem.encode())
            public_key.verify(der_sig, raw_body, ec.ECDSA(hashes.SHA1()))
            return True
        except InvalidSignature:
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("signature verify error", extra={"error": str(exc)})
            return False

    @staticmethod
    def _decode_signature_header(signature: str) -> Optional[tuple[str, bytes]]:
        """base64(JSON) -> (kid, DER-Signatur-Bytes). None bei Fehlformat."""
        if not signature:
            return None
        try:
            meta = json.loads(base64.b64decode(signature, validate=True))
            kid = meta["kid"]
            der_sig = base64.b64decode(meta["signature"], validate=True)
            return kid, der_sig
        except (ValueError, KeyError, TypeError):  # binascii.Error erbt von ValueError
            return None

    def _fetch_public_key_pem(self, kid: str) -> Optional[str]:
        """getPublicKey({kid}) (App-Token, client_credentials) mit ~1h-Cache. Synchron."""
        cached = self._pubkey_cache.get(kid)
        if cached and time.monotonic() < cached[1]:
            return cached[0]
        try:
            with httpx.Client(timeout=5.0) as client:
                tok = client.post(
                    self._token_url,
                    headers={"Authorization": self._basic_auth(),
                             "Content-Type": "application/x-www-form-urlencoded"},
                    data={"grant_type": "client_credentials", "scope": _SCOPE_APP},
                )
                tok.raise_for_status()
                app_token = tok.json()["access_token"]
                resp = client.get(
                    f"{self._host}/commerce/notification/v1/public_key/{kid}",
                    headers={"Authorization": f"Bearer {app_token}", "Accept": "application/json"},
                )
                resp.raise_for_status()
                pem = resp.json().get("key")
        except Exception as exc:  # noqa: BLE001
            logger.warning("getPublicKey failed", extra={"kid": kid, "error": str(exc)})
            return None
        if pem:
            self._pubkey_cache[kid] = (pem, time.monotonic() + 3600)
        return pem

    @staticmethod
    def account_deletion_challenge_response(
        challenge_code: str, verification_token: str, endpoint_url: str
    ) -> str:
        """SHA-256(challengeCode + verificationToken + endpointURL) als HEX.

        Pflicht-Endpoint fuer die Marketplace-Account-Deletion-Validierung (Reihenfolge exakt).
        """
        h = hashlib.sha256()
        h.update(challenge_code.encode())
        h.update(verification_token.encode())
        h.update(endpoint_url.encode())
        return h.hexdigest()
