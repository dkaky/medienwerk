"""AliExpress-Integration (Spec Kap. 4.3).

Interface + Mock + echter Client gegen die **offizielle AliExpress Open-Platform /
Dropshipping-API** (signierte Requests, Methoden ``aliexpress.ds.*``). Das Protokoll
(Signatur HMAC-SHA256/MD5, Request, Parser) liegt in ``aliexpress_api.py`` und ist
ohne Netz getestet. Voraussetzung fuer den Echtbetrieb: ALIEXPRESS_APP_KEY/SECRET
(Open-Platform-/Dropshipping-Zugang). Produktdaten (scrape_product) sind voll
implementiert; Bestellung/Tracking rufen die ds.*-Methoden und sollten mit echten
Credentials final verifiziert werden (geldwirksame Bestellung!).
"""
from __future__ import annotations

import abc
import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import httpx

from app.retry import PersistentError, TransientError

from . import aliexpress_api as api

logger = logging.getLogger("app.integrations.aliexpress")


class ProductNotFoundError(Exception):
    """E_NOTFOUND – Produkt nicht mehr verfuegbar (Bildsuche triggern)."""


class OutOfStockError(Exception):
    """E_OOS – ausverkauft."""


class OrderRejectedError(PersistentError):
    """ds.order.create hat die Bestellung ABGELEHNT (is_success=false / 4xx-Validierung).

    Bedeutung fuer den Geld-Pfad: es wurde SICHER keine Bestellung angelegt ->
    der Claim darf gefahrlos zurueckgerollt und spaeter erneut versucht werden.
    """


class OrderUncertainError(Exception):
    """Ausgang der Bestellung UNKLAR (Timeout/5xx/Netzfehler/Parse-Fehler nach dem Send).

    Die Order KANN bei AliExpress bereits existieren -> NIEMALS automatisch erneut
    senden und den Claim NICHT loeschen. Nutzer muss das AliExpress-Konto pruefen.
    """


@dataclass
class ScrapedProduct:
    aliexpress_id: str
    title_raw: str
    description_raw: str
    price_cny: Decimal
    images: list[str]
    variants: dict
    supplier_id: str
    supplier_rating: Decimal
    in_stock: bool = True   # Bestands-Signal fuers Monitoring/Repricing
    # Hat AliExpress WIRKLICH Bestandszahlen geliefert? False = degradierte Antwort
    # (in_stock ist dann fail-open geraten) -> Monitoring darf Mengen nicht ERHOEHEN.
    stock_reported: bool = True
    specs: list = field(default_factory=list)   # [{name, value}] Produktmerkmale


@dataclass
class PlacedOrder:
    aliexpress_order_id: str
    cost_cny: Decimal
    status: str = "ordered"


@dataclass
class TrackingInfo:
    tracking_number: Optional[str]
    carrier: Optional[str]
    status: str
    estimated_delivery: Optional[datetime] = None


class AliExpressClient(abc.ABC):
    @abc.abstractmethod
    async def scrape_product(self, url: str) -> ScrapedProduct:
        ...

    @abc.abstractmethod
    async def place_order(self, *, url: str, variant: dict | None, quantity: int,
                          delivery_name: str, delivery_address: dict) -> PlacedOrder:
        ...

    async def place_order_multi(self, *, items: list[dict], delivery_name: str,
                                delivery_address: dict) -> list[dict]:
        """Mehrere Positionen EINER Lieferadresse bestellen.

        ``items``: [{url, variant, quantity}]. Rueckgabe je AliExpress-Order:
        [{aliexpress_order_id, cost_cny, item_indexes: [Index in items]}].
        Default-Implementierung (Mock/Fallback): eine Einzelbestellung je Item —
        deterministisch, 1:1-Zuordnung. Der echte Client buendelt in EINEM
        ds.order.create-Call (gleicher Haendler -> eine Order).
        """
        out = []
        for i, it in enumerate(items):
            placed = await self.place_order(
                url=it["url"], variant=it.get("variant"),
                quantity=it.get("quantity") or 1,
                delivery_name=delivery_name, delivery_address=delivery_address)
            out.append({"aliexpress_order_id": placed.aliexpress_order_id,
                        "cost_cny": placed.cost_cny, "item_indexes": [i]})
        return out

    @abc.abstractmethod
    async def get_tracking(self, aliexpress_order_id: str) -> TrackingInfo:
        ...

    @abc.abstractmethod
    async def find_alternative(self, image_url: str) -> list[str]:
        """Bildsuche nach Alternativprodukt (Spec Kap. 4.4 / 5.2)."""

    async def query_freight(self, *, product_id: str, sku_id: str | None = None,
                            country: str | None = None, quantity: int = 1) -> dict | None:
        """Echte Lieferanten-Versandkosten (freight query) fuer ein Produkt.

        Rueckgabe ``{fee_eur, free_shipping, carrier, delivery_days, tracking}`` der
        guenstigsten trackbaren Option oder ``None`` (unbekannt). Default: ``None`` –
        die Preiskalkulation faellt dann auf die Pauschal-Schaetzung zurueck.
        """
        return None

    async def delivery_availability(self, *, product_id: str, country: str,
                                    sku_id: str | None = None) -> bool | None:
        """Liefert der Haendler dieses Produkt ins Land? True/False = DEFINITIV,
        None = unbekannt (Fehler/Mock). FAIL-OPEN gehoert dem Aufrufer: None darf
        nie warnen oder eine Bestellung blockieren. Default (Mock): unbekannt."""
        return None


class MockAliExpressClient(AliExpressClient):
    @staticmethod
    def _seed(value: str) -> int:
        return int(hashlib.sha256(value.encode()).hexdigest(), 16)

    async def scrape_product(self, url: str) -> ScrapedProduct:
        seed = self._seed(url)
        ae_id = str(1_000_000_000 + seed % 9_000_000_000)
        return ScrapedProduct(
            aliexpress_id=ae_id,
            title_raw="2024 New Fashion Wireless Bluetooth Headphones 中国直邮 Fast Shipping",
            description_raw=(
                "High quality product. 中国发货 Delivery 15-30 days. "
                "Cheap price from China factory. 包邮"
            ),
            price_cny=Decimal(str(round(5 + seed % 200 + 0.99, 2))),
            images=[f"https://img.example/{ae_id}_{i}.jpg" for i in range(3)],
            variants={"color": ["black", "white"], "size": ["M", "L"]},
            supplier_id=f"sup_{seed % 100000}",
            supplier_rating=Decimal("4.7"),
            in_stock="oos" not in url,   # Mock: URL-Marker simuliert Ausverkauf
        )

    async def place_order(self, *, url, variant, quantity, delivery_name, delivery_address):
        seed = self._seed(url + delivery_name)
        # Mock-Fehlerfaelle, damit Error-Handling testbar ist:
        if "notfound" in url:
            raise ProductNotFoundError(url)
        if "oos" in url:
            raise OutOfStockError(url)
        return PlacedOrder(
            aliexpress_order_id=str(8_000_000_000 + seed % 1_000_000_000),
            cost_cny=Decimal(str(round(5 + seed % 200 + 0.5, 2))),
        )

    async def get_tracking(self, aliexpress_order_id: str) -> TrackingInfo:
        return TrackingInfo(
            tracking_number=f"1Z999AA{self._seed(aliexpress_order_id) % 10_000_000_000:010d}",
            carrier="DHL",
            status="in_transit",
            estimated_delivery=datetime.now(timezone.utc) + timedelta(days=7),
        )

    async def find_alternative(self, image_url: str) -> list[str]:
        h = hashlib.sha256(image_url.encode()).hexdigest()[:8]
        return [f"https://de.aliexpress.com/item/alt_{h}_{i}.html" for i in range(2)]


class RealAliExpressClient(AliExpressClient):
    """Echter Client gegen die offizielle AliExpress Open-Platform / Dropshipping-API.

    Produktdaten (scrape_product) ueber ``aliexpress.ds.product.get`` sind voll
    implementiert und getestet (Signatur + Parser). Bestellung/Tracking/Bildsuche
    rufen die passenden ds.*-Methoden auf; ihre Parameter-/Response-Schemata sind
    je nach API-Version leicht unterschiedlich und sollten mit echten Credentials
    im API-Explorer verifiziert werden (vor allem die geldwirksame Bestellung).
    """

    _TOKEN_BUFFER_S = 300  # access_token vorausschauend 5 min vor Ablauf erneuern

    def __init__(self, settings) -> None:
        self.settings = settings
        self._client: Optional[httpx.AsyncClient] = None
        self._token_lock = asyncio.Lock()
        # Token + Ablauf: aus dem persistenten Store laden, sonst aus den Settings (.env).
        store = api.load_token_store(settings.aliexpress_token_file) or {}
        self._access_token: Optional[str] = (
            store.get("access_token") or settings.aliexpress_access_token or None
        )
        self._refresh_token: Optional[str] = (
            store.get("refresh_token") or settings.aliexpress_refresh_token or None
        )
        # 0 = Ablauf unbekannt (z.B. nur .env-Token) -> nicht proaktiv erneuern, nur reaktiv.
        self._token_expiry: float = float(store.get("expires_at") or 0.0)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    def _require_keys(self) -> None:
        if not (self.settings.aliexpress_app_key and self.settings.aliexpress_app_secret):
            raise PersistentError(
                "ALIEXPRESS_APP_KEY/SECRET fehlen – Open-Platform-API nicht konfiguriert."
            )

    # ------------------------------------------------------------------ Token
    async def _refresh_access_token(self, *, force: bool = False) -> None:
        """access_token via refresh_token erneuern (serialisiert, mit Persistenz)."""
        async with self._token_lock:
            # Double-Check: evtl. hat eine andere Coroutine schon erneuert.
            if not force and self._access_token and (
                self._token_expiry == 0.0 or time.time() < self._token_expiry - self._TOKEN_BUFFER_S
            ):
                return
            if not self._refresh_token:
                raise PersistentError(
                    "AliExpress access_token abgelaufen und kein refresh_token – "
                    "neu autorisieren (scripts.aliexpress_oauth)."
                )
            s = self.settings
            data = await api.call(
                self._http(), base=s.aliexpress_api_base, method="/auth/token/refresh",
                business={"refresh_token": self._refresh_token},
                app_key=s.aliexpress_app_key, app_secret=s.aliexpress_app_secret,
                sign_method=s.aliexpress_sign_method,
            )
            tok = api.parse_token(data)
            if not tok["access_token"]:
                raise PersistentError(f"Token-Refresh ohne access_token: {str(data)[:200]}")
            self._access_token = tok["access_token"]
            if tok["refresh_token"]:
                self._refresh_token = tok["refresh_token"]
            self._token_expiry = time.time() + int(tok["expires_in"] or 86400)
            api.save_token_store(
                s.aliexpress_token_file, access_token=self._access_token,
                refresh_token=self._refresh_token, expires_in=tok["expires_in"] or 86400,
            )
            logger.info("aliexpress access_token erneuert")

    async def _ensure_token(self) -> None:
        if self._access_token and (
            self._token_expiry == 0.0 or time.time() < self._token_expiry - self._TOKEN_BUFFER_S
        ):
            return
        await self._refresh_access_token()

    async def _call(self, method: str, business: dict) -> dict:
        self._require_keys()
        s = self.settings
        is_auth = method.startswith("/auth/")
        if not is_auth:
            # Token-Beschaffung VOR dem eigentlichen Call: schlaegt sie fehl, wurde die Anfrage
            # NIE gesendet -> PersistentError (= sicher nicht ausgefuehrt). Ohne diese Umwandlung
            # landete ein abgelaufener Refresh-Token als "Ausgang unklar" beim Besteller und
            # blockierte den Verkauf unnoetig (Vorfall 01.08., Sales 1227/1228).
            try:
                await self._ensure_token()
            except (PersistentError, TransientError):
                raise
            except Exception as exc:  # noqa: BLE001 – Auth-Fehler = Anfrage nicht gesendet
                raise PersistentError(
                    f"AliExpress-Autorisierung fehlgeschlagen (neu autorisieren): {exc}") from exc
        try:
            return await self._raw_call(method, business, is_auth)
        except api.AliExpressApiError as exc:
            # Abgelaufener/ungueltiger Token -> einmal erneuern und erneut versuchen.
            if not is_auth and self._refresh_token and api.is_token_error(str(exc)):
                logger.info("aliexpress token-Fehler -> refresh + retry")
                # Dieselbe Umwandlung wie oben im Vorab-Pfad, und aus demselben Grund.
                # Ohne sie verliesse eine Ausnahme aus _refresh_access_token diesen
                # except-Zweig ROH - Geschwister-except-Zweige desselben try fangen
                # nichts mehr, was in einem von ihnen geworfen wurde. In _order_create
                # griffe dann nicht "AliExpress-Autorisierung abgelaufen ... Es wurde
                # NICHTS bestellt", sondern der generische Zweig mit "Ausgang unklar":
                # Verkauf blockiert, Handpruefung im AliExpress-Konto verlangt - obwohl
                # der erste Versuch nachweislich am Token abgelehnt wurde und damit
                # sicher nichts ausgeloest hat.
                #
                # Nicht nur api.AliExpressApiError faellt hier an: ein 401 auf dem
                # Token-Endpunkt kommt als httpx.HTTPStatusError. Deshalb breit fangen.
                try:
                    await self._refresh_access_token(force=True)
                except (PersistentError, TransientError):
                    raise
                except Exception as auth_exc:  # noqa: BLE001 – Erneuerung gescheitert
                    raise PersistentError(
                        f"AliExpress-Autorisierung fehlgeschlagen (neu autorisieren): "
                        f"{auth_exc}") from auth_exc
                return await self._raw_call(method, business, is_auth)
            raise PersistentError(f"AliExpress-API-Fehler: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code >= 500 or code == 429:
                raise TransientError(f"AliExpress {code}") from exc
            raise PersistentError(f"AliExpress {code}: {exc.response.text[:200]}") from exc
        except httpx.RequestError as exc:
            raise TransientError(str(exc)) from exc

    async def _raw_call(self, method: str, business: dict, is_auth: bool) -> dict:
        s = self.settings
        if not is_auth and self._access_token:
            business = {**business, "access_token": self._access_token}
        return await api.call(
            self._http(), base=s.aliexpress_api_base, method=method, business=business,
            app_key=s.aliexpress_app_key, app_secret=s.aliexpress_app_secret,
            sign_method=s.aliexpress_sign_method,
        )

    async def scrape_product(self, url: str) -> ScrapedProduct:
        pid = api.extract_product_id(url)
        if not pid:
            raise ProductNotFoundError(f"Keine AliExpress-Produkt-ID in URL: {url}")
        s = self.settings
        data = await self._call("aliexpress.ds.product.get", {
            "product_id": pid,
            "ship_to_country": s.aliexpress_ship_to,
            "target_currency": s.aliexpress_target_currency,
            "target_language": s.aliexpress_target_language,
        })
        p = api.parse_product(data)
        if not p["aliexpress_id"]:
            # NICHT pauschal „geloescht" behaupten – den echten Grund aus der API-Antwort zeigen
            # (DS-Katalog/Lieferland/gedrosselt), damit klar ist, ob das Produkt wirklich weg ist.
            raise ProductNotFoundError(api.explain_empty_product(pid, data))
        return ScrapedProduct(
            aliexpress_id=p["aliexpress_id"],
            title_raw=p["title_raw"],
            description_raw=p["description_raw"],
            price_cny=p["price_cny"],
            images=p["images"],
            variants=p["variants"],
            supplier_id=p["supplier_id"],
            supplier_rating=p["supplier_rating"],
            in_stock=p["in_stock"],
            stock_reported=p.get("stock_reported", True),
            specs=p.get("specs", []),
        )

    async def query_freight(self, *, product_id, sku_id=None, country=None, quantity=1):
        s = self.settings
        ctry = (country or s.aliexpress_ship_to or "DE").upper()
        lang = (s.aliexpress_target_language or "de").lower()
        try:
            # selectedSkuId ist Pflicht. Fehlt sie, holen wir eine (erste SKU des Produkts).
            if not sku_id:
                pdata = await self._call("aliexpress.ds.product.get", {
                    "product_id": str(product_id), "ship_to_country": ctry,
                    "target_currency": s.aliexpress_target_currency,
                    "target_language": s.aliexpress_target_language})
                skus = api._sku_list(api._unwrap_result(pdata))
                sku_id = skus[0].get("sku_id") if skus else None
            if not sku_id:
                return None
            req = {
                "productId": str(product_id),
                "quantity": max(1, int(quantity or 1)),
                "shipToCountry": ctry,
                "selectedSkuId": str(sku_id),
                "language": f"{lang}_{ctry}",
                "locale": f"{lang}_{ctry}",
                "currency": s.aliexpress_target_currency or "EUR",
            }
            data = await self._call("aliexpress.ds.freight.query",
                                    {"queryDeliveryReq": json.dumps(req)})
        except Exception as exc:  # noqa: BLE001 – Versand unbekannt -> Kalkulation nutzt Pauschale
            logger.warning("freight query (%s) failed: %s", product_id, str(exc)[:150])
            return None
        return api.parse_freight(data)

    async def delivery_availability(self, *, product_id: str, country: str,
                                    sku_id: str | None = None) -> bool | None:
        """Definitive Liefer-Aussage fuer den Liefer-Check (16.08.).

        Im Gegensatz zu query_freight (None = "egal warum unbekannt") wird hier
        unterschieden: leerer Options-Container = liefert NICHT (False), Abfrage-
        Fehler/degradierte Antwort = unbekannt (None, fail-open beim Aufrufer).
        Der Rohgrund wird geloggt, damit unbekannte Antwortformen live nachgezogen
        werden koennen."""
        s = self.settings
        ctry = (country or "DE").upper()
        lang = (s.aliexpress_target_language or "de").lower()
        try:
            if not sku_id:
                pdata = await self._call("aliexpress.ds.product.get", {
                    "product_id": str(product_id), "ship_to_country": ctry,
                    "target_currency": s.aliexpress_target_currency,
                    "target_language": s.aliexpress_target_language})
                skus = api._sku_list(api._unwrap_result(pdata))
                sku_id = skus[0].get("sku_id") if skus else None
            if not sku_id:
                return None
            req = {
                "productId": str(product_id),
                "quantity": 1,
                "shipToCountry": ctry,
                "selectedSkuId": str(sku_id),
                "language": f"{lang}_{ctry}",
                "locale": f"{lang}_{ctry}",
                "currency": s.aliexpress_target_currency or "EUR",
            }
            data = await self._call("aliexpress.ds.freight.query",
                                    {"queryDeliveryReq": json.dumps(req)})
        except Exception as exc:  # noqa: BLE001 – unbekannt, NIE als Nein werten
            logger.info("delivery availability %s->%s unbekannt: %s",
                        product_id, ctry, str(exc)[:150])
            return None
        verdict = api.freight_deliverable(data)
        if verdict is None:
            logger.info("delivery availability %s->%s ohne Options-Feld (degradiert)",
                        product_id, ctry)
        return verdict

    # PLZ -> Bundesland (Oesterreich). AliExpress verlangt bei AT ein GUELTIGES Bundesland
    # ("Please select a State/Province/County", Vorfall Sale 1123) – eBay-Adressen tragen
    # bei AT oft keins. Ableitung aus der PLZ-Leitzone; Grenzfaelle sind unkritisch, weil
    # der Zusteller nach PLZ+Ort routet (das Bundesland dient nur der Validierung).
    # Je Zone (deutscher Name, englischer Name) – der englische ist der Retry-Fallback,
    # falls AliExpress die deutsche Schreibweise ablehnt.
    _AT_PLZ_BUNDESLAND = {
        "1": ("Wien", "Vienna"),
        "2": ("Niederösterreich", "Lower Austria"),
        "3": ("Niederösterreich", "Lower Austria"),
        "4": ("Oberösterreich", "Upper Austria"),
        "5": ("Salzburg", "Salzburg"),
        "6": ("Tirol", "Tyrol"),           # 67xx-69xx -> Vorarlberg (siehe _derive_province)
        "7": ("Burgenland", "Burgenland"),
        "8": ("Steiermark", "Styria"),
        "9": ("Kärnten", "Carinthia"),     # 99xx (Osttirol) -> Tirol
    }
    # Telefon-Laendervorwahl je Zielland (Default war stur '49' – falsch fuer AT/CH).
    _PHONE_COUNTRY = {"DE": "49", "AT": "43", "CH": "41", "NL": "31", "BE": "32",
                      "FR": "33", "IT": "39", "PL": "48", "LU": "352",
                      # weitere eBay.de-Zielländer (Fall 1144: LT bekam falsch '49')
                      "LT": "370", "LV": "371", "EE": "372", "CZ": "420", "SK": "421",
                      "HU": "36", "SI": "386", "HR": "385", "RO": "40", "BG": "359",
                      "ES": "34", "PT": "351", "GR": "30", "IE": "353", "DK": "45",
                      "SE": "46", "FI": "358"}
    # Womit Mobilnummern NACH der Vorwahl beginnen – Guard gegen falsches Vorwahl-Strippen
    # bei Festnetz-/Ortsnummern, die zufaellig mit den Vorwahl-Ziffern anfangen
    # (z.B. AT-Festnetz '4352 12345': '43' ist hier ORTSVORWAHL, nicht Laendercode).
    _MOBILE_START = {"49": ("1",), "43": ("6",), "41": ("7",), "31": ("6",),
                     "32": ("4",), "33": ("6", "7"), "39": ("3",),
                     "48": ("5", "6", "7", "8"), "352": ("6",)}

    # PLZ-Zone -> Landeshauptstadt (AT): letzte Rettung, wenn AliExpress den Ort nicht
    # kennt ("Please enter a City", Sale 1123/1256). Das Dorf wandert in die Strassen-
    # zeile; die Post routet ohnehin nach PLZ+Strasse, die Stadt dient der Validierung.
    _AT_PLZ_HAUPTSTADT = {"1": "Wien", "2": "St. Pölten", "3": "St. Pölten",
                          "4": "Linz", "5": "Salzburg", "6": "Innsbruck",
                          "7": "Eisenstadt", "8": "Graz", "9": "Klagenfurt"}
    # Deutsche Staedtenamen, die die AliExpress-Validierung nur ENGLISCH kennt
    # (Sale 1256: 'Wien'/PLZ 1030 wurde mit "Please enter a City" abgelehnt —
    # die AT-Staedteliste von AliExpress ist englisch -> 'Vienna'). Erweiterbar.
    @staticmethod
    def _de_ascii(text: str) -> str:
        """Deutsche Umlaute ASCII-transliterieren ('Fügen' -> 'Fuegen', Sale 1274)."""
        return (text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
                .replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue")
                .replace("ß", "ss"))

    # AliExpress-Divisionsliste AT (Recherche 12.08., Quelle: ALD-Plugin
    # AT-states.json): province MUSS einer von 9 ENGLISCHEN Bundeslandnamen
    # (oder 'Other') sein — deutsche Namen stehen NICHT in der Liste.
    _AT_BL_EN = {
        "burgenland": "Burgenland",
        "kärnten": "Carinthia", "kaernten": "Carinthia", "carinthia": "Carinthia",
        "niederösterreich": "Lower Austria", "niederoesterreich": "Lower Austria",
        "lower austria": "Lower Austria",
        "oberösterreich": "Upper Austria", "oberoesterreich": "Upper Austria",
        "upper austria": "Upper Austria",
        "salzburg": "Salzburg",
        "steiermark": "Styria", "styria": "Styria",
        "tirol": "Tyrol", "tyrol": "Tyrol",
        "vorarlberg": "Vorarlberg",
        "wien": "Vienna", "vienna": "Vienna",
    }

    _CITY_EN = {"wien": "Vienna", "zürich": "Zurich", "genf": "Geneva",
                "köln": "Cologne", "münchen": "Munich", "nürnberg": "Nuremberg"}
    # Italien (Sale 1266): eBay liefert den 2-Buchstaben-PROVINZCODE ('GE'), die
    # AliExpress-Provinzliste erwartet den vollen Namen ('Genova') -> Ablehnung
    # "Please select a State/Province/County". Offizielle Provinzcodes.
    _IT_PROVINCES = {
        "AG": "Agrigento", "AL": "Alessandria", "AN": "Ancona", "AO": "Aosta",
        "AP": "Ascoli Piceno", "AQ": "L'Aquila", "AR": "Arezzo", "AT": "Asti",
        "AV": "Avellino", "BA": "Bari", "BG": "Bergamo", "BI": "Biella",
        "BL": "Belluno", "BN": "Benevento", "BO": "Bologna", "BR": "Brindisi",
        "BS": "Brescia", "BT": "Barletta-Andria-Trani", "BZ": "Bolzano",
        "CA": "Cagliari", "CB": "Campobasso", "CE": "Caserta", "CH": "Chieti",
        "CL": "Caltanissetta", "CN": "Cuneo", "CO": "Como", "CR": "Cremona",
        "CS": "Cosenza", "CT": "Catania", "CZ": "Catanzaro", "EN": "Enna",
        "FC": "Forlì-Cesena", "FE": "Ferrara", "FG": "Foggia", "FI": "Firenze",
        "FM": "Fermo", "FR": "Frosinone", "GE": "Genova", "GO": "Gorizia",
        "GR": "Grosseto", "IM": "Imperia", "IS": "Isernia", "KR": "Crotone",
        "LC": "Lecco", "LE": "Lecce", "LI": "Livorno", "LO": "Lodi",
        "LT": "Latina", "LU": "Lucca", "MB": "Monza e Brianza", "MC": "Macerata",
        "ME": "Messina", "MI": "Milano", "MN": "Mantova", "MO": "Modena",
        "MS": "Massa-Carrara", "MT": "Matera", "NA": "Napoli", "NO": "Novara",
        "NU": "Nuoro", "OR": "Oristano", "PA": "Palermo", "PC": "Piacenza",
        "PD": "Padova", "PE": "Pescara", "PG": "Perugia", "PI": "Pisa",
        "PN": "Pordenone", "PO": "Prato", "PR": "Parma", "PT": "Pistoia",
        "PU": "Pesaro e Urbino", "PV": "Pavia", "PZ": "Potenza", "RA": "Ravenna",
        "RC": "Reggio Calabria", "RE": "Reggio Emilia", "RG": "Ragusa",
        "RI": "Rieti", "RM": "Roma", "RN": "Rimini", "RO": "Rovigo",
        "SA": "Salerno", "SI": "Siena", "SO": "Sondrio", "SP": "La Spezia",
        "SR": "Siracusa", "SS": "Sassari", "SU": "Sud Sardegna", "SV": "Savona",
        "TA": "Taranto", "TE": "Teramo", "TN": "Trento", "TO": "Torino",
        "TP": "Trapani", "TR": "Terni", "TS": "Trieste", "TV": "Treviso",
        "UD": "Udine", "VA": "Varese", "VB": "Verbano-Cusio-Ossola",
        "VC": "Vercelli", "VE": "Venezia", "VI": "Vicenza", "VR": "Verona",
        "VT": "Viterbo", "VV": "Vibo Valentia",
    }
    # Provinzcode -> REGION (italienisch, englisch): Der Live-Test Sale 1266 zeigte,
    # dass AliExpress' Italien-Liste weder Provinzcodes NOCH Provinznamen kennt —
    # sie fuehrt die 20 REGIONEN (Genova -> 'Liguria').
    _IT_REGIONS = {
        **dict.fromkeys(("AL", "AT", "BI", "CN", "NO", "TO", "VB", "VC"),
                        ("Piemonte", "Piedmont")),
        "AO": ("Valle d'Aosta", "Aosta Valley"),
        **dict.fromkeys(("BG", "BS", "CO", "CR", "LC", "LO", "MB", "MI", "MN",
                         "PV", "SO", "VA"), ("Lombardia", "Lombardy")),
        **dict.fromkeys(("BZ", "TN"), ("Trentino-Alto Adige", "Trentino-Alto Adige")),
        **dict.fromkeys(("BL", "PD", "RO", "TV", "VE", "VR", "VI"),
                        ("Veneto", "Veneto")),
        **dict.fromkeys(("GO", "PN", "TS", "UD"),
                        ("Friuli-Venezia Giulia", "Friuli-Venezia Giulia")),
        **dict.fromkeys(("GE", "IM", "SP", "SV"), ("Liguria", "Liguria")),
        **dict.fromkeys(("BO", "FC", "FE", "MO", "PC", "PR", "RA", "RE", "RN"),
                        ("Emilia-Romagna", "Emilia-Romagna")),
        **dict.fromkeys(("AR", "FI", "GR", "LI", "LU", "MS", "PI", "PO", "PT", "SI"),
                        ("Toscana", "Tuscany")),
        **dict.fromkeys(("PG", "TR"), ("Umbria", "Umbria")),
        **dict.fromkeys(("AN", "AP", "FM", "MC", "PU"), ("Marche", "Marche")),
        **dict.fromkeys(("FR", "LT", "RI", "RM", "VT"), ("Lazio", "Lazio")),
        **dict.fromkeys(("AQ", "CH", "PE", "TE"), ("Abruzzo", "Abruzzo")),
        **dict.fromkeys(("CB", "IS"), ("Molise", "Molise")),
        **dict.fromkeys(("AV", "BN", "CE", "NA", "SA"), ("Campania", "Campania")),
        **dict.fromkeys(("BA", "BR", "BT", "FG", "LE", "TA"), ("Puglia", "Apulia")),
        **dict.fromkeys(("MT", "PZ"), ("Basilicata", "Basilicata")),
        **dict.fromkeys(("CS", "CZ", "KR", "RC", "VV"), ("Calabria", "Calabria")),
        **dict.fromkeys(("AG", "CL", "CT", "EN", "ME", "PA", "RG", "SR", "TP"),
                        ("Sicilia", "Sicily")),
        **dict.fromkeys(("CA", "NU", "OR", "SS", "SU"), ("Sardegna", "Sardinia")),
    }

    @classmethod
    def _fallback_city(cls, country, postal, city) -> str | None:
        """Groessere Ersatz-Stadt fuer die AliExpress-Validierung (nur AT), oder None."""
        import re as _re
        if (country or "").strip().upper() != "AT":
            return None
        plz = _re.sub(r"\D", "", str(postal or ""))
        if not plz:
            return None
        cap = ("Bregenz" if plz[:2] in ("67", "68", "69")
               else ("Lienz" if plz[:2] == "99" else cls._AT_PLZ_HAUPTSTADT.get(plz[:1])))
        if not cap or (str(city or "").strip().lower() == cap.lower()):
            return None
        return cap

    @staticmethod
    def _derive_province(country: str | None, postal, *, english: bool = False) -> str | None:
        """Fehlendes Bundesland aus der PLZ ableiten (aktuell nur AT). None = unbekannt."""
        import re as _re
        c = (country or "").strip().upper()
        plz = _re.sub(r"\D", "", str(postal or ""))
        if c != "AT" or not plz:
            return None
        if plz[:2] in ("67", "68", "69"):
            pair = ("Vorarlberg", "Vorarlberg")
        elif plz[:2] == "99":
            pair = ("Tirol", "Tyrol")
        else:
            pair = RealAliExpressClient._AT_PLZ_BUNDESLAND.get(plz[:1])
        if not pair:
            return None
        return pair[1] if english else pair[0]

    @staticmethod
    def _logistics_address(delivery_name: str, delivery_address: dict, ship_to: str) -> dict:
        import re as _re
        addr = delivery_address or {}
        country = (addr.get("country") or ship_to)
        # AliExpress lehnt Vorwahl/Nummer MIT Sonderzeichen ab (Fehler
        # B_DROPSHIPPER_DELIVERY_ADDRESS_VALIDATE_FAIL). Darum NUR Ziffern senden:
        # '+49' -> '49', Nummer ohne +, Leerzeichen, Klammern, Bindestrichen.
        default_cc = RealAliExpressClient._PHONE_COUNTRY.get(str(country).strip().upper(), "49")
        phone_country = _re.sub(r"\D", "", str(addr.get("phone_country") or default_cc)) or default_cc
        raw_phone = str(addr.get("phone") or addr.get("mobile") or "")
        mobile = _re.sub(r"\D", "", raw_phone)
        # Fuehrende Vorwahl aus der Nummer entfernen, falls doppelt (z.B. '+4915...' -> '15...').
        # Nur bei EXPLIZIT internationaler Schreibweise ('+', '00…') bedingungslos strippen;
        # sonst nur, wenn der Rest wie eine Mobilnummer des Landes aussieht – schuetzt
        # Festnetznummern, deren Ortsvorwahl zufaellig mit dem Laendercode beginnt.
        explicit_intl = raw_phone.lstrip().startswith("+") or mobile.startswith("00" + phone_country)
        if mobile.startswith("00" + phone_country):
            mobile = mobile[2:]
        if mobile.startswith(phone_country) and len(mobile) > len(phone_country) + 6:
            rest = mobile[len(phone_country):]
            starts = RealAliExpressClient._MOBILE_START.get(phone_country, ())
            if explicit_intl or rest.startswith(starts):
                mobile = rest
        mobile = mobile.lstrip("0") or ""    # fuehrende 0 der nationalen Nummer weg
        # AliExpress verlangt 7-13 Ziffern ("Phone Number should contain 7-13 digits",
        # Sale 1147: Kaeufer-Tippfehler mit 14 Ziffern). Ungueltige Laenge -> Fallback,
        # wie bei fehlender Nummer (die Nummer dient nur der Zusteller-Kontaktbarkeit).
        if not mobile or not (7 <= len(mobile) <= 13):
            from app.config import get_settings
            mobile = _re.sub(r"\D", "", get_settings().aliexpress_fallback_phone) or "1600000000"
        # AliExpress verlangt province zwingend. Reihenfolge: echtes Bundesland aus der
        # Adresse > aus der PLZ abgeleitet (AT, Vorfall Sale 1123: Stadt-Fallback ist dort
        # KEIN gueltiges Bundesland -> ADDRESS_VALIDATE_FAIL) > Stadt (DE toleriert das,
        # DHL routet ohnehin nach PLZ+Ort) > Land.
        postal = str(addr.get("postal") or addr.get("postcode") or addr.get("zip") or "").strip()
        # eBay-Kaeufer schreiben die PLZ oft mit Laender-Praefix ("A-1010", "AT 1010",
        # "D-50667") — damit scheitern AliExpress-Validierung UND Bundesland-Ableitung.
        # Nur einen reinen BUCHSTABEN-Praefix vor einer Ziffern-PLZ strippen; NL-Formate
        # ("1234 AB") und GB-Formate ("SW1A 1AA") bleiben unangetastet.
        m_plz = _re.fullmatch(r"[A-Za-z]{1,3}[-\s]+(.*\d.*)", postal)
        if m_plz:
            postal = m_plz.group(1).strip()
        postal = postal or None
        # Stadt normalisieren: komplett kleingeschriebene Kaeufer-Eingaben ("hohenems")
        # lehnt die AliExpress-Staedte-Validierung ab ("Please enter a City", Sale 1143) —
        # Title-Case ("Hohenems") besteht sie. Gemischte Schreibweisen bleiben unangetastet.
        city = str(addr.get("city") or "").strip()
        if city and city == city.lower():
            city = city.title()
        province = (addr.get("province") or addr.get("state")
                    or RealAliExpressClient._derive_province(country, postal)
                    or city or country)
        # AT: die AliExpress-Divisionsliste kennt NUR englische Bundeslandnamen
        # ('Tirol'/'Wien' stehen nicht drin, Sale 1256/1274) -> direkt den
        # Listen-Namen senden, damit schon der ERSTE Versuch stimmt.
        if str(country or "").strip().upper() == "AT" and province:
            province = (RealAliExpressClient._AT_BL_EN.get(str(province).strip().lower())
                        or RealAliExpressClient._derive_province(country, postal, english=True)
                        or province)
        return {
            "contact_person": delivery_name,
            "full_name": delivery_name,
            "address": addr.get("street") or addr.get("address"),
            "city": city or None,
            "province": province,
            "zip": postal,
            "country": country,
            "phone_country": phone_country,
            "mobile_no": mobile,
        }

    @staticmethod
    def _extract_order_ids(result) -> list[str]:
        """ALLE Order-IDs aus der ds.order.create-Antwort ziehen.

        order_list kommt je nach Fall als Einzelwert, Liste oder {number: [...]}.
        Bei Haendler-Split legt AliExpress mehrere Orders an — keine ID verlieren.
        """
        raw = api._first_present(result, "order_id", "order_list", "ae_order_id")
        if isinstance(raw, dict):
            raw = api._first_present(raw, "number", "order_id") or raw
        if isinstance(raw, dict):        # weiterhin dict -> Werte nehmen
            raw = list(raw.values())
        if not isinstance(raw, (list, tuple)):
            raw = [raw]
        out: list[str] = []
        for r in raw:
            if isinstance(r, (list, tuple)):
                out.extend(str(x) for x in r if x)
            elif r:
                out.append(str(r))
        # Reihenfolge bewahren, Duplikate raus
        seen: set[str] = set()
        return [x for x in out if not (x in seen or seen.add(x))]

    # Kryptische AliExpress-Ablehnungscodes -> Klartext-Hinweis, was zu tun ist.
    _REJECT_HINTS = {
        "DELIVERY_METHOD_NOT_EXIST": (
            "Keine Versandart verfuegbar – meist reicht der Lieferanten-Bestand nicht fuer "
            "die bestellte MENGE (Vorfall Sale 1124: nur 1 Stueck da, 3 bestellt) oder das "
            "Produkt liefert nicht (mehr) ins Zielland. Bestand der Variante pruefen, "
            "Ausweich-Quelle verknuepfen oder Kaeufer kontaktieren."),
        "B_DROPSHIPPER_DELIVERY_ADDRESS_VALIDATE_FAIL": (
            "AliExpress lehnt die Lieferadresse ab — auch nach den automatischen "
            "Retries (Bundesland aus PLZ, englische Schreibweise, bei AT Ersatz-Stadt "
            "aus der PLZ-Zone). 'Please enter a City' -> als Stadt manuell die naechste "
            "Bezirksstadt eintragen, das Dorf mit in die Strassenzeile nehmen, PLZ "
            "unveraendert lassen (Post routet nach PLZ+Strasse; Vorfall Sale 1123: "
            "Rappoltenkirchen -> Stadt 'Tulln'). Sonst Telefon/Sonderzeichen pruefen."),
        "ERROR_WHEN_BUILD_FOR_PLACE_ORDER": (
            "AliExpress kann die Bestellung intern nicht aufbauen (Sale 1258). Haeufigste "
            "Ursachen: das Quell-Produkt ist per Dropshipping-API nicht bestellbar "
            "(z.B. 'Choice'-/Aktionsartikel), die Variante/Menge passt nicht mehr zum "
            "Angebot, oder es liefert nicht ins Zielland. Quelle im Preis-Check neu "
            "laden und pruefen (Choice-Logo?); sonst andere Quelle verknuepfen oder "
            "einmal manuell im AliExpress-Konto bestellen."),
        "SKU_NOT_EXIST": (
            "Die gewaehlte Variante (sku) existiert beim Lieferanten nicht (mehr) – Quelle "
            "im Preis-Check neu laden und Variante neu waehlen."),
        "INVENTORY_HOLD_ERROR": (
            "Lieferanten-Bestand reicht nicht (reserviert/ausverkauft) – Menge reduzieren "
            "oder Ausweich-Quelle nutzen."),
        "PRODUCT_NOT_EXIST": (
            "Das Quell-Produkt existiert bei AliExpress nicht mehr (vom Haendler entfernt / "
            "ausgelistet) – ueber diese Quelle NICHT bestellbar. Ausweich-Quelle im Preis-Check "
            "verknuepfen (Button Quelle) oder Kaeufer kontaktieren/erstatten, wenn es den "
            "Artikel nirgends mehr gibt."),
    }
    # Order-Reject-Codes, die bedeuten: das Produkt gibt es beim Lieferanten NICHT mehr.
    # Wie ein Scrape-404 behandeln (Ausweich-Quellen-Fluss), NICHT als generische Ablehnung.
    _PRODUCT_GONE_CODES = ("PRODUCT_NOT_EXIST", "PRODUCT_OFF_SHELF", "PRODUCT_HAS_BEEN_DELETED")

    @classmethod
    def _reject_message(cls, result) -> str:
        # Code-Suche im VOLLTEXT (nicht im gekuerzten) – bei langen error_msg stuende der
        # error_code sonst hinter der 250er-Grenze und der Klartext-Hinweis ginge verloren.
        full = str(result)
        msg = f"AliExpress-Bestellung abgelehnt: {full[:250]}"
        for code, hint in cls._REJECT_HINTS.items():
            if code in full:
                return f"{msg}\n→ {hint}"
        return msg

    async def _order_create(self, *, product_items: list[dict], delivery_name: str,
                            delivery_address: dict) -> list[str]:
        """Roher ds.order.create-Call -> Liste ALLER angelegten Order-IDs.

        GELD-SICHERHEIT: Fehler werden nach "sicher nicht bestellt" (OrderRejectedError)
        vs. "Ausgang unklar" (OrderUncertainError) getrennt, damit der Aufrufer nie
        eine womoeglich schon existierende Order ein zweites Mal sendet.

        ADRESS-FALLBACK: Wurde das Bundesland aus der PLZ abgeleitet (AT) und AliExpress
        lehnt die ADRESSE ab, wird GENAU EINMAL mit der englischen Schreibweise erneut
        versucht (z.B. 'Niederösterreich' -> 'Lower Austria'). Das ist geld-sicher, weil
        eine ABGELEHNTE Bestellung definitiv nicht angelegt wurde.
        """
        import json as _json
        addr = delivery_address or {}
        primary = self._logistics_address(
            delivery_name, delivery_address, self.settings.aliexpress_ship_to)
        candidates = [primary]
        # Englische Provinz-Variante nur, wenn die Provinz von UNS abgeleitet wurde
        # (Adresse selbst hatte keine) und sich die Schreibweise unterscheidet.
        if not (addr.get("province") or addr.get("state")):
            alt = self._derive_province(
                primary.get("country"),
                addr.get("postal") or addr.get("postcode") or addr.get("zip"),
                english=True)
            if alt and alt != primary.get("province"):
                second = dict(primary)
                second["province"] = alt
                candidates.append(second)
        # Letzte Rettung bei "Please enter a City" (Sale 1123/1256, AT): AliExpress kennt
        # kleine Orte nicht -> Landeshauptstadt der PLZ-Zone als Stadt, das Dorf bleibt
        # in der Strassenzeile. Geld-sicher: laeuft NUR nach einer ABLEHNUNG (definitiv
        # nicht bestellt), wie der englische Bundesland-Retry.
        is_at = str(primary.get("country") or "").strip().upper() == "AT"
        cap = self._fallback_city(primary.get("country"), primary.get("zip"),
                                  primary.get("city"))
        # AT: Landeshauptstaedte ('Innsbruck') und englische Stadtnamen ('Vienna')
        # stehen NICHT in der AliExpress-Divisionsliste (dort sind es BEZIRKE,
        # Aufloesung 12.08.) — diese Varianten sind fuer AT tote API-Calls und
        # werden uebersprungen; die Divisions-Staffel unten uebernimmt.
        if cap and not is_at:
            third = dict(candidates[-1])
            orig_city = str(primary.get("city") or "").strip()
            if orig_city and orig_city.lower() not in str(third.get("address") or "").lower():
                third["address"] = f"{third.get('address') or ''}, {orig_city}".strip(", ")
            third["city"] = cap
            candidates.append(third)
        # Englischer Staedtename (Sale 1256: 'Wien' -> 'Vienna'): die AliExpress-
        # Staedteliste ist bei AT/CH englisch. Nur nach definitiver Ablehnung dran.
        en_city = self._CITY_EN.get(str(primary.get("city") or "").strip().lower())
        if en_city and not is_at:
            cand = dict(candidates[-1])
            cand["city"] = en_city
            candidates.append(cand)
        # Italien (Sale 1266): eBay liefert den 2-Buchstaben-Provinzcode ('GE'); die
        # AliExpress-Liste kennt weder Codes noch Provinznamen ('Genova' live abgelehnt),
        # sondern die 20 REGIONEN -> Kette: Region (it), ggf. Region (en), zuletzt
        # zusaetzlich Provinzhauptort als Stadt (Dorf bleibt in der Strassenzeile).
        prov_code = str(primary.get("province") or "").strip().upper()
        if (str(primary.get("country") or "").strip().upper() == "IT"
                and prov_code in self._IT_REGIONS):
            region_it, region_en = self._IT_REGIONS[prov_code]
            capital = self._IT_PROVINCES.get(prov_code)
            cand_b = dict(candidates[-1])
            cand_b["province"] = region_it
            candidates.append(cand_b)
            if region_en != region_it:
                cand_b2 = dict(cand_b)
                cand_b2["province"] = region_en
                candidates.append(cand_b2)
            city_now = str(primary.get("city") or "").strip()
            if capital and city_now.lower() != capital.lower():
                cand_c = dict(candidates[-1])
                if city_now and city_now.lower() not in str(cand_c.get("address") or "").lower():
                    cand_c["address"] = f"{cand_c.get('address') or ''}, {city_now}".strip(", ")
                cand_c["city"] = capital
                candidates.append(cand_c)
        # Oesterreich, Aufloesung 12.08. (Recherche AliExpress-Divisionsliste, Sale
        # 1256/1274): AliExpress fuehrt fuer AT eine FESTE 2-Ebenen-Liste — province
        # nur als ENGLISCHER Bundeslandname ('Tyrol', 'Vienna', ...; deutsch steht
        # nicht drin, daher scheiterte 'Tirol'), und das city-Feld traegt BEZIRKE
        # ('Schwaz', 'Innsbruck-Stadt'; 'Innsbruck' selbst existiert NICHT — daher
        # 'Please enter a City' trotz gueltiger Stadt). 'Austria'/Leerstring: live
        # widerlegt ('Please select a province'). Produktiv bewaehrter Ausweg der
        # Dropshipping-Tools (ALD/AliDropship): city='Other', der echte Ort wandert
        # in die Adresszeile (Zustellung routet ueber PLZ+Strasse). Staffel:
        # EN-Bundesland+'Other' -> 'Other'/'Other'. Geld-sicher wie alle Retries:
        # laeuft NUR nach definitiver Ablehnung (= nicht bestellt).
        if (str(primary.get("country") or "").strip().upper() == "AT"
                and primary.get("province")):
            orig_city = str(primary.get("city") or "").strip()
            en_bl = (self._AT_BL_EN.get(str(primary.get("province") or "").strip().lower())
                     or self._derive_province("AT", primary.get("zip"), english=True)
                     or "Other")
            with_other = dict(primary)
            with_other["province"] = en_bl
            with_other["city"] = "Other"
            if orig_city and orig_city.lower() not in str(with_other.get("address") or "").lower():
                with_other["address"] = f"{with_other.get('address') or ''}, {orig_city}".strip(", ")
            candidates.append(with_other)
            last_resort = dict(with_other)
            last_resort["province"] = "Other"
            candidates.append(last_resort)

        last_reject: str | None = None
        # Diagnose-Transparenz (Sale 1258): WAS wurde bestellt? Nur Produkt/SKU/Menge,
        # keine Kaeuferdaten.
        items_info = " | ".join(
            f"pid={it.get('product_id')} sku_attr='{it.get('sku_attr')}' n={it.get('product_count')}"
            for it in product_items)
        for i, logistics_address in enumerate(candidates):
            ds_request = {
                "logistics_address": logistics_address,
                "product_items": product_items,
            }
            # Offizieller Parametername der ds.order.create-API (per MissingParameter-
            # Fehler verifiziert): param_place_order_request4_open_api_d_t_o
            try:
                data = await self._call("aliexpress.ds.order.create", {
                    "param_place_order_request4_open_api_d_t_o": _json.dumps(ds_request, ensure_ascii=False),
                })
            except PersistentError as exc:
                # Signatur-/Parameter-/Auth-Fehler VOR der Bestellung -> sicher nicht angelegt.
                # Grund MITGEBEN, sonst sieht der Nutzer nur "Request-Fehler" und weiss nicht,
                # dass z.B. die AliExpress-Autorisierung erneuert werden muss.
                _m = str(exc)
                if "utorisier" in _m or "token" in _m.lower():
                    raise OrderRejectedError(
                        "AliExpress-Autorisierung abgelaufen – bitte den AliExpress-Zugang neu "
                        "autorisieren, danach die Bestellung erneut ausloesen. Es wurde NICHTS "
                        f"bestellt. ({_m[:120]})")
                # Parameter-Ablehnung EINER Adress-Variante darf die Kette nicht
                # abbrechen (Sale 1274: MissingParameter 'province' beim Ohne-
                # Bundesland-Versuch stoppte die restlichen Varianten). Geld-sicher:
                # der Request wurde VOR der Bestellanlage abgelehnt. Nach der
                # letzten Variante wirft das Loop-Ende mit der Versuchsliste.
                last_reject = f"AliExpress-Bestellung abgelehnt: {_m[:180]}"
                logger.info("order candidate rejected pre-order, trying next",
                            extra={"idx": i, "of": len(candidates)})
                continue
            except Exception as exc:  # TransientError (Timeout/5xx/Netz) o.ae. -> Ausgang UNKLAR
                raise OrderUncertainError(
                    f"AliExpress-Bestellung: Ausgang unklar ({str(exc)[:150]}).")
            result = api._unwrap_result(data)
            if isinstance(result, dict) and result.get("is_success") in (False, "false"):
                last_reject = self._reject_message(result)
                # Abgelehnt = sicher NICHT bestellt -> Adress-Variante B einmal versuchen.
                if "ADDRESS_VALIDATE" in str(result) and i + 1 < len(candidates):
                    logger.info("order address rejected, retrying with english province",
                                extra={"province": candidates[i + 1].get("province")})
                    continue
                # Produkt beim Lieferanten weg -> wie 404 behandeln: der Aufrufer routet dann in den
                # Ausweich-Quellen-Fluss (Alternativvorschlag) statt in ein totes 'needs_manual_review'.
                # GELD-SICHER: is_success=false = die Order wurde definitiv NICHT angelegt.
                if any(code in str(result) for code in self._PRODUCT_GONE_CODES):
                    raise ProductNotFoundError(last_reject)
                # Diagnose: die tatsaechlich VERSUCHTEN Adress-Varianten (bis inkl. i) —
                # nur Stadt/Bundesland/PLZ, keine Namen/Strassen (Vorfall Sale 1256).
                tried_now = " | ".join(
                    f"{j + 1}) Stadt '{c.get('city')}' / Bundesland '{c.get('province')}' / PLZ '{c.get('zip')}'"
                    for j, c in enumerate(candidates[:i + 1]))
                raise OrderRejectedError(
                    f"{last_reject}\n[Positionen: {items_info}]"
                    f"\n[Adress-Versuche: {tried_now}]")
            ids = self._extract_order_ids(result)
            if not ids:
                # Antwort kam an, aber keine Order-ID -> die Order KANN trotzdem existieren.
                raise OrderUncertainError(f"AliExpress-Bestellung ohne Order-ID: {str(data)[:200]}")
            return ids
        # Transparenz fuer die Diagnose (Sale 1256): WELCHE Adress-Varianten wurden
        # versucht? Nur Stadt/Bundesland/PLZ — keine Namen/Strassen.
        tried = " | ".join(
            f"{i + 1}) Stadt '{c.get('city')}' / Bundesland '{c.get('province')}' / PLZ '{c.get('zip')}'"
            for i, c in enumerate(candidates))
        raise OrderRejectedError(
            (last_reject or "AliExpress-Bestellung abgelehnt.")
            + f"\n[Adress-Versuche: {tried}]")

    @staticmethod
    def _product_item(url: str, variant, quantity, delivery_address: dict) -> dict:
        return {
            "product_id": api.extract_product_id(url),
            "product_count": quantity or 1,
            "sku_attr": (variant or {}).get("attr") if isinstance(variant, dict) else None,
            "logistics_service_name": (delivery_address or {}).get("logistics_service_name"),
        }

    async def place_order(self, *, url, variant, quantity, delivery_name, delivery_address):
        ids = await self._order_create(
            product_items=[self._product_item(url, variant, quantity, delivery_address)],
            delivery_name=delivery_name, delivery_address=delivery_address)
        return PlacedOrder(aliexpress_order_id=ids[0], cost_cny=Decimal("0"))

    async def place_order_multi(self, *, items, delivery_name, delivery_address):
        """EIN ds.order.create mit allen Positionen (gleiche Lieferadresse).

        Erwartung: Positionen desselben Haendlers -> genau EINE Order-ID fuer alle.
        Liefert AliExpress dennoch mehrere IDs (Server-Split), koennen wir die
        Zuordnung ID->Position nicht sicher bestimmen -> alle IDs werden gemeldet,
        die erste ID wird allen Positionen zugeordnet und der Aufrufer bekommt
        ``mapping_uncertain`` zum Flaggen (Nutzer prueft im AliExpress-Konto).
        """
        product_items = [self._product_item(it["url"], it.get("variant"),
                                            it.get("quantity") or 1, delivery_address)
                         for it in items]
        ids = await self._order_create(product_items=product_items,
                                       delivery_name=delivery_name,
                                       delivery_address=delivery_address)
        if len(ids) == 1:
            return [{"aliexpress_order_id": ids[0], "cost_cny": Decimal("0"),
                     "item_indexes": list(range(len(items)))}]
        # Server-seitiger Split: Zuordnung unsicher -> konservativ flaggen.
        return [{"aliexpress_order_id": ids[0], "cost_cny": Decimal("0"),
                 "item_indexes": list(range(len(items))),
                 "all_order_ids": ids, "mapping_uncertain": True}]

    async def get_tracking(self, aliexpress_order_id: str) -> TrackingInfo:
        # NUR ds.order.tracking.get: das liefert die finale, kaeuferseitige Nummer
        # (in DE die DHL-/Hermes-Nummer), sobald sie existiert – vorher None. Die
        # Nummer entsteht erst mit der Zollabfertigung in DE. BEWUSST KEIN Fallback
        # auf trade.ds.order.get: dessen logistics_no ist nur die PROVISORISCHE
        # AliExpress/Cainiao-Nummer (AP…/LP…), die laut Nutzerregel (2026-07-05) NIE
        # an eBay gemeldet werden soll – gemeldet wird ausschliesslich die finale
        # Zusteller-Nummer (siehe order_service._is_final_tracking).
        data = await self._call("aliexpress.ds.order.tracking.get", {
            "ae_order_id": aliexpress_order_id,
            "language": self.settings.aliexpress_target_language,
        })
        t = api.parse_tracking(data)
        return TrackingInfo(
            tracking_number=t["tracking_number"],
            carrier=t["carrier"],
            status=t["status"],
        )

    async def get_order_detail(self, aliexpress_order_id: str) -> dict:
        """trade.ds.order.get: die ECHTE Order-Summe + Aufschluesselung.

        ds.order.create liefert KEINE Betraege zurueck – erst diese API zeigt, was
        tatsaechlich belastet wird: ``total`` (user_order_amount, meist USD) inkl.
        Produktpreis, ECHTEM Versand und der versteckten Steuer/Gebuehr (variiert je
        Produkt!). Genau diese Summe ist der reale EK (Vorfall #1142: geschaetzt
        8,88 €, real 13,37 $ = 11,72 €).
        Rueckgabe: {total, product, shipping, tax, currency, order_status}.
        """
        import json as _json
        data = await self._call("aliexpress.trade.ds.order.get", {
            "single_order_query": _json.dumps({"order_id": str(aliexpress_order_id)}),
        })
        res = ((data.get("aliexpress_trade_ds_order_get_response") or {}).get("result")) or {}
        childs = ((res.get("child_order_list") or {}).get("aeop_child_order_info")) or []
        if isinstance(childs, dict):
            childs = [childs]

        def _amt(node) -> float:
            try:
                return float((node or {}).get("amount") or 0)
            except (TypeError, ValueError):
                return 0.0

        # total + currency IMMER aus demselben Knoten. Fallback auf order_amount NUR,
        # wenn user_order_amount ganz FEHLT — steht er auf 0 (nie bezahlt/storniert),
        # ist 0 die Wahrheit und darf nicht durch den Nominalbetrag ersetzt werden.
        node = res.get("user_order_amount")
        if not isinstance(node, dict):
            node = res.get("order_amount") or {}
        return {
            "total": _amt(node),
            "product": sum(_amt(c.get("product_price")) * int(c.get("product_count") or 1)
                           for c in childs),
            "shipping": sum(_amt(c.get("actual_shipping_fee") or c.get("shipping_fee"))
                            for c in childs),
            "tax": sum(_amt(c.get("actual_tax_fee")) for c in childs),
            "currency": node.get("currency_code") or "USD",
            "order_status": res.get("order_status"),
        }

    async def image_search(self, image_bytes: bytes, *, page_size: int = 8) -> list[dict]:
        """ds.image.search: gleiches/ähnliches Produkt bei ANDEREN Händlern finden.

        Die API verlangt das Bild als Multipart-Datei `image_file_bytes` – dieses Feld
        wird NICHT signiert. Die String-Parameter werden normal signiert.
        Rückgabe: [{aliexpress_id, url, title, image, price_eur, store_url, seller_id}].
        """
        await self._ensure_token()
        s = self.settings
        business = {
            "access_token": self._access_token,
            "shpt_to": s.aliexpress_ship_to,
            "target_currency": "EUR",
            "target_language": s.aliexpress_target_language,
            "page_no": "1",
            "page_size": str(page_size),
        }
        params = api.signed_params(
            "aliexpress.ds.image.search", business,
            app_key=s.aliexpress_app_key, app_secret=s.aliexpress_app_secret,
            sign_method=s.aliexpress_sign_method,
        )
        try:
            resp = await self._http().post(
                s.aliexpress_api_base, data=params,
                files={"image_file_bytes": ("query.jpg", image_bytes, "image/jpeg")},
            )
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
        except httpx.HTTPStatusError as exc:
            raise TransientError(f"AliExpress image.search {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise TransientError(str(exc)) from exc
        if isinstance(data, dict) and "error_response" in data:
            raise PersistentError(f"AliExpress image.search: {data['error_response']}")

        root = data.get(next(iter(data)), {}) if len(data) == 1 else data
        hits = api._deep_get(root, "data", "products", "traffic_image_product_d_t_o") or []
        if isinstance(hits, dict):
            hits = [hits]
        out = []
        for p in hits if isinstance(hits, list) else []:
            pid = str(api._first_present(p, "product_id", "productId") or "")
            if not pid:
                continue
            price = api._first_present(p, "target_sale_price", "sale_price", "app_sale_price")
            out.append({
                "aliexpress_id": pid,
                "url": f"https://de.aliexpress.com/item/{pid}.html",
                "title": api._first_present(p, "product_title", "title"),
                "image": api._first_present(p, "product_main_image_url", "product_image_url"),
                "price_eur": float(str(price).replace(",", ".")) if price not in (None, "") else None,
                "store_url": api._first_present(p, "shop_url", "store_url"),
                "seller_id": api._first_present(p, "seller_id", "shop_id"),
            })
        return out

    async def find_alternative(self, image_url: str) -> list[str]:
        """Rückwärtskompatibel: Bild von der URL laden -> image_search -> nur URLs."""
        try:
            img = (await self._http().get(image_url)).content
            hits = await self.image_search(img, page_size=6)
        except Exception as exc:  # noqa: BLE001
            logger.warning("aliexpress image search failed", extra={"error": str(exc)})
            return []
        return [h["url"] for h in hits if h.get("url")]
