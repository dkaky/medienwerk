"""Pydantic-Request/Response-Schemas (Spec Kap. 3 – REST API)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, EmailStr, Field


# ============================ Bereich 1: Produktupload =====================
class ProductUploadRequest(BaseModel):
    aliexpress_url: str
    skip_autods: bool = False  # Test-Modus
    # Bewusstes "trotzdem hochladen" des Nutzers, wenn das Programm das Listing
    # noch als live fuehrt, es auf eBay aber nicht mehr gibt. Nie automatisch.
    ignoriere_duplikat: bool = False


class ProductUploadResponse(BaseModel):
    product_id: int
    listing_id: int
    ebay_draft_id: Optional[str] = None
    status: str
    title_seo: str
    description: str
    warnings: list[str] = Field(default_factory=list)
    backend: Optional[str] = None          # "native" | "autods"
    price_eur: Optional[float] = None
    cost_eur: Optional[float] = None
    profit_eur: Optional[float] = None
    margin_pct: Optional[float] = None


class FinalizeOverrides(BaseModel):
    title: Optional[str] = None
    category_id: Optional[str] = None


class ProductFinalizeRequest(BaseModel):
    approve: bool = True
    overrides: FinalizeOverrides = Field(default_factory=FinalizeOverrides)


class ProductFinalizeResponse(BaseModel):
    listing_id: int
    ebay_item_id: Optional[str] = None
    status: str


# ============================ Bereich 2: Auftragsabwicklung ================
class IpnAck(BaseModel):
    status: str = "received"


class FulfillRequest(BaseModel):
    approve_variant: bool = True
    override_delivery_name: Optional[str] = None
    # Optional: explizit gewaehlte Ausweich-Quelle (Slot 2/3) statt der Hauptquelle
    source_aliexpress_id: Optional[str] = None
    # Optional: exakte AliExpress-Variante (aus dem Quellen-/Varianten-Modal)
    sku_attr: Optional[str] = None
    # Verlust-Sperre bewusst uebergehen ("Trotzdem bestellen").
    force: bool = False


class FulfillResponse(BaseModel):
    sale_id: int
    order_id: int
    aliexpress_order_id: Optional[str] = None
    tracking_placeholder: str = "pending"
    status: str
    # Transparenz: automatisch auf die verknuepfte Ausweich-Variante geroutet?
    source_aliexpress_id: Optional[str] = None
    auto_routed: bool = False
    routed_from_attr: Optional[str] = None
    routed_reason: Optional[str] = None


class TrackingResponse(BaseModel):
    order_id: int
    tracking_number: Optional[str] = None
    carrier: Optional[str] = None
    status: str
    last_update: Optional[datetime] = None
    estimated_delivery: Optional[datetime] = None


# ============================ Bereich 3: Belegablage =======================
class InvoiceAttachRequest(BaseModel):
    bank_transaction_id: Optional[int] = None


class InvoiceItem(BaseModel):
    type: str
    file_path: str
    amount: Optional[Decimal] = None


class BankMatch(BaseModel):
    id: int
    status: str


class InvoiceAttachResponse(BaseModel):
    order_id: int
    invoices: list[InvoiceItem]
    bank_match: Optional[BankMatch] = None


class InvoiceSearchRequest(BaseModel):
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    amount_min: Optional[Decimal] = None
    amount_max: Optional[Decimal] = None
    type: str = "all"  # all | aliexpress_purchase | ebay_sales


class InvoiceSearchResult(BaseModel):
    id: int
    type: Optional[str] = None
    invoice_number: Optional[str] = None
    date: Optional[date] = None
    file_path: Optional[str] = None


class InvoiceSearchResponse(BaseModel):
    results: list[InvoiceSearchResult]


# ============================ Bereich 4: Listing-Optimierung ===============
class ListingPerformanceItem(BaseModel):
    listing_id: int
    title: str
    impressions: int
    clicks: int
    ctr: float
    optimization_status: Optional[str] = None
    last_changed: Optional[datetime] = None
    category: Optional[str] = None


class PerformanceStats(BaseModel):
    total: int
    active: int
    zero_clicks: int
    in_escalation: int


class ListingPerformanceResponse(BaseModel):
    listings: list[ListingPerformanceItem]
    stats: PerformanceStats


class OptimizeStageRequest(BaseModel):
    new_title: Optional[str] = None
    new_category: Optional[str] = None
    new_image_index: Optional[int] = None
    reason: Optional[str] = None


class OptimizeStageResponse(BaseModel):
    listing_id: int
    stage: int
    changes_applied: list[str]
    next_review: Optional[datetime] = None


class WeeklyOptimizeRequest(BaseModel):
    dry_run: bool = False


class WeeklyOptimizeResponse(BaseModel):
    task_id: str
    listings_analyzed: int
    candidates: int            # 0-Klick-Listings, die einen Vorschlag brauchen
    suggestions_created: int   # neue KI-Titelvorschlaege (propose-only, nicht angewandt)
    status: str


# ============================ Pricing / Repricing ==========================
class PricingCalculateRequest(BaseModel):
    """Verkaufspreis-Rechner (Modell nach eigenem Kalkulator). Alles in EUR."""
    cost_eur: Optional[float] = None
    price_cny: Optional[float] = None    # optionaler Alt-Pfad (wird mit Faktor umgerechnet)
    fee_pct: Optional[float] = None
    fixed_fee_eur: Optional[float] = None
    profit_pct: Optional[float] = None
    profit_eur: Optional[float] = None
    min_profit_eur: Optional[float] = None
    price_cents: Optional[float] = None
    min_price_eur: Optional[float] = None
    max_price_eur: Optional[float] = None


class PriceBreakdownResponse(BaseModel):
    cost_eur: float
    profit_eur: float
    fees_eur: float
    price_eur: float
    rounded_price_eur: float
    ebay_fee_eur: float
    fixed_fee_eur: float
    margin_pct: float
    markup_pct: float
    price_cents: float
    clamped: Optional[str] = None


class MonitoringSyncResponse(BaseModel):
    task_id: str
    checked: int
    price_drift: int          # erkannte Preis-Abweichungen (Freigabe im Preis-Check)
    out_of_stock: int
    errors: int
    status: str


class SyncListingResponse(BaseModel):
    listing_id: int
    action: str
    in_stock: bool
    old_price_eur: Optional[float] = None
    new_price_eur: Optional[float] = None
    target_price_eur: Optional[float] = None   # Soll-Preis laut Kalkulation (nur Info)


class MonitoringStatusItem(BaseModel):
    listing_id: int
    title: Optional[str] = None
    sku: Optional[str] = None
    price_eur: Optional[float] = None
    cost_eur: Optional[float] = None
    profit_eur: Optional[float] = None
    auto_reprice: bool
    supplier_in_stock: bool
    monitor_status: Optional[str] = None
    last_monitored_at: Optional[datetime] = None


class MonitoringStatusResponse(BaseModel):
    items: list[MonitoringStatusItem]
    stats: dict[str, int]


# ============================ System ======================================
class HealthResponse(BaseModel):
    status: str
    uptime_hours: float
    db_connected: bool
    tasks_pending: int
    mocks_enabled: bool                 # Basis-Schalter use_mocks (Dev-Default; nur Sekundär-Reads/IPN)
    ebay_live: bool = False             # REAL: echte eBay-Zugangsdaten gesetzt -> Listings/Orders/Gebühren laufen live
    mock_llm: bool = False              # EFFEKTIV: läuft die KI als Mock? (use_mock("llm"))
    fulfillment_engine: str = "native"
    sandbox: bool = False
    growth_engine_enabled: bool = False
    studio_enabled: bool = False        # Riegel des Studio-Trakts (eigene Motive, Print-on-Demand)
    # Sperrt der Attrappen-Betrieb das VEROEFFENTLICHEN? Lesen (Bestellungen holen,
    # Preise abgleichen) laeuft weiter echt - deshalb reicht ein einzelnes
    # "Live"-Signal nicht mehr aus, um den Zustand ehrlich zu beschreiben.
    ebay_schreiben_gesperrt: bool = False
