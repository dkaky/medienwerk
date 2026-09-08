"""AutoDS-Integration (Spec Kap. 4.2).

REST mit ApiKey-Auth: Produktimport + Draft-Erstellung auf eBay.
HINWEIS: Die in der Spec angenommene oeffentliche AutoDS-REST-API ist nicht
verifiziert – vor Produktiveinsatz Verfuegbarkeit pruefen. Fallback laut Spec:
Selenium (siehe aliexpress.py).
"""
from __future__ import annotations

import abc
import hashlib
from dataclasses import dataclass


@dataclass
class AutoDSDraft:
    import_id: str
    ebay_draft_id: str
    status: str = "draft_created"


class AutoDSClient(abc.ABC):
    @abc.abstractmethod
    async def import_aliexpress(self, *, aliexpress_url: str, title_seo: str,
                               description: str, category_id: str | None) -> AutoDSDraft:
        ...

    @abc.abstractmethod
    async def sync_listing(self, listing_id: str, *, title: str, description: str) -> None:
        ...


class MockAutoDSClient(AutoDSClient):
    async def import_aliexpress(self, *, aliexpress_url, title_seo, description, category_id):
        h = hashlib.sha256(aliexpress_url.encode()).hexdigest()[:12]
        return AutoDSDraft(import_id=f"imp_{h}", ebay_draft_id=f"draft_{h}")

    async def sync_listing(self, listing_id, *, title, description) -> None:
        return None


class RealAutoDSClient(AutoDSClient):
    def __init__(self, settings) -> None:
        self.settings = settings
        self.base_url = settings.autods_base_url

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.settings.autods_api_key}"}

    async def import_aliexpress(self, *, aliexpress_url, title_seo, description, category_id):
        # TODO: POST {base_url}/import/aliexpress  (httpx.AsyncClient)
        raise NotImplementedError("AutoDS API noch nicht konfiguriert (USE_MOCKS=true setzen)")

    async def sync_listing(self, listing_id, *, title, description) -> None:
        # TODO: POST {base_url}/listings/{listing_id}/sync
        raise NotImplementedError
