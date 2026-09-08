"""SQLAlchemy-ORM-Modelle (Spec Kap. 2 – Datenmodell).

Hinweise zur Spec:
* Die Spec nutzt `INDEX ... (...)` innerhalb von CREATE TABLE – das ist
  MySQL-Syntax und in PostgreSQL ungueltig. Hier korrekt ueber
  `index=True` / `__table_args__` geloest.
* JSONB wird als plattformneutraler JSON-Typ abgebildet (Postgres: JSONB
  via Variante, SQLite: JSON-Text), damit das Projekt auf beiden laeuft.
* Alle Tabellen haben created_at/updated_at (TimestampMixin). Das Audit-Log
  ist die Tabelle task_logs.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# JSON auf Postgres als JSONB, sonst generisches JSON (SQLite).
JSONType = JSON().with_variant(JSONB(), "postgresql")


class TimestampMixin:
    # ``default`` UND ``server_default``, und das ist kein Guertel-plus-Hosentraeger:
    # ``server_default`` steht nur in der CREATE-TABLE-Anweisung. Eine Tabelle, die
    # schon ohne diesen Default existierte, bekommt ihn nie mehr - create_all laesst
    # bestehende Tabellen in Ruhe, und _sqlite_add_missing_columns() traegt beim
    # ALTER TABLE nur ``default`` nach, nicht ``server_default``. Genau so entstand
    # data/pod_studio.db: created_at NOT NULL OHNE Default. Jedes erzeugte Motiv
    # starb danach an "NOT NULL constraint failed: studio_designs.created_at" -
    # nach dem bezahlten Bild, vor dem Speichern.
    # ``default`` schreibt den Wert dagegen in das INSERT selbst. Damit haengt das
    # Speichern nicht mehr daran, wie die Tabelle einmal angelegt wurde.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# --------------------------------------------------------------------------
# Product (Quelle / AliExpress)
# --------------------------------------------------------------------------
class Product(TimestampMixin, Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    aliexpress_url: Mapped[str] = mapped_column(String(1024), unique=True, nullable=False)
    aliexpress_id: Mapped[Optional[str]] = mapped_column(String(50), unique=True)
    title_raw: Mapped[Optional[str]] = mapped_column(Text)
    description_raw: Mapped[Optional[str]] = mapped_column(Text)
    price_cny: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    images: Mapped[Optional[list]] = mapped_column(JSONType)        # array of URLs
    variants: Mapped[Optional[dict]] = mapped_column(JSONType)      # variant tree
    supplier_id: Mapped[Optional[str]] = mapped_column(String(100))
    supplier_rating: Mapped[Optional[Decimal]] = mapped_column(Numeric(3, 2))
    # Alternative Haendler (Bildsuche): {items:[{url,store_url,price_eur,...}], avg_price_eur}
    alternatives: Mapped[Optional[dict]] = mapped_column(JSONType)

    listings: Mapped[list["Listing"]] = relationship(back_populates="product")


# --------------------------------------------------------------------------
# Listing (eBay)
# --------------------------------------------------------------------------
class Listing(TimestampMixin, Base):
    __tablename__ = "listings"
    __table_args__ = (
        Index("idx_listing_status", "listing_status"),
        Index("idx_listing_product", "product_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("products.id"))
    ebay_item_id: Mapped[Optional[str]] = mapped_column(String(20), unique=True)
    ebay_sku: Mapped[Optional[str]] = mapped_column(String(100))
    image_url: Mapped[Optional[str]] = mapped_column(String(1024))  # Titelbild (eBay-Galerie)
    title_seo: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category_id: Mapped[Optional[str]] = mapped_column(String(50))
    category_name: Mapped[Optional[str]] = mapped_column(String(255))
    # active | draft | ended | delist_pending
    listing_status: Mapped[str] = mapped_column(String(20), default="draft")
    price_eur: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    quantity_available: Mapped[Optional[int]] = mapped_column(default=0)
    impressions_week: Mapped[int] = mapped_column(default=0)
    clicks_week: Mapped[int] = mapped_column(default=0)
    # healthy | needs_review | in_escalation | optimized | reviewed
    optimization_status: Mapped[Optional[str]] = mapped_column(String(20), default="healthy")
    # 0=detect 1=title 2=image 3=delisted
    optimization_stage: Mapped[int] = mapped_column(default=0)
    last_optimization_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Zeitpunkt, an dem tatsaechlich Optimierungs-AENDERUNGEN uebernommen wurden
    # (Titel/Preis/Anzeigentarif). Steuert das 14-Tage-Ausblenden aus der Kandidatenliste
    # (getrennt von last_optimization_date = Vorschlags-Zeitpunkt).
    optimized_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Vorher-Momentaufnahme ZUM ZEITPUNKT der Optimierung: {clicks, impressions, sales,
    # price_eur, at}. Fuer den Nachverfolgungs-Reiter (Entwicklung vorher -> nachher).
    opt_snapshot: Mapped[Optional[dict]] = mapped_column(JSONType)
    # Echtes eBay-Einstelldatum (ListingDetails/StartTime) – "seit wann online".
    listing_start_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Aufraeum-Empfehlung verworfen ("behalten"): Zeitstempel = aus dem cleanup-Bucket
    # ausgeblendet (Nutzer will das Listing behalten). Nie automatisches Loeschen.
    cleanup_dismissed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Optimierung verworfen ("so lassen"): der Artikel soll unveraendert weiterlaufen und
    # taucht NICHT mehr in Preis-/Titel-/Reaktivieren-Buckets auf (Nutzer ist zufrieden).
    opt_dismissed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # eBay-Volume-Pricing (Mengenrabatt) Promotion-ID, wenn aktiviert (sonst None). Nur
    # per manuellem Klick gesetzt; Loeschen ueber deactivate. Nie automatisch.
    volume_promotion_id: Mapped[Optional[str]] = mapped_column(String(64))
    # PROPOSE-ONLY: von der KI vorgeschlagener neuer Titel, der NUR nach manueller
    # Freigabe auf eBay geht (nie automatisch). None = kein offener Vorschlag.
    optimization_suggestion: Mapped[Optional[str]] = mapped_column(Text)
    ebay_draft_id: Mapped[Optional[str]] = mapped_column(String(100))
    # Go-Live-Warteschlange: True = Publish angefordert/laeuft (ueberlebt Neustarts,
    # wird beim App-Start neu eingereiht); publish_error = letzter Fehler fuers UI.
    publish_queued: Mapped[bool] = mapped_column(default=False)
    publish_error: Mapped[Optional[str]] = mapped_column(Text)
    # Gelernte Varianten-Zuordnung eBay->AliExpress je Listing:
    # {norm. eBay-Auswahl (z.B. "schwarz"): sku_attr (z.B. "14:691#Black AI")}.
    # Wird gesetzt, wenn der Nutzer eine mehrdeutige Variante manuell zuordnet ->
    # alle kuenftigen Sales dieses Listings loesen sich automatisch auf.
    variant_map: Mapped[Optional[dict]] = mapped_column(JSONType)
    # MANUELL gesetzte Verkaufspreise je Variante (stabil ueber den AliExpress-sku_attr,
    # ueberlebt Re-Publish). {sku_attr: preis_eur}. Hat VORRANG vor der Kalkulation beim
    # Publish – so gehen die im Preis-Editor gesetzten Preise nie durch Neuveroeffentlichung
    # verloren (Vorfall 07/2026: 18,95 gesetzt, Publish machte 24,95 aus der Config-Formel).
    variant_prices: Mapped[Optional[dict]] = mapped_column(JSONType)
    # PER-VARIANTE Bestand, der zuletzt an eBay gepusht wurde: {ebay_sku: menge}.
    # ebay_sku = "<base>-V<i>" (gleiche stabile Zuordnung wie variant_prices/Publish).
    # 0 = diese Variante ist beim Lieferanten ausverkauft und auf eBay auf 0 gesetzt.
    # Spiegelt den Live-eBay-Zustand -> idempotenter Push (nur geaenderte SKUs) + UI-Anzeige.
    variant_stock: Mapped[Optional[dict]] = mapped_column(JSONType)
    # PROPOSE-ONLY Ausweich-Quelle JE VARIANTE: {sku_attr: aliexpress_id}. Vom Nutzer im
    # Preis-Check gesetzt. Ist eine Variante bei der Hauptquelle ausverkauft, bleibt sie
    # SELLBAR (Menge != 0), solange die verknuepfte Ausweich-Quelle lieferbar ist; bestellt
    # wird dann gezielt dort (Freigabe pro Bestellung). Ohne Verknuepfung -> Menge 0.
    variant_source_map: Mapped[Optional[dict]] = mapped_column(JSONType)
    # EIGENBESTAND je Variante: {sku_attr: {"qty": int, "cost_eur": float, "price_eur": float}}.
    # Selbst gelagerte Varianten brauchen KEINE AliExpress-Quelle, rechnen mit dem eigenen EK
    # (oft guenstigerer Versand) und gelten als lieferbar, solange qty > 0 (sonst -> OOS).
    self_stock: Mapped[Optional[dict]] = mapped_column(JSONType)

    # --- Repricing / Monitoring (nativer AutoDS-Ersatz) ---
    cost_eur: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))      # Einkauf inkl. FX
    # Echte AliExpress-Versandkosten dieses Produkts (freight query, EUR). None = unbekannt
    # -> Preiskalkulation nutzt die Pauschal-Schaetzung (1,99 €). Manche Artikel kosten mehr
    # (z.B. 3,29 €), manche liefern gratis (0 €) -> so wird die Marge nicht unterschaetzt.
    supplier_ship_eur: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    markup_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))     # Ziel-Aufschlag (z.B. 0.35)
    auto_reprice: Mapped[bool] = mapped_column(default=True)
    min_price_eur: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    max_price_eur: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    # Individueller Ziel-Mindestgewinn € (ueberschreibt die Config-Vorgabe von 8 €).
    # So kann man je Produkt eine kleinere/groessere Marge fahren (Konkurrenzfaehigkeit)
    # -> wirkt auf den Basis- UND alle Varianten-Preise.
    min_profit_eur: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    # Varianten-Bilder DES EIGENEN EBAY-LISTINGS: {variant_map_key(eBay-Auswahl): image_url}.
    # Quelle der Wahrheit fuer die Orders-Vorschau ("welche Variante wurde verkauft?") –
    # der Nutzer sieht SEIN eBay-Bild, nicht das (ggf. abweichende) AliExpress-SKU-Bild.
    ebay_variant_images: Mapped[Optional[dict]] = mapped_column(JSONType)
    # Voll-Galerie DES EIGENEN EBAY-LISTINGS (24h-Cache): {"urls": [...], "fetched_at": iso}.
    # Das Detail-Modal zeigt die AKTIVEN eBay-Bilder statt der AliExpress-Quelle
    # (Nutzer-Fund 08.08.); init_db legt die Spalte beim Start selbst an.
    ebay_gallery: Mapped[Optional[dict]] = mapped_column(JSONType)
    # ECHTE Live-eBay-Preise je Variante {ebay_sku: preis_eur}, aus der eBay-API abgeglichen
    # (get_item_price_info). QUELLE DER WAHRHEIT fuer die Cockpit-Marge: die Marge wird auf DIESEM
    # Preis gerechnet (nicht dem internen Zielpreis) und es wird GEWARNT, wenn intern != eBay – so
    # kann kein nie/fehlgeschlagen gepushter Preis eine optimistische Scheinmarge vortaeuschen
    # (Fund 17.07.: intern 29-32 €, eBay 17,95 €). None = noch nie abgeglichen.
    ebay_live_prices: Mapped[Optional[dict]] = mapped_column(JSONType)
    ebay_price_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    supplier_in_stock: Mapped[bool] = mapped_column(default=True)
    # ok | price_changed | out_of_stock | error
    monitor_status: Mapped[str] = mapped_column(String(20), default="ok")
    # Manuelle Verkaufs-Sperre: Listing bleibt bestehen, wird aber vom Monitoring NICHT
    # angefasst (kein Auto-Restock) und auf eBay auf Menge 0 gehalten. Genutzt, wenn ein
    # Listing korrigiert werden muss, bevor es wieder verkaufen darf (z.B. falsche Maße).
    sales_hold: Mapped[bool] = mapped_column(default=False)
    hold_reason: Mapped[Optional[str]] = mapped_column(String(200))
    last_monitored_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    item_specifics: Mapped[Optional[dict]] = mapped_column(JSONType)   # eBay-Artikelmerkmale

    # --- Performance-Statistik (eBay: QuantitySold + Analytics Traffic-Report) ---
    sales_total: Mapped[Optional[int]] = mapped_column()      # Verkäufe gesamt (Lebenszeit)
    views_30d: Mapped[Optional[int]] = mapped_column()        # Aufrufe letzte 30 Tage
    stats_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Zuletzt bekannte ECHTE Promoted-Listings-Anzeigenrate als BRUCH (0.12 = 12%).
    # Aus eBay Marketing API (getAds) oder beim Push (set_ad_rate) gesetzt.
    # None = unbekannt -> Gebuehrenkalkulation nutzt die Pauschale (settings.ebay_ad_rate_pct).
    ad_rate_pct: Mapped[Optional[float]] = mapped_column()

    # --- Versand-Policy des Live-Listings (Trading GetItem, fuer 3€-Zuschlag-Filter) ---
    shipping_policy_id: Mapped[Optional[str]] = mapped_column(String(20))
    shipping_policy_name: Mapped[Optional[str]] = mapped_column(String(120))

    product: Mapped[Optional["Product"]] = relationship(back_populates="listings")
    sales: Mapped[list["Sale"]] = relationship(back_populates="listing")
    price_history: Mapped[list["PriceHistory"]] = relationship(
        back_populates="listing", cascade="all, delete-orphan"
    )


# --------------------------------------------------------------------------
# PriceHistory (Repricing-Audit)
# --------------------------------------------------------------------------
class PriceHistory(TimestampMixin, Base):
    __tablename__ = "price_history"
    __table_args__ = (Index("idx_pricehist_listing", "listing_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"))
    old_price_eur: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    new_price_eur: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    cost_eur: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    profit_eur: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    margin_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    # reprice | stock_sync | manual | initial
    reason: Mapped[Optional[str]] = mapped_column(String(30))

    listing: Mapped["Listing"] = relationship(back_populates="price_history")


# --------------------------------------------------------------------------
# Sale (eBay-Verkauf)
# --------------------------------------------------------------------------
class Sale(TimestampMixin, Base):
    __tablename__ = "sales"
    __table_args__ = (
        Index("idx_sale_ebay_tx", "ebay_transaction_id"),
        Index("idx_sale_listing", "listing_id"),
        # Mehrpositions-Bestellungen: alle Positionen einer eBay-Order gruppieren
        Index("idx_sale_ebay_order", "ebay_order_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ebay_transaction_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    ebay_order_id: Mapped[Optional[str]] = mapped_column(String(50))
    ebay_line_item_id: Mapped[Optional[str]] = mapped_column(String(50))  # fuer createShippingFulfillment
    listing_id: Mapped[Optional[int]] = mapped_column(ForeignKey("listings.id"))
    buyer_name: Mapped[Optional[str]] = mapped_column(String(255))
    buyer_email: Mapped[Optional[str]] = mapped_column(String(255))
    delivery_address: Mapped[Optional[dict]] = mapped_column(JSONType)
    quantity: Mapped[Optional[int]] = mapped_column(default=1)
    variant_selected: Mapped[Optional[dict]] = mapped_column(JSONType)
    price_eur: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    fee_eur_actual: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))  # echte eBay-Gebuehr (Finances API)
    sale_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # pending | ordered_aliexpress | tracking | delivered | refunded | cancelled
    # needs_manual_review | alternative_pending | manual_intervention_required
    status: Mapped[str] = mapped_column(String(40), default="pending")
    # Roh-Stornostatus von eBay (cancelStatus.state: CANCELED | IN_PROGRESS).
    # Getrennt vom eigenen status, damit "storniert NACH Einkauf" abbildbar ist,
    # ohne den Fulfillment-Status zu verbiegen.
    ebay_cancel_state: Mapped[Optional[str]] = mapped_column(String(20))
    # Storno geprueft/abgehakt: cancelled-Sales sind nur TEMPORAER ein Handlungspunkt
    # (Haendler-Storno -> neu bestellen?). Nach "Erledigt" fallen sie aus der
    # Storno-Reviewliste (langfristiges Tracking ist unwichtig).
    cancel_reviewed: Mapped[bool] = mapped_column(default=False)
    # "Bei AliExpress bezahlt" — rein manuelle Markierung aus dem Dashboard ("Bezahlen").
    # AliExpress meldet uns den Zahlstatus nicht zurueck; das Flag zeigt an, dass die
    # Zahlung erledigt ist (unbezahlte AE-Orders verfallen nach ~24 h).
    ae_paid: Mapped[bool] = mapped_column(default=False)
    # Liefer-Check fuer Auslands-Bestellungen (Nutzerauftrag 16.08.): liefert der
    # AliExpress-Haendler ins Zielland? {"land","checked_at","haupt_ok":bool|null,
    # "alt_ok":{ali_id:bool|null},"warnung":str|null}; NULL = nie geprueft (z.B. DE).
    delivery_check: Mapped[Optional[dict]] = mapped_column(JSONType)

    listing: Mapped[Optional["Listing"]] = relationship(back_populates="sales")
    order: Mapped[Optional["OrderAliexpress"]] = relationship(back_populates="sale", uselist=False)


# --------------------------------------------------------------------------
# Order (AliExpress)
# --------------------------------------------------------------------------
class OrderAliexpress(TimestampMixin, Base):
    __tablename__ = "orders_aliexpress"
    __table_args__ = (
        Index("idx_ae_order", "aliexpress_order_id"),
        Index("idx_ae_sale", "sale_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sales.id"), unique=True)
    # NICHT unique: bei Mehrpositions-Bestellungen (eine eBay-Order, ein Haendler)
    # teilen sich mehrere Sales EINE AliExpress-Order-ID.
    aliexpress_order_id: Mapped[Optional[str]] = mapped_column(String(50))
    product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("products.id"))
    # Tatsaechlich bestellte Quelle (Slot), falls nicht die Hauptquelle bestellt wurde.
    source_aliexpress_id: Mapped[Optional[str]] = mapped_column(String(50))
    variant_selected: Mapped[Optional[dict]] = mapped_column(JSONType)
    quantity: Mapped[Optional[int]] = mapped_column(default=1)
    cost_cny: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    # Herkunft des EK-Werts: api (echte Order-Summe) | estimate | receipt (Beleg) |
    # manual (EK-Doppelklick). Der Backfill ueberschreibt NUR estimate/api/NULL —
    # nie manuelle Korrekturen oder Beleg-Werte (Review-Fund 13.07.).
    cost_source: Mapped[Optional[str]] = mapped_column(String(12))
    delivery_name: Mapped[Optional[str]] = mapped_column(String(255))
    delivery_address: Mapped[Optional[dict]] = mapped_column(JSONType)
    order_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # ordering (Claim VOR dem API-Call, gegen Doppelbestellung) |
    # pending | ordered | shipped | delivered | failed
    status: Mapped[str] = mapped_column(String(20), default="pending")
    tracking_number: Mapped[Optional[str]] = mapped_column(String(100))
    tracking_carrier: Mapped[Optional[str]] = mapped_column(String(50))
    estimated_delivery: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Letzter DHL-Zustellstatus-Abruf (Rate-Limit-Drossel: je Sendung max. ~1x/Tag pruefen).
    delivery_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    invoice_data: Mapped[Optional[dict]] = mapped_column(JSONType)

    sale: Mapped[Optional["Sale"]] = relationship(back_populates="order")


# --------------------------------------------------------------------------
# Invoice (Belege)
# --------------------------------------------------------------------------
class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        Index("idx_invoice_reference", "reference_id"),
        Index("idx_invoice_sale", "sale_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # ebay_sales | aliexpress_purchase | betriebsausgabe (manuell erfasste Ausgabe)
    type: Mapped[Optional[str]] = mapped_column(String(30))
    # Kategorie fuer manuelle Betriebsausgaben (Software/Abo, Hosting, KI/API, Wareneinkauf, ...)
    category: Mapped[Optional[str]] = mapped_column(String(60))
    reference_id: Mapped[Optional[str]] = mapped_column(String(100))
    # Voller Freitext (ungekuerzt) – z.B. Bewirtungsbeleg-Pflichtangaben (Anlass, Teilnehmer,
    # Ort). reference_id bleibt das kurze Label (100 Zeichen); note haelt die vollen Details.
    note: Mapped[Optional[str]] = mapped_column(Text)
    invoice_number: Mapped[Optional[str]] = mapped_column(String(100))
    invoice_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    currency: Mapped[Optional[str]] = mapped_column(String(3))
    file_path: Mapped[Optional[str]] = mapped_column(String(1024))
    file_hash: Mapped[Optional[str]] = mapped_column(String(64))  # SHA256 zur Dedup
    # True = echtes heruntergeladenes Original (AE-Bild / eBay-PDF), nicht der generierte Platzhalter
    is_original: Mapped[bool] = mapped_column(default=False)
    # Aus dem Original-BELEG erzeugte RECHNUNG (1:1-Nachbau der AliExpress-Rechnung).
    # Liegt NEBEN dem Beleg – file_path/is_original bleiben davon unberuehrt.
    generated_path: Mapped[Optional[str]] = mapped_column(String(1024))
    generated_hash: Mapped[Optional[str]] = mapped_column(String(64))
    # Was auf dem Beleg stand (Audit-Spur: welche Werte sind in die Rechnung geflossen)
    receipt_data: Mapped[Optional[dict]] = mapped_column(JSONType)
    sale_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sales.id"))
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders_aliexpress.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# --------------------------------------------------------------------------
# BankTransaction (Bankbuchung)
# --------------------------------------------------------------------------
class BankTransaction(TimestampMixin, Base):
    __tablename__ = "bank_transactions"
    __table_args__ = (
        Index("idx_bank_date", "transaction_date"),
        Index("idx_bank_amount", "amount"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    bank_ref: Mapped[Optional[str]] = mapped_column(String(100), unique=True)
    transaction_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    description: Mapped[Optional[str]] = mapped_column(Text)
    counterparty_name: Mapped[Optional[str]] = mapped_column(String(255))
    # pending | matched | invoiced
    status: Mapped[str] = mapped_column(String(20), default="pending")
    sale_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sales.id"))
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders_aliexpress.id"))
    invoices: Mapped[Optional[list]] = mapped_column(JSONType)  # array of invoice IDs
    # Kontist-Abgleich (bank_sync_service): {"kind","rule","order_ids","ae_order_ids"}.
    # Gesetzt beim automatischen AliExpress-Matching; bank_ref traegt "kontist:<Tx-ID>".
    match_info: Mapped[Optional[dict]] = mapped_column(JSONType)
    # Kontierung (kontierung_service, Grundstein fuer Wajjahats Buchhaltungs-System):
    # interner Kategorie-Schluessel (KATEGORIEN) + Quelle regel|manuell.
    # Regeln ueberschreiben NIE manuell gesetzte Werte.
    kontierung: Mapped[Optional[str]] = mapped_column(String(40))
    kontierung_source: Mapped[Optional[str]] = mapped_column(String(10))


# --------------------------------------------------------------------------
# ProductIdea (Produkt-Recherche / Vorschläge zum Review)
# --------------------------------------------------------------------------
class ProductIdea(TimestampMixin, Base):
    __tablename__ = "product_ideas"
    __table_args__ = (Index("idx_idea_status", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    aliexpress_id: Mapped[str] = mapped_column(String(40), unique=True)
    aliexpress_url: Mapped[Optional[str]] = mapped_column(String(1024))
    title: Mapped[Optional[str]] = mapped_column(String(500))
    short_desc: Mapped[Optional[str]] = mapped_column(Text)
    image_url: Mapped[Optional[str]] = mapped_column(String(1024))
    category: Mapped[Optional[str]] = mapped_column(String(255))
    niche: Mapped[Optional[str]] = mapped_column(String(120))    # Suchbegriff/Nische
    cost_eur: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))   # EK inkl. Versandaufschlag
    price_eur: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))  # empf. VK
    profit_eur: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    margin_pct: Mapped[Optional[float]] = mapped_column()
    rating: Mapped[Optional[float]] = mapped_column()            # Sterne (0-5)
    reviews: Mapped[Optional[int]] = mapped_column()
    orders_volume: Mapped[Optional[int]] = mapped_column()
    delivery_days: Mapped[Optional[int]] = mapped_column()
    store_name: Mapped[Optional[str]] = mapped_column(String(255))
    # AliExpress-Store-ID (ae_store_info) — Grundlage der Store-Pipeline
    # (Stores lokaler Produkte scrapen, Nutzerauftrag 14.08.).
    store_id: Mapped[Optional[str]] = mapped_column(String(32))
    is_choice: Mapped[Optional[bool]] = mapped_column(default=False)   # Näherung (sl_product)
    alternatives: Mapped[Optional[list]] = mapped_column(JSONType)     # [{url,store,price_eur}]
    # True = aus der taeglichen KI-Trend-Recherche (fuer 🔥-Markierung im UI).
    from_trend: Mapped[Optional[bool]] = mapped_column(default=False)
    # "Versand aus"-Werte der SKU-Achse 200007763 (z. B. ["Polen","Frankreich"]);
    # NULL = keine Angabe. Grundlage des Lokal-Lager-Filters (Fokus 08/2026).
    ships_from: Mapped[Optional[list]] = mapped_column(JSONType)
    # Entdeckungs-QUELLE der Idee: aliexpress (Direktsuche). 'amazon' kommt nur noch in
    # ALTBESTAND vor – die Amazon-Trend-Suche wurde am 03.08.2026 entfernt (lieferte zu oft
    # nichts Brauchbares), die vorhandenen Ideen bleiben aber sicht- und nutzbar. Der
    # importierte Artikel ist immer das AliExpress-Aequivalent (aliexpress_id); 'source'
    # merkt sich nur, WO der Trend gefunden wurde (fuer Badge + Herkunfts-Filter).
    source: Mapped[Optional[str]] = mapped_column(String(20), default="aliexpress")
    # Klartext-Herkunft der Quelle (Altbestand z.B. "Amazon-Bestseller #3 Sport: <Titel>").
    source_note: Mapped[Optional[str]] = mapped_column(String(400))
    # Link zum Original-Produkt der Quelle, aus dem die Idee stammt.
    source_url: Mapped[Optional[str]] = mapped_column(String(1024))
    # new | kept | rejected | importing | imported | import_failed
    status: Mapped[str] = mapped_column(String(20), default="new")
    import_error: Mapped[Optional[str]] = mapped_column(String(400))   # letzter Import-Fehler (Klartext)
    # Wollte der Nutzer LIVE (🚀) oder nur Entwurf? Persistiert, damit die Zombie-Recovery nach
    # einem Neustart die urspruengliche Absicht wiederherstellt und einen Live-Wunsch nicht
    # stillschweigend zum Entwurf degradiert (Review-Fund 11.07.).
    publish_requested: Mapped[Optional[bool]] = mapped_column(default=False)


# --------------------------------------------------------------------------
# TaskLog (Audit)
# --------------------------------------------------------------------------
class TaskLog(TimestampMixin, Base):
    __tablename__ = "task_logs"
    __table_args__ = (Index("idx_task_type_status", "task_type", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # upload | fulfill | optimize | invoice_attach
    task_type: Mapped[Optional[str]] = mapped_column(String(50))
    reference_id: Mapped[Optional[str]] = mapped_column(String(100))
    # pending | in_progress | success | failed
    status: Mapped[str] = mapped_column(String(20), default="pending")
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    result_data: Mapped[Optional[dict]] = mapped_column(JSONType)
    retry_count: Mapped[int] = mapped_column(default=0)


class AppSetting(TimestampMixin, Base):
    """Kleiner Key/Value-Speicher fuer LAUFZEIT-Konfig, die der Nutzer im Dashboard setzt
    (z.B. DHL-API-Key) – ohne Server-/.env-Zugriff. Nur unkritische Betriebs-Einstellungen.
    """
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(Text)


# --------------------------------------------------------------------------
# Growth Engine V1
# --------------------------------------------------------------------------
class GrowthScorecard(TimestampMixin, Base):
    """Unveraenderliche, statusbereinigte Momentaufnahme der Geschaeftslage.

    Jede Kennzahl traegt ihre Abdeckung und Herkunft in ``coverage`` bzw.
    ``provenance``. Schaetzungen werden dadurch nie still als bestaetigte Zahlen
    behandelt.
    """
    __tablename__ = "growth_scorecards"
    __table_args__ = (
        Index("idx_growth_scorecard_calculated", "calculated_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_days: Mapped[int] = mapped_column(default=30)
    status_corrected_revenue_eur: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    completed_revenue_eur: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    completed_contribution_eur: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    contribution_margin_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(7, 4))
    ebay_fees_eur: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    ebay_fee_load_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(7, 4))
    supplier_cost_eur: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    supplier_cost_share_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(7, 4))
    active_listings: Mapped[int] = mapped_column(default=0)
    winner_listings: Mapped[int] = mapped_column(default=0)
    winner_concentration_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(7, 4))
    mature_no_sale_listings: Mapped[int] = mapped_column(default=0)
    confidence: Mapped[str] = mapped_column(String(16), default="LOW")
    coverage: Mapped[dict] = mapped_column(JSONType, default=dict)
    provenance: Mapped[dict] = mapped_column(JSONType, default=dict)
    schema_version: Mapped[str] = mapped_column(String(16), default="1.0")


class GrowthOpportunity(TimestampMixin, Base):
    """Erklaerbare, persistente Wachstumschance ohne Ausfuehrungsbefugnis."""
    __tablename__ = "growth_opportunities"
    __table_args__ = (
        Index("idx_growth_opp_status", "status"),
        Index("idx_growth_opp_type", "type"),
        Index("idx_growth_opp_listing", "listing_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    listing_id: Mapped[Optional[int]] = mapped_column(ForeignKey("listings.id"))
    product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("products.id"))
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_action: Mapped[Optional[str]] = mapped_column(Text)
    why_now: Mapped[Optional[str]] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSONType, default=dict)
    expected_impact: Mapped[int] = mapped_column(default=1)
    expected_contribution_uplift_eur: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    expected_contribution_margin_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(7, 4))
    expected_contribution_per_sale_eur: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    economic_provenance: Mapped[dict] = mapped_column(JSONType, default=dict)
    confidence: Mapped[str] = mapped_column(String(16), default="LOW")
    risk: Mapped[str] = mapped_column(String(16), default="MEDIUM")
    status: Mapped[str] = mapped_column(String(32), default="DISCOVERED")
    decision_state: Mapped[str] = mapped_column(String(24), default="PENDING")
    missing_evidence: Mapped[list] = mapped_column(JSONType, default=list)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text)
    last_evaluated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class GrowthExperiment(TimestampMixin, Base):
    """Registriertes Experiment; V1 veraendert selbst keine Live-Listings."""
    __tablename__ = "growth_experiments"
    __table_args__ = (
        Index("idx_growth_experiment_status", "status"),
        Index("idx_growth_experiment_opportunity", "opportunity_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[Optional[int]] = mapped_column(ForeignKey("growth_opportunities.id"))
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    target_cohort: Mapped[dict] = mapped_column(JSONType, default=dict)
    intervention: Mapped[str] = mapped_column(Text, nullable=False)
    control_method: Mapped[str] = mapped_column(Text, nullable=False)
    baseline: Mapped[dict] = mapped_column(JSONType, default=dict)
    start_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    observation_period_days: Mapped[int] = mapped_column(default=30)
    metrics: Mapped[dict] = mapped_column(JSONType, default=dict)
    success_threshold: Mapped[dict] = mapped_column(JSONType, default=dict)
    failure_threshold: Mapped[dict] = mapped_column(JSONType, default=dict)
    rollback_rule: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[Optional[dict]] = mapped_column(JSONType)
    status: Mapped[str] = mapped_column(String(32), default="DRAFT")
    evaluated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class GrowthExperimentAssignment(TimestampMixin, Base):
    """Listing-Zuordnung fuer Kontroll-, Wellen- und Multiple-Baseline-Designs."""
    __tablename__ = "growth_experiment_assignments"
    __table_args__ = (
        Index("idx_growth_assignment_experiment", "experiment_id"),
        Index("idx_growth_assignment_listing", "listing_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("growth_experiments.id"), nullable=False)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), nullable=False)
    cohort: Mapped[str] = mapped_column(String(32), default="TREATMENT")
    intervention_family: Mapped[Optional[str]] = mapped_column(String(64))
    wave: Mapped[Optional[int]] = mapped_column()
    baseline_snapshot: Mapped[dict] = mapped_column(JSONType, default=dict)
    intervention_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class GrowthApproval(TimestampMixin, Base):
    """Wiederverwendbares, append-only gedachtes Owner-Approval-Protokoll.

    Eine Freigabe dokumentiert eine Entscheidung. Sie fuehrt die angefragte Aktion
    niemals selbst aus.
    """
    __tablename__ = "growth_approvals"
    __table_args__ = (
        Index("idx_growth_approval_status", "status"),
        Index("idx_growth_approval_entity", "entity_type", "entity_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    action_type: Mapped[str] = mapped_column(String(48), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[int] = mapped_column(nullable=False)
    requested_action: Mapped[dict] = mapped_column(JSONType, default=dict)
    rationale: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[Optional[str]] = mapped_column(String(80))
    decision_note: Mapped[Optional[str]] = mapped_column(Text)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class GrowthListingMetric(TimestampMixin, Base):
    """Taeglicher, rein lesend erfasster Traffic-/Sales-Stand fuer Early Signals."""
    __tablename__ = "growth_listing_metrics"
    __table_args__ = (
        Index("idx_growth_metric_listing_captured", "listing_id", "captured_at", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    impressions: Mapped[Optional[int]] = mapped_column()
    clicks: Mapped[Optional[int]] = mapped_column()
    sales_total: Mapped[Optional[int]] = mapped_column()
    listing_status: Mapped[Optional[str]] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(32), default="LOCAL_LISTING_STATE")


class GrowthLearning(TimestampMixin, Base):
    """Abgeleitete, nachvollziehbare Erkenntnis aus einem abgeschlossenen Experiment."""
    __tablename__ = "growth_learnings"
    __table_args__ = (Index("idx_growth_learning_experiment", "experiment_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("growth_experiments.id"), nullable=False)
    classification: Mapped[str] = mapped_column(String(16), default="INFERENCE")
    conclusion: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONType, default=dict)
