"""Eigenstaendiger AliExpress-Client fuer den lokalen MCP-Server (nur httpx).

EHRLICH ZUR REALITAET:
AliExpress hat KEINEN Konto-Login fuer eine API. Zugang gibt es nur ueber die
**AliExpress Open Platform** (openservice.aliexpress.com): du registrierst eine App
und erhaeltst **App Key + App Secret**. Produkt-/Dropshipping-APIs ("aliexpress.ds.*")
muessen freigeschaltet werden, und manche Endpunkte brauchen zusaetzlich einen
**access_token** aus einem OAuth-Autorisierungsschritt. Es gibt also kein
"mit Benutzername/Passwort einloggen".

Praktische Alternative: AutoDS (bei dir bereits verbunden) bridged AliExpress
ohnehin -> fuer Sourcing/Import den AutoDS-MCP nutzen.

Modi:
  * mock=True (Default ohne App-Key): deterministische Beispieldaten, sofort lauffaehig.
  * mock=False: signierte Calls gegen das Open-Platform-Gateway. Die Signatur/
    Methoden-Namen sind mit ``# ANNAHME`` markiert und gegen die aktuelle
    Open-Platform-Doku zu verifizieren.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx


@dataclass
class AliExpressConfig:
    app_key: str = ""
    app_secret: str = ""
    access_token: str = ""
    base_url: str = "https://api-sg.aliexpress.com/sync"
    timeout: float = 30.0

    @property
    def mock(self) -> bool:
        return not (self.app_key and self.app_secret)

    @classmethod
    def from_env(cls) -> "AliExpressConfig":
        return cls(
            app_key=os.getenv("ALIEXPRESS_APP_KEY", "").strip(),
            app_secret=os.getenv("ALIEXPRESS_APP_SECRET", "").strip(),
            access_token=os.getenv("ALIEXPRESS_ACCESS_TOKEN", "").strip(),
            base_url=os.getenv("ALIEXPRESS_BASE_URL", "https://api-sg.aliexpress.com/sync").strip(),
        )


class AliExpressError(Exception):
    pass


class AliExpressClient:
    def __init__(self, config: Optional[AliExpressConfig] = None) -> None:
        self.cfg = config or AliExpressConfig.from_env()
        self._http: Optional[httpx.Client] = None

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=self.cfg.timeout)
        return self._http

    def _sign(self, params: dict) -> str:
        """HMAC-SHA256-Signatur ueber sortierte key+value-Konkatenation (# ANNAHME).

        Entspricht dem gaengigen Open-Platform-Schema (sign_method=sha256):
        Großbuchstaben-Hex von HMAC_SHA256(app_secret, ''.join(k+v fuer sortierte Paare)).
        """
        base = "".join(f"{k}{params[k]}" for k in sorted(params))
        digest = hmac.new(self.cfg.app_secret.encode(), base.encode(), hashlib.sha256).hexdigest()
        return digest.upper()

    def _call(self, method: str, business: dict) -> Any:
        sys_params = {
            "app_key": self.cfg.app_key,
            "method": method,
            "format": "json",
            "v": "2.0",
            "sign_method": "sha256",
            "timestamp": str(int(time.time() * 1000)),  # ms seit Epoch (# ANNAHME)
        }
        if self.cfg.access_token:
            sys_params["access_token"] = self.cfg.access_token
        params = {**sys_params, **{k: str(v) for k, v in business.items()}}
        params["sign"] = self._sign(params)
        try:
            r = self._client().post(self.cfg.base_url, data=params)
            r.raise_for_status()
            return r.json() if r.content else {}
        except httpx.HTTPStatusError as exc:
            raise AliExpressError(f"AliExpress {exc.response.status_code}: {exc.response.text[:300]}")
        except httpx.RequestError as exc:
            raise AliExpressError(f"Netzwerkfehler: {exc}")
        except ValueError as exc:
            raise AliExpressError(f"Kein JSON: {exc}")

    @staticmethod
    def _seed(value: str) -> int:
        return int(hashlib.sha256(value.encode()).hexdigest(), 16)

    # ------------------------------------------------------------------ Tools
    def test_connection(self) -> dict:
        if self.cfg.mock:
            return {"mode": "mock", "ok": True,
                    "note": "Kein ALIEXPRESS_APP_KEY/SECRET -> Mock. Echtbetrieb braucht eine "
                            "Open-Platform-App (App Key+Secret, ggf. access_token). "
                            "Fuer Sourcing ist der verbundene AutoDS-MCP der einfachere Weg."}
        return self._call("aliexpress.ds.member.benefit.get", {})  # ANNAHME: leichter Ping-Call

    def get_product(self, product_id: str, ship_to: str = "DE", currency: str = "EUR") -> dict:
        if self.cfg.mock:
            seed = self._seed(product_id)
            return {"mode": "mock", "productId": product_id,
                    "title": "Beispiel-Produkt (Mock) – Fahrrad Kettenschloss 95 cm",
                    "priceEur": round(3 + seed % 40 + 0.35, 2),
                    "variants": [{"name": "Farbe", "values": ["Orange", "Schwarz"]}],
                    "shipTo": ship_to, "currency": currency}
        return self._call("aliexpress.ds.product.get",  # ANNAHME
                         {"product_id": product_id, "ship_to_country": ship_to,
                          "target_currency": currency, "target_language": "DE"})

    def search(self, keywords: str, limit: int = 10, ship_to: str = "DE") -> dict:
        if self.cfg.mock:
            seed = self._seed(keywords)
            return {"mode": "mock", "keywords": keywords, "results": [{
                "productId": str(1005000000000 + (seed + i) % 999999),
                "title": f"{keywords} – Treffer {i + 1}",
                "priceEur": round(2 + (seed + i) % 60 + 0.99, 2),
            } for i in range(min(limit, 5))]}
        return self._call("aliexpress.ds.text.search",  # ANNAHME
                         {"keyWord": keywords, "pageSize": limit, "shipToCountry": ship_to})

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None
