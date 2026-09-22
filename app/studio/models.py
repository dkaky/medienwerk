"""Tabellen des Studio-Trakts (eigene Motive, Print-on-Demand).

Bewusst getrennt vom Handel: ``products`` bleibt der AliExpress-Bestand, hier
entstehen eigene Motive. Beruehrungspunkt ist genau eine Tabelle,
``studio_listing_links`` - sie beantwortet die Frage "gehoert dieses eBay-Angebot
zum Studio?".

Warum eine eigene Verknuepfungstabelle statt einer Spalte in ``listings``:
Eine zusaetzliche Spalte waere in der Abfrage billiger, verlangt aber ein
``ALTER TABLE`` auf der Handelstabelle - im laufenden Verkaufsbetrieb genau das,
was sich hinterher niemand mehr beweisen kann. So bleibt das Schema des Handels
unangetastet, und der Beweis "am Handel hat sich nichts geaendert" ist eine
einfache Gegenueberstellung.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models import TimestampMixin

# Zustaende eines Motivs. Etappe 1 kennt nur "draft" - erzeugt wird noch nichts.
DESIGN_STATUS = ("draft", "ready", "archived")


class StudioDesign(TimestampMixin, Base):
    """Ein eigenes Motiv - vom Einfall bis zur Druckdatei."""

    __tablename__ = "studio_designs"
    __table_args__ = (Index("idx_studio_design_status", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    # Womit das Bild entstanden ist (gpt-image-1, flux, upload) - reine Notiz.
    source: Mapped[Optional[str]] = mapped_column(String(30))
    image_url: Mapped[Optional[str]] = mapped_column(String(1024))
    # Prompt, Seed, Masse. Bewusst formfrei, damit spaetere Etappen nichts umbauen muessen.
    meta_json: Mapped[Optional[str]] = mapped_column(Text)


class StudioListingLink(Base):
    """Verbindet ein eBay-Angebot mit einem Studio-Motiv.

    Der Primaerschluessel ist die Angebots-Nummer: ein Angebot gehoert hoechstens
    einem Motiv. Diese Tabelle ist die alleinige Wahrheit fuer ``is_studio()``.
    """

    __tablename__ = "studio_listing_links"

    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), primary_key=True)
    design_id: Mapped[Optional[int]] = mapped_column(ForeignKey("studio_designs.id"))
    linked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Warum von Hand verknuepft - hilft spaeter beim Nachvollziehen.
    note: Mapped[Optional[str]] = mapped_column(Text)


# Die Kostenbremse bringt ihre eigene Tabelle mit. Hier eingebunden,
# damit sie beim Anlegen der Datenbank mit erzeugt wird.
from app.studio.kosten import StudioCostLog  # noqa: E402,F401


# --------------------------------------------------------------------------
# Motiv-Radar: was in FREMDEN Shops laeuft
# --------------------------------------------------------------------------

# Lebenslauf einer Idee: frisch geerntet -> vom Menschen sortiert -> zu einem
# eigenen Motiv geworden.
IDEE_STATUS = ("neu", "verworfen", "uebernommen")


class MotivIdee(TimestampMixin, Base):
    """Eine BEOBACHTUNG aus einem fremden Shop - keine eigene Idee.

    Bewusst eine eigene Tabelle neben ``studio_designs``. Ein Motiv dort ist
    unser Werk; eine Zeile hier ist fremdes Schaufenster. Wer beides in einen
    Topf wirft, verliert genau die Trennung, auf die es rechtlich ankommt:

    * ``fremdtitel`` ist BELEG - er sagt, wo die Beobachtung herkommt, und
      geht NIE in einen Bilder-Prompt (siehe ``app/studio/radar/umwandlung.py``).
    * ``thema`` und ``stichworte`` sind das, was uebernommen werden darf: der
      Gedanke, nicht der Wortlaut und nicht die Gestaltung.

    Zahlen bleiben leer, wenn der Shop sie nicht hergibt (Projektregel 3:
    keine Schaetzungen). ``signal`` ist deshalb immer nur so gut wie
    ``signal_grund`` es begruendet.
    """

    __tablename__ = "studio_motiv_ideen"
    __table_args__ = (
        Index("idx_motiv_idee_status", "status"),
        Index("idx_motiv_idee_quelle", "quelle_plattform", "quelle_shop"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    quelle_plattform: Mapped[str] = mapped_column(String(20), nullable=False)
    quelle_shop: Mapped[Optional[str]] = mapped_column(String(120))
    quelle_url: Mapped[Optional[str]] = mapped_column(String(1024))
    # Erkennungsmerkmal des fremden Artikels - haelt denselben Fund ueber
    # mehrere Laeufe hinweg zusammen, statt ihn jedes Mal neu anzulegen.
    fremd_id: Mapped[Optional[str]] = mapped_column(String(80))

    fremdtitel: Mapped[str] = mapped_column(Text, nullable=False)
    bild_url: Mapped[Optional[str]] = mapped_column(String(1024))
    thema: Mapped[Optional[str]] = mapped_column(String(255))
    stichworte: Mapped[Optional[str]] = mapped_column(Text)   # JSON-Liste
    # Die genaue Beschreibung dessen, was auf dem Motiv zu sehen ist - als JSON
    # (siehe ``app/studio/radar/beschreibung.py``). Sie ist das eigentliche
    # Arbeitsmaterial: aus ihr entsteht der Prompt, nicht aus dem Titel.
    beschreibung: Mapped[Optional[str]] = mapped_column(Text)

    # Marktsignal. Fehlt eine Zahl, bleibt sie None - nicht 0.
    signal: Mapped[Optional[float]] = mapped_column()
    signal_grund: Mapped[Optional[str]] = mapped_column(String(255))
    verkauft: Mapped[Optional[int]] = mapped_column()
    bewertungen: Mapped[Optional[int]] = mapped_column()
    platz: Mapped[Optional[int]] = mapped_column()

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="neu")
    eigener_prompt: Mapped[Optional[str]] = mapped_column(Text)
    design_id: Mapped[Optional[int]] = mapped_column(ForeignKey("studio_designs.id"))
    # Ergebnis von ``produktweg.pruefe`` als JSON. EINMAL beim Erzeugen
    # geschrieben statt bei jedem Listenaufruf gerechnet: die Pruefung oeffnet
    # und trimmt die PNG-Datei - bei 200 Karten waere das derselbe
    # Speicherfresser wie die Vorschaubilder im August.
    druckcheck: Mapped[Optional[str]] = mapped_column(Text)
    notiz: Mapped[Optional[str]] = mapped_column(Text)


# POD-Betrieb: kanalneutral, ohne Lieferantenimport oder Auto-Fulfillment
# --------------------------------------------------------------------------
class PodProduct(TimestampMixin, Base):
    """Ein eigenes, druckbares Produkt auf Basis eines Studio-Motivs."""

    __tablename__ = "pod_products"
    __table_args__ = (Index("idx_pod_product_status", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    design_id: Mapped[Optional[int]] = mapped_column(ForeignKey("studio_designs.id"))
    # Welches Produkt aus dem Katalog (app/studio/ebay_weg.py): tshirt, polo,
    # oversize, hoodie, tasse. Ohne dieses Feld liessen sich T-Shirt- und
    # Tassen-Angebot DESSELBEN Motivs nicht auseinanderhalten.
    produktart: Mapped[Optional[str]] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    provider: Mapped[Optional[str]] = mapped_column(String(40))
    provider_product_id: Mapped[Optional[str]] = mapped_column(String(120))
    base_cost_eur: Mapped[Optional[float]] = mapped_column()
    target_price_eur: Mapped[Optional[float]] = mapped_column()
    stock_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="on_demand")
    note: Mapped[Optional[str]] = mapped_column(Text)


class PodListing(TimestampMixin, Base):
    """Ein Verkaufsangebot eines POD-Produkts auf einem Vertriebskanal."""

    __tablename__ = "pod_listings"
    __table_args__ = (Index("idx_pod_listing_channel", "channel", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("pod_products.id"), nullable=False)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    external_id: Mapped[Optional[str]] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    price_eur: Mapped[Optional[float]] = mapped_column()
    quantity_available: Mapped[Optional[int]] = mapped_column()
    url: Mapped[Optional[str]] = mapped_column(String(1024))
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class PodOrder(TimestampMixin, Base):
    """Kanalunabhaengige Bestellung; jede Ausfuehrung bleibt manuell bestaetigt."""

    __tablename__ = "pod_orders"
    __table_args__ = (Index("idx_pod_order_channel", "channel", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[Optional[int]] = mapped_column(ForeignKey("pod_listings.id"))
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    external_id: Mapped[Optional[str]] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="new")
    sale_total_eur: Mapped[Optional[float]] = mapped_column()
    fulfillment_cost_eur: Mapped[Optional[float]] = mapped_column()
    ordered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    note: Mapped[Optional[str]] = mapped_column(Text)
    # Was genau bestellt wurde (Titel, Menge, Produktart, Farbe, Groesse, Motiv-Nummer) als JSON.
    positionen_json: Mapped[Optional[str]] = mapped_column(Text)
    # Name + Anschrift + E-Mail des Kaeufers (aus der eBay-Bestellung) als JSON -
    # fuer die selbst ausgestellte Verkaufsrechnung. Keine eigene Tabelle: es ist
    # ein Feld, das nur beim Rechnungdruck gebraucht wird.
    kaeufer_json: Mapped[Optional[str]] = mapped_column(Text)


class PodLedgerEntry(TimestampMixin, Base):
    """Manuell oder aus einem bestätigten Kanalimport erfasster Buchungsposten."""

    __tablename__ = "pod_ledger_entries"
    __table_args__ = (Index("idx_pod_ledger_kind", "kind", "occurred_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    amount_eur: Mapped[float] = mapped_column(nullable=False)
    occurred_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    reference: Mapped[Optional[str]] = mapped_column(String(255))
    note: Mapped[Optional[str]] = mapped_column(Text)


class StudioPreis(TimestampMixin, Base):
    """Vom Betreiber im Studio eingestellter Verkaufspreis je Produktart.

    Steht hier ein Eintrag, gilt er vor ``EBAY_PREISE`` und vor dem Katalogpreis
    (``ebay_weg.preis``). Ohne Eintrag bleibt alles wie im Katalog.
    """

    __tablename__ = "studio_preise"

    produkt: Mapped[str] = mapped_column(String(30), primary_key=True)
    preis_eur: Mapped[float] = mapped_column(nullable=False)

class StudioAngebotOption(TimestampMixin, Base):
    """Einstellungen fuer EIN eingestelltes Angebot (Motiv x Produktart).

    ``preis_eur`` ueberschreibt den Preis der Produktart; ``farben_aus`` und
    ``groessen_aus`` sind JSON-Listen dessen, was bei diesem Angebot nicht mehr
    verfuegbar sein soll. Gilt beim naechsten Einstellen/Aktualisieren bei eBay.
    """

    __tablename__ = "studio_angebot_optionen"

    design_id: Mapped[int] = mapped_column(ForeignKey("studio_designs.id"), primary_key=True)
    produkt: Mapped[str] = mapped_column(String(30), primary_key=True)
    preis_eur: Mapped[Optional[float]] = mapped_column()
    farben_aus: Mapped[Optional[str]] = mapped_column(Text)
    groessen_aus: Mapped[Optional[str]] = mapped_column(Text)
