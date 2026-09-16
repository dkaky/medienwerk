"""Ein- und Ausgabeformen der Studio-Endpunkte."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class DesignIn(BaseModel):
    """Ein neues Motiv anlegen. Erzeugt wird dabei noch nichts."""

    title: str = Field(min_length=1, max_length=255)
    source: Optional[str] = Field(default=None, max_length=30)
    image_url: Optional[str] = Field(default=None, max_length=1024)
    meta_json: Optional[str] = None


class DesignOut(BaseModel):
    """Ein Motiv, wie es das Dashboard anzeigt."""

    id: int
    title: str
    status: str
    source: Optional[str] = None
    image_url: Optional[str] = None
    meta_json: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class DesignStatusIn(BaseModel):
    """Entscheidung ueber ein Motiv. Geloescht wird bewusst nichts."""

    status: str = Field(pattern="^(draft|ready|archived)$")


class LinkIn(BaseModel):
    """Ein eBay-Angebot einem Motiv zuordnen.

    Diese Zuordnung macht den Riegel scharf: ab dann fasst keine
    Handels-Automatik das Angebot mehr an.
    """

    listing_id: int
    design_id: Optional[int] = None
    note: Optional[str] = None


class LinkOut(BaseModel):
    listing_id: int
    design_id: Optional[int] = None
    linked_at: Optional[datetime] = None
    note: Optional[str] = None

    model_config = {"from_attributes": True}


class StudioStatus(BaseModel):
    """Kurzuebersicht fuer das Dashboard."""
    tagesbudget_usd: float = 0.0
    verbraucht_heute_usd: float = 0.0
    rest_heute_usd: float | None = 0.0


    enabled: bool
    designs: int
    verknuepfte_angebote: int


class GenerateIn(BaseModel):
    """Auftrag fuer eine Bilderzeugung."""

    prompt: str = Field(min_length=3, max_length=1000)
    titel: str | None = Field(default=None, max_length=255)
    anbieter: str | None = Field(default=None, pattern="^(mock|openai|fal)$")
    # Freitext fuer die MACHART - "realistisch", "Comic", "Retro-Linien, zwei
    # Farben". Bewusst getrennt vom Motivfeld: das eine sagt, WAS zu sehen ist,
    # das andere WIE. In einem Feld vermischt sich beides. Die Angabe geht durch
    # denselben Rechte- und Motivart-Filter wie die Beschreibung.
    stil: str | None = Field(default=None, max_length=200)
    # Quadratisch als Vorgabe. Vorher stand hier Hochformat 1024x1536, ohne dass
    # es je waehlbar gewesen waere - daher sind alle 50 Altmotive hochkant. Das
    # Verhaeltnis 0,667 passt zu nichts: die Druckleinwand hat 0,833, und ein
    # Brustmotiv nutzt davon nur 10-12 von 15 Zoll Breite. Siehe erzeuge().
    breite: int = Field(default=1024, ge=256, le=2048)
    hoehe: int = Field(default=1024, ge=256, le=2048)


class VeredelnIn(BaseModel):
    """Auftrag fuer eine Promptveredelung. Erzeugt nichts."""

    idee: str = Field(min_length=3, max_length=1000)
    # Bestimmt Format und Aufloesungswarnung. Textil ist die Vorgabe, weil dort
    # der Grossteil der Ware liegt.
    ziel: str = Field(default="textil", max_length=40)


class VeredelnOut(BaseModel):
    """Der veredelte Prompt samt Pruefbericht.

    ``abbruch`` heisst: eine der harten Grenzen hat gehalten (fremde Rechte,
    Ware im Bild). Dann ist ``prompt`` leer und nur der Bericht traegt etwas -
    absichtlich, damit die Oberflaeche keinen halben Vorschlag anbietet.
    """

    prompt: str
    breite: int
    hoehe: int
    stil: str
    ziel: str
    bericht: list[str]
    abbruch: bool
    # "modell" oder "regeln" - der Betreiber soll sehen, ob das Sprachmodell
    # ueberhaupt beteiligt war. Ein regelbasierter Prompt sieht sonst genauso
    # fertig aus, ist aber deutlich schwaecher.
    quelle: str


class GenerateOut(BaseModel):
    """Ergebnis samt Kostenangabe - der Betreiber soll sehen, was es kostet."""

    design: DesignOut
    kosten_usd: float
    rest_budget_usd: float | None
    anbieter: str
    # Was mit eBay passiert (automatisch eingestellt, Probebetrieb, nicht bereit).
    # Leer, wenn die Automatik aus ist.
    ebay: str | None = None


# --------------------------------------------------------------------------
# Motiv-Radar
# --------------------------------------------------------------------------

class RadarLaufIn(BaseModel):
    """Einen fremden Shop lesen. Nur lesen - es entsteht kein Bild."""

    link: str = Field(min_length=4, max_length=500)
    limit: int = Field(default=40, ge=1, le=100)
    # Der beste Marktbeleg, den eBay hergibt - verlangt aber ein angemeldetes
    # Browser-Profil. Deshalb ausdruecklich anzuschalten, nicht als Vorgabe.
    nur_verkauft: bool = False


class IdeeOut(BaseModel):
    """Eine Beobachtung aus einem fremden Shop.

    ``fremdtitel`` steht als Beleg der Herkunft hier - was weiterverwendet
    werden darf, ist ``thema`` und ``stichworte``.
    """

    id: int
    quelle_plattform: str
    quelle_shop: Optional[str] = None
    quelle_url: Optional[str] = None
    fremdtitel: str
    # Das Produktfoto des fremden Artikels. Ohne dieses Feld stand das Motiv
    # zwar in der Datenbank, war in der Oberflaeche aber nicht zu sehen - man
    # entschied ueber einen Fund, ohne ihn anzusehen.
    bild_url: Optional[str] = None
    thema: Optional[str] = None
    stichworte: list[str] = []
    # Die ausfuehrliche Bildanalyse (``radar/beschreibung.py``). In der Spalte
    # steht JSON-TEXT, hier ein Objekt - der Router wandelt VOR der Pruefung um.
    beschreibung: dict = {}
    signal: Optional[float] = None
    signal_grund: Optional[str] = None
    verkauft: Optional[int] = None
    bewertungen: Optional[int] = None
    platz: Optional[int] = None
    status: str
    eigener_prompt: Optional[str] = None
    design_id: Optional[int] = None
    design_bild_url: Optional[str] = None
    # Ergebnis der Druckpruefung zum erzeugten Motiv, ebenfalls JSON in der Spalte.
    druckcheck: dict = {}
    notiz: Optional[str] = None

    model_config = {"from_attributes": True}


class IdeeStatusIn(BaseModel):
    """Uebernehmen oder verwerfen."""

    status: str = Field(pattern="^(neu|verworfen|uebernommen)$")
    notiz: Optional[str] = Field(default=None, max_length=2000)


class EntwurfIn(BaseModel):
    """Eigene Handschrift zum Entwurf - wird mitgeprueft."""

    zusatz: Optional[str] = Field(default=None, max_length=500)


class EntwurfOut(BaseModel):
    """Ein Vorschlag zum Lesen. Erzeugt wird nichts (Propose-only)."""

    idee_id: int
    prompt: str


class TrendLaufIn(BaseModel):
    """Trends im Netz suchen. Es entsteht kein Bild."""

    anzahl: int = Field(default=12, ge=3, le=20)
    kategorie: Literal[
        "mix", "sport", "zeichen", "anime", "gothic", "astronomie", "sonntag"
    ] = "mix"


class RadarErzeugenIn(BaseModel):
    """Aus einem Vorschlag ein Motiv erzeugen - der Klick, der Geld kostet."""

    anbieter: str = Field(default="openai", pattern="^(mock|openai|fal)$")


class NutzenIn(BaseModel):
    """Der eine Klick, der die ganze Kette ausloest.

    Dieser Klick IST die menschliche Freigabe (Eiserne Regel 1). Danach darf
    durchlaufen: Entwurf, Bild, Motiv, Druckpruefung. Nach Printify geht dabei
    nichts - das bleibt ein eigener Weg mit eigener Bestaetigung.
    """

    zusatz: Optional[str] = Field(default=None, max_length=500)
    # UNSER Schriftzug fuers Bild, nie der des fremden Shops. Wird geprueft.
    eigener_spruch: Optional[str] = Field(default=None, max_length=200)
    anbieter: str = Field(default="openai", pattern="^(mock|openai|fal)$")
    produkttyp: str = Field(default="tshirt", pattern="^(tshirt|hoodie|mug)$")


class NutzenOut(BaseModel):
    """Was aus dem Klick geworden ist - samt Kosten und Druckurteil."""

    idee: IdeeOut
    design_id: int
    bild_url: str
    prompt: str
    kosten_usd: float
    rest_budget_usd: float | None
    druckcheck: dict = {}


class EbeneIn(BaseModel):
    """Ein Motiv auf der Druckflaeche (Anteile der Flaeche, siehe app/studio/druckseiten.py)."""

    design_id: int
    mitte_x: float = Field(0.5, ge=0.0, le=1.0)
    oben: float = Field(0.0, ge=-0.5, le=1.0)
    groesse: float = Field(1.0, ge=0.05, le=1.5)


class DruckseitenIn(BaseModel):
    """Die Ebenen je Seite. Eine leere Liste = diese Seite bleibt unbedruckt."""

    vorne: list[EbeneIn] = Field(default_factory=list, max_length=5)
    hinten: list[EbeneIn] = Field(default_factory=list, max_length=5)
