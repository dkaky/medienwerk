"""Vom Motiv zum eBay-Angebot: fuenf Produkte, 8 Farben, echte Produktfotos.

**Katalog (Preise inkl. Versand, Vorgabe des Betreibers vom 13.09.2026):**

========  ==================  =========  ==================================
Produkt   eBay-Kategorie      Preis      Varianten
========  ==================  =========  ==================================
tshirt    15687 T-Shirts      14,90 €    Farbe x Groesse (XS-3XL)
polo      185101 Poloshirts   17,90 €    Farbe x Groesse (XS-3XL)
oversize  15687 T-Shirts      34,90 €    Farbe x Groesse (S-3XL)
hoodie    155183 Kapuzenp.    34,90 €    Farbe x Groesse (XS-3XL)
tasse     20695 Tassen        11,90 €    keine (Weiss, Einzelangebot)
========  ==================  =========  ==================================

Kategorien und Merkmalswerte stammen aus eBays Taxonomie (EBAY_DE, abgefragt
13.09.2026). Fundstuecke daraus:

* Groessen heissen ``2XL``/``3XL``, nicht ``XXL``.
* Der Hoodie steht in "Sport-Kapuzenpullover & -Sweatshirts" (155183); in
  "Pullover & Strick" (11484) ist als Produktart nur "Pullover" erlaubt.
* "Oversize" ist kein Listenwert der Passform, die Passform nimmt aber freien Text.

**Farben und Fotos** kommen aus ``mockup_plan`` (B&C #E190, B&C ID.001,
Build Your Brand Heavy Oversize Tee, B&C ID.333 Hoodie). Textilien brauchen
echte Fotos: flach von vorne plus je ein Mann- und Frau-Model. Erste Wahl ist
die lokale Montage auf eigenen Chroma-Key-Vorlagen (``mockup_montage``, jede
Farbe mit allen Ansichten, kostenlos); Dynamic Mockups bleibt als Ausweich.
Gezeichnete Textilbilder gehen nicht mehr zu eBay. Gerenderte Fotos werden
zwischengespeichert.

**Bildreihenfolge** (Vorgabe des Betreibers): je Farbe 1. die Ware allein,
2. am Mann, 3. an der Frau, danach dieselben drei von hinten - alles Mockups mit
dem aktuellen Motiv, kein separates Motivbild. Welches Motiv vorne und hinten
sitzt, legt ``druckseiten`` fest; eine Seite ohne Motiv wird unbedruckt gezeigt,
und ist nur hinten bedruckt, kommt die Rueckseite zuerst. Die Galerie des
Angebots beginnt mit diesen Bildern in der Hauptfarbe, danach je Farbe das erste.

**Material** nennt nur das T-Shirt (Angabe des Betreibers), und zwar je Farbe in
der Beschreibung: B&C Sport Grey ist 85 % Baumwolle / 15 % Viskose. Ein
pauschales Merkmal "Baumwolle" waere fuer graue Shirts falsch.

**Wiederholbar.** Artikel und Gruppe werden per PUT ersetzt, ein vorhandenes
Angebot erkennt der Client und aktualisiert es.

**Automatik** (``auto_nach_erzeugung``) laeuft nur mit
``EBAY_AUTO_VEROEFFENTLICHEN=true`` UND ``MOCK_EBAY=false``.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw
from sqlalchemy import select

from app.config import Settings, get_settings
from app.studio import mockup_plan
from app.studio.models import PodListing, PodProduct

logger = logging.getLogger("app.studio.ebay_weg")

KANAL = "ebay"
BILD_ORDNER = "ebay"
MOCKUP_ORDNER = "mockups"
_BILD_KANTE = 1600
_MAX_BILDER = 12             # eBay: hoechstens 12 Bilder je Artikel/Gruppe

_PROMPT_REST = re.compile(
    r"\b(freigestellt|transparent\w*|hintergrund|druckfertig\w*|hochaufl\w*|"
    r"illustration|vektor\w*|zentriert|stil)\b", re.IGNORECASE)


class EbayWegFehler(RuntimeError):
    """Das Motiv laesst sich (noch) nicht bei eBay einstellen."""


@dataclass(frozen=True)
class Produkt:
    key: str
    label: str
    kategorie_id: str
    preis_eur: float
    textil: bool
    form: str
    titel_wort: str
    merkmale: dict[str, list[str]]
    stichpunkte: tuple[str, ...]
    groessen: tuple[str, ...] | None = None      # None -> EBAY_TEXTIL_GROESSEN
    material_je_farbe: bool = False              # Material aus mockup_plan.FARBEN nennen


PRODUKTE: dict[str, Produkt] = {
    "tshirt": Produkt(
        "tshirt", "T-Shirt", "15687", 14.90, True, "shirt", "T-Shirt",
        {"Produktart": ["T-Shirt"], "Abteilung": ["Unisex Erwachsene"], "Ärmellänge": ["Kurzarm"]},
        ("Klassischer Schnitt", "Kurzarm"), material_je_farbe=True),
    "polo": Produkt(
        "polo", "Poloshirt", "185101", 17.90, True, "polo", "Poloshirt",
        {"Produktart": ["Polo"], "Abteilung": ["Unisex Erwachsene"], "Ärmellänge": ["Kurzarm"]},
        ("Polokragen", "Kurzarm")),
    "oversize": Produkt(
        "oversize", "Oversize T-Shirt", "15687", 34.90, True, "oversize", "Oversize T-Shirt",
        {"Produktart": ["T-Shirt"], "Abteilung": ["Unisex Erwachsene"],
         "Passform": ["Oversize"], "Ärmellänge": ["Kurzarm"]},
        ("Oversize-Schnitt", "Kurzarm"),
        # Eigene Produktlinie mit eigenem Groessenlauf (Betreiber, 13.09.2026).
        groessen=("S", "M", "L", "XL", "2XL", "3XL")),
    "hoodie": Produkt(
        "hoodie", "Hoodie", "155183", 34.90, True, "hoodie", "Hoodie",
        {"Produktart": ["Kapuzenpullover"], "Stil": ["Pullover"],
         "Abteilung": ["Unisex Erwachsene"], "Ärmellänge": ["Langarm"]},
        ("Mit Kapuze", "Langarm")),
    "tasse": Produkt(
        "tasse", "Tasse", "20695", 11.90, False, "tasse", "Tasse",
        {"Produktart": ["Kaffeetasse"]},
        ("Bedruckt mit eigenem Motiv",)),
}

TASSENFARBE = "Weiß"


@dataclass
class Bereitschaft:
    bereit: bool
    fehlt: list[str] = field(default_factory=list)
    probebetrieb: bool = True


# --------------------------------------------------------------------------
# Angebotsdaten
# --------------------------------------------------------------------------
def produkt(key: str) -> Produkt:
    p = PRODUKTE.get(key)
    if p is None:
        raise EbayWegFehler(f"Unbekanntes Produkt '{key}'. Moeglich: {', '.join(PRODUKTE)}")
    return p


def groessen(p: Produkt, s: Settings) -> list[str]:
    if not p.textil:
        return []
    if p.groessen:
        return list(p.groessen)
    return [g.strip() for g in (s.ebay_textil_groessen or "").split(",") if g.strip()]


def farben(p: Produkt) -> list[str]:
    return [f.name for f in mockup_plan.FARBEN] if p.textil else [TASSENFARBE]


def hauptfarbe(s: Settings) -> str:
    return mockup_plan.farbe(s.ebay_produktfarbe or "Weiß").name


def preis(p: Produkt, s: Settings) -> float:
    """Katalogpreis, ausser EBAY_PREISE ueberschreibt ihn fuer dieses Produkt."""
    for teil in (s.ebay_preise or "").split(","):
        name, _, wert = teil.partition("=")
        if name.strip() == p.key and wert.strip():
            try:
                return round(float(wert.strip()), 2)
            except ValueError:
                break
    return round(p.preis_eur, 2)


def _farbcode(farbname: str) -> str:
    """ASCII-Kuerzel fuer Artikelnummern ("Flaschengrün" -> "BottleGreen")."""
    return mockup_plan.farbe(farbname).hersteller.replace(" ", "")


def sku(design_id: int, p: Produkt, groesse: str | None = None, farbe: str | None = None) -> str:
    teile = [f"MW-{int(design_id)}-{p.key}"]
    if farbe:
        teile.append(_farbcode(farbe))
    if groesse:
        teile.append(groesse)
    return "-".join(teile)


def gruppe(design_id: int, p: Produkt) -> str:
    return f"MW-{int(design_id)}-{p.key}-GRP"


def freigabe_schluessel(design_id: int) -> int:
    """Negativ, damit er nie mit Listing-IDs des alten Handelsteils kollidiert."""
    return -int(design_id)


def motivname(design: Any) -> str:
    roh = (getattr(design, "title", None) or "Motiv").strip()
    kern = re.split(r"[.,;:]", roh, maxsplit=1)[0]
    kern = _PROMPT_REST.sub(" ", kern)
    kern = re.sub(r"\s{2,}", " ", kern).strip(" -")
    return kern or "Motiv"


def titel(design: Any, p: Produkt) -> str:
    zusatz = "Kaffeetasse Motiv Geschenk" if p.key == "tasse" else "Unisex Motiv"
    voll = f"{p.titel_wort} {motivname(design)} {zusatz}"
    if len(voll) <= 80:
        return voll
    return voll[:80].rsplit(" ", 1)[0]


def beschreibung(design: Any, p: Produkt, s: Settings, druck: str | None = None) -> str:
    punkte = list(p.stichpunkte)
    if p.material_je_farbe:
        materialien: dict[str, list[str]] = {}
        for f in mockup_plan.FARBEN:
            materialien.setdefault(f.material, []).append(f.name)
        haupt = max(materialien, key=lambda m: len(materialien[m]))
        text = f"Material: {haupt}"
        rest = [f"{', '.join(n)}: {m}" for m, n in materialien.items() if m != haupt]
        punkte.insert(0, text + (f" ({'; '.join(rest)})" if rest else ""))
    if p.textil:
        punkte.append(f"Farben: {', '.join(farben(p))}")
        punkte.append(f"Größen: {', '.join(groessen(p, s))}")
    if druck:
        punkte.append(druck)
    punkte += ["Eigenes Motiv, auf Bestellung gedruckt", "Preis inkl. Versand"]
    liste = "".join(f"<li>{x}</li>" for x in punkte)
    return (f"<h2>{motivname(design)} – {p.label}</h2>"
            f"<p>Ein eigenes Motiv von {s.ebay_marke or s.seller_name or 'uns'}.</p>"
            f"<ul>{liste}</ul>")


def basis_merkmale(p: Produkt, s: Settings) -> dict[str, list[str]]:
    return {"Marke": [s.ebay_marke or "Markenlos"], **{k: list(v) for k, v in p.merkmale.items()}}


def marke(s: Settings) -> str:
    return (s.ebay_marke or "Markenlos").strip() or "Markenlos"


def mpn(design_id: int, p: Produkt) -> str:
    return f"MW-{int(design_id)}-{p.key}"


# --------------------------------------------------------------------------
# Gezeichnete Notbilder
# --------------------------------------------------------------------------
_HINTERGRUND = (238, 238, 236)
_KANTE = (190, 190, 188)
_SHIRT = [(540, 290), (330, 380), (150, 640), (330, 760), (420, 660), (420, 1450),
          (1180, 1450), (1180, 660), (1270, 760), (1450, 640), (1270, 380), (1060, 290),
          (960, 345), (800, 375), (640, 345)]
_OVERSIZE = [(520, 280), (260, 380), (80, 760), (300, 880), (380, 720), (360, 1480),
             (1240, 1480), (1220, 720), (1300, 880), (1520, 760), (1340, 380), (1080, 280),
             (970, 340), (800, 370), (630, 340)]
_HOODIE = [(560, 330), (320, 420), (130, 1300), (290, 1340), (420, 760), (420, 1470),
           (1180, 1470), (1180, 760), (1310, 1340), (1470, 1300), (1280, 420), (1040, 330)]


def _rgb(hexwert: str) -> tuple[int, int, int]:
    h = (hexwert or "").lstrip("#")
    if len(h) != 6:
        return (255, 255, 255)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _polygon(z: ImageDraw.ImageDraw, punkte, farbe) -> None:
    z.polygon(punkte, fill=farbe, outline=_KANTE)
    z.line(list(punkte) + [punkte[0]], fill=_KANTE, width=6)


def _ware(form: str, hexwert: str) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """Leinwand mit gezeichneter Ware + Druckfeld (Mitte x, oben y, max. Breite, max. Hoehe)."""
    bild = Image.new("RGB", (_BILD_KANTE, _BILD_KANTE), _HINTERGRUND)
    z = ImageDraw.Draw(bild)
    stoff = _rgb(hexwert)
    if form == "oversize":
        _polygon(z, _OVERSIZE, stoff)
        return bild, (800, 500, 560, 620)
    if form == "hoodie":
        z.ellipse((560, 150, 1040, 520), fill=stoff, outline=_KANTE, width=6)
        _polygon(z, _HOODIE, stoff)
        z.rectangle((560, 1120, 1040, 1330), outline=_KANTE, width=5)
        z.line((720, 380, 700, 620), fill=_KANTE, width=4)
        z.line((880, 380, 900, 620), fill=_KANTE, width=4)
        return bild, (800, 600, 460, 460)
    if form == "tasse":
        z.rounded_rectangle((440, 380, 1100, 1260), radius=40, fill=stoff, outline=_KANTE, width=6)
        z.arc((1000, 560, 1340, 1040), start=-90, end=90, fill=_KANTE, width=48)
        z.arc((1000, 560, 1340, 1040), start=-90, end=90, fill=stoff, width=36)
        return bild, (770, 520, 520, 600)
    _polygon(z, _SHIRT, stoff)
    if form == "polo":
        z.polygon([(640, 345), (800, 375), (760, 470)], fill=stoff, outline=_KANTE)
        z.polygon([(960, 345), (800, 375), (840, 470)], fill=stoff, outline=_KANTE)
        z.line((800, 375, 800, 600), fill=_KANTE, width=5)
        return bild, (800, 640, 420, 460)
    return bild, (800, 480, 480, 560)


def _einpassen(motiv: Image.Image, breite: int, hoehe: int) -> Image.Image:
    motiv = motiv.convert("RGBA")
    rand = motiv.getbbox()
    if rand:
        motiv = motiv.crop(rand)
    motiv.thumbnail((breite, hoehe), Image.LANCZOS)
    return motiv


def zeichne_ware(motiv_pfad: Path, ziel: Path, p: Produkt, hexwert: str) -> Path:
    """Notbild: Motiv auf gezeichneter Ware in der gegebenen Farbe. JPEG, 1600 px."""
    ziel.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(motiv_pfad) as roh:
        motiv = roh.convert("RGBA")
    ware, (mitte, oben, max_b, max_h) = _ware(p.form, hexwert)
    druck = _einpassen(motiv, max_b, max_h)
    ware.paste(druck, (mitte - druck.width // 2, oben), druck)
    ware.save(ziel, "JPEG", quality=92)
    return ziel


# --------------------------------------------------------------------------
# Produktfotos: eigene Vorlagen (Montage), Dynamic Mockups, Tasse mit Notbild
# --------------------------------------------------------------------------
def montage_fehlt(p: Produkt, s: Settings) -> list[str]:
    """Welche eigenen Vorlagen fuer die lokale Montage fehlen (leer = alles da)."""
    from app.studio import mockup_montage

    return mockup_montage.fehlende_vorlagen(p.key, textil=p.textil,
                                            ordner=Path(s.mockup_montage_ordner))


def fotoquelle(p: Produkt, s: Settings) -> tuple[str, list[str]]:
    """Woher die Fotos kaemen - ohne Netz, ohne Credits."""
    if not montage_fehlt(p, s):
        return "montage", []
    plan = mockup_plan.plane(p.key, textil=p.textil,
                             vorlagen=mockup_plan.lade_vorlagen(s.mockup_vorlagen_datei),
                             hauptfarbe=hauptfarbe(s))
    if not s.dynamic_mockups_api_key:
        if p.textil:
            return "blockiert", montage_fehlt(p, s)
        return "gezeichnet", ["DYNAMIC_MOCKUPS_API_KEY fehlt in der .env"]
    if not plan.auftraege:
        return ("blockiert" if p.textil else "gezeichnet"), plan.fehlt
    return ("mockups" if not plan.fehlt else "mockups-teilweise"), plan.fehlt


def pruefe_textilfotos(p: Produkt, s: Settings) -> list[str]:
    """Warum dieses Textil nicht mit echten Fotos erstellt werden kann."""
    if not p.textil:
        return []
    montage = montage_fehlt(p, s)
    if not montage:
        return []
    fehlt: list[str] = []
    if not s.dynamic_mockups_api_key:
        fehlt.append("DYNAMIC_MOCKUPS_API_KEY fehlt in der .env")
    fehlt.extend(mockup_plan.textil_vollstaendig(
        p.key,
        vorlagen=mockup_plan.lade_vorlagen(s.mockup_vorlagen_datei),
        hauptfarbe=hauptfarbe(s),
    ))
    # Dynamic Mockups vollstaendig eingerichtet -> reicht als Ausweich.
    return (montage + fehlt) if fehlt else []


async def produktfotos(design: Any, p: Produkt, *, s: Settings, bildordner: Path,
                       motiv: Path | None, mockups: Any = None,
                       hinten: Path | None = None) -> dict[str, Any]:
    """Bilder je Farbe. Rendert nur, was noch nicht im Zwischenspeicher liegt."""
    textil_fehlt = pruefe_textilfotos(p, s)
    if textil_fehlt:
        raise EbayWegFehler(
            "Echte Textilfotos fehlen: " + "; ".join(textil_fehlt)
            + ". Bitte eigene Vorlagen (vorne, mann, frau, hinten, mann_hinten, frau_hinten) "
            "in MOCKUP_MONTAGE_ORDNER ablegen."
        )
    haupt = hauptfarbe(s) if p.textil else TASSENFARBE
    je_farbe: dict[str, list[Path]] = {f: [] for f in farben(p)}
    quelle, fehlt = fotoquelle(p, s)
    gerendert = 0
    ordner = bildordner / BILD_ORDNER / str(design.id)

    if quelle == "montage":
        import asyncio

        from app.studio import mockup_montage

        ziel = bildordner / MOCKUP_ORDNER / str(design.id)
        vorher = set(ziel.glob("*.jpg")) if ziel.is_dir() else set()
        farbwerte = [(f, mockup_plan.farbe(f).hex if p.textil else "#FFFFFF") for f in farben(p)]
        fotos = await asyncio.to_thread(
            mockup_montage.rendere, motiv, hinten=hinten, produkt=p.key, textil=p.textil, farben=farbwerte,
            ziel_ordner=ziel, ordner=Path(s.mockup_montage_ordner))
        gerendert = len({x for liste in fotos.values() for x in liste} - vorher)
        return {"je_farbe": fotos, "quelle": "montage", "fehlt": [], "gerendert": gerendert}

    motiv = motiv or hinten                  # Dynamic Mockups und Notbild kennen nur eine Seite
    plan = mockup_plan.plane(p.key, textil=p.textil,
                             vorlagen=mockup_plan.lade_vorlagen(s.mockup_vorlagen_datei),
                             hauptfarbe=haupt if p.textil else "Weiß")
    if plan.auftraege and (mockups is not None or s.dynamic_mockups_api_key):
        from app.integrations.dynamic_mockups import DynamicMockupsClient

        client = mockups or DynamicMockupsClient(s.dynamic_mockups_api_key)
        try:
            for a in plan.auftraege:
                ziel = bildordner / MOCKUP_ORDNER / str(design.id) / f"{a.label}.jpg"
                if not ziel.is_file():
                    adresse = await client.rendere(
                        mockup_uuid=a.mockup_uuid, motiv_objekt=a.motiv_objekt, motiv_datei=motiv,
                        farb_objekt=a.farb_objekt, farbe_hex=a.farbe.hex if a.farbe else None,
                        label=a.label)
                    await client.lade_herunter(adresse, ziel)
                    gerendert += 1
                je_farbe.setdefault(a.farbe.name if a.farbe else haupt, []).append(ziel)
        finally:
            if mockups is None:
                await client.aclose()
        quelle = "mockups" if not plan.fehlt else "mockups-teilweise"
        fehlt = plan.fehlt

    for f in farben(p):
        if not je_farbe.get(f):
            hexwert = mockup_plan.farbe(f).hex if p.textil else "#FFFFFF"
            je_farbe[f] = [zeichne_ware(motiv, ordner / f"{p.key}-{_farbcode(f)}.jpg", p, hexwert)]
    return {"je_farbe": je_farbe, "quelle": quelle, "fehlt": fehlt, "gerendert": gerendert}


# --------------------------------------------------------------------------
# Pruefen und Festhalten
# --------------------------------------------------------------------------
def pruefe(design: Any, *, s: Settings, bildordner: Path) -> Bereitschaft:
    """Ginge es? Ohne Netz, aendert nichts. Gilt fuer alle Produkte gleich."""
    from app.studio import produktweg

    fehlt = []
    try:
        produktweg.bildpfad(design, bildordner)
    except produktweg.MotivFehler as exc:
        fehlt.append(str(exc))
    for name, wert in (("EBAY_CLIENT_ID", s.ebay_client_id),
                       ("EBAY_CLIENT_SECRET", s.ebay_client_secret),
                       ("EBAY_REFRESH_TOKEN (eBay-Zugang verknuepfen)", s.ebay_refresh_token),
                       ("EBAY_WAREHOUSE_POSTAL (Versand-PLZ)", s.ebay_warehouse_postal),
                       ("EBAY_WAREHOUSE_CITY (Versandort)", s.ebay_warehouse_city)):
        if not wert:
            fehlt.append(f"{name} fehlt in der .env")
    if not [g for g in (s.ebay_textil_groessen or "").split(",") if g.strip()]:
        fehlt.append("Keine Groessen (EBAY_TEXTIL_GROESSEN)")
    try:
        hauptfarbe(s)
    except ValueError as exc:
        fehlt.append(f"EBAY_PRODUKTFARBE: {exc}")
    return Bereitschaft(bereit=not fehlt, fehlt=fehlt, probebetrieb=s.use_mock("ebay"))


def aktives_angebot(db, design_id: int, produkt_key: str) -> PodListing | None:
    return db.scalars(
        select(PodListing).join(PodProduct, PodListing.product_id == PodProduct.id)
        .where(PodProduct.design_id == design_id, PodProduct.produktart == produkt_key,
               PodListing.channel == KANAL, PodListing.status == "active")
    ).first()


def produkt_fuer(db, design: Any, p: Produkt) -> PodProduct:
    eintrag = db.scalars(select(PodProduct).where(PodProduct.design_id == design.id,
                                                  PodProduct.produktart == p.key)).first()
    if eintrag is None:
        eintrag = PodProduct(design_id=design.id, produktart=p.key, title=titel(design, p),
                             status="draft", provider="eigen")
        db.add(eintrag)
        db.flush()
    return eintrag


def vermerke(db, design: Any, p: Produkt, status: str, notiz: str) -> None:
    eintrag = produkt_fuer(db, design, p)
    eintrag.status = status
    eintrag.note = notiz[:500]
    db.commit()


# --------------------------------------------------------------------------
# Veroeffentlichen
# --------------------------------------------------------------------------
async def veroeffentliche(db, design: Any, *, produkt_key: str, ebay: Any, s: Settings,
                          bildordner: Path, mockups: Any = None) -> dict[str, Any]:
    p = produkt(produkt_key)
    vorhanden = aktives_angebot(db, design.id, p.key)
    if vorhanden is not None:
        return {"produkt": p.key, "listing_id": vorhanden.external_id, "url": vorhanden.url,
                "schon_vorhanden": True, "automatisch_ergaenzt": []}

    bereitschaft = pruefe(design, s=s, bildordner=bildordner)
    if not bereitschaft.bereit:
        raise EbayWegFehler("Noch nicht bereit: " + "; ".join(bereitschaft.fehlt))

    from app.studio import druckseiten

    eintrag = produkt_fuer(db, design, p)
    betrag, menge, gr = preis(p, s), int(s.ebay_menge_je_variante), groessen(p, s)
    haupt = hauptfarbe(s) if p.textil else TASSENFARBE
    try:
        seiten = druckseiten.lese(db, design)
        vorne_pfad, hinten_pfad = druckseiten.pfade(seiten, bildordner)
        if vorne_pfad is None and hinten_pfad is None:
            raise EbayWegFehler("Weder Vorder- noch Rueckseite hat ein Motiv.")
        fotos = await produktfotos(design, p, s=s, bildordner=bildordner, motiv=vorne_pfad,
                                   hinten=hinten_pfad, mockups=mockups)
        adressen = {f: [await ebay.upload_image(x) for x in pfade[:_MAX_BILDER]]
                    for f, pfade in fotos["je_farbe"].items()}

        basis = basis_merkmale(p, s)
        merkmale = await ebay.build_aspects(p.kategorie_id, basis)
        for achse in ("Größe", "Farbe"):           # tragen die Varianten selbst
            merkmale.pop(achse, None)
        ergaenzt = sorted(set(merkmale) - set(basis))
        t = titel(design, p)
        text = beschreibung(design, p, s, druck=seiten.beschreibung if p.textil else None)

        if p.textil:
            skus = []
            for f in farben(p):
                for g in gr:
                    nummer = sku(design.id, p, g, farbe=f)
                    await ebay.create_inventory_item(
                        nummer, title=t, description=text, image_urls=adressen[f][:_MAX_BILDER],
                        quantity=menge, aspects={**merkmale, "Farbe": [f], "Größe": [g]},
                        brand=marke(s), mpn=nummer)
                    skus.append(nummer)
            gruppenbilder = (adressen[haupt]
                             + [adressen[f][0] for f in farben(p) if f != haupt])[:_MAX_BILDER]
            await ebay.create_inventory_item_group(
                gruppe(design.id, p), title=t, description=text, image_urls=gruppenbilder,
                variant_skus=skus,
                specifications=[{"name": "Farbe", "values": farben(p)},
                                {"name": "Größe", "values": gr}],
                image_varies_by=["Farbe"], aspects=merkmale)
            for nummer in skus:
                await ebay.create_offer(nummer, price_eur=betrag, category_id=p.kategorie_id,
                                        quantity=menge, listing_description=text)
            listing_id = await ebay.publish_offer_by_inventory_item_group(gruppe(design.id, p))
            bestand = menge * len(skus)
        else:
            nummer = sku(design.id, p)
            await ebay.create_inventory_item(
                nummer, title=t, description=text,
                image_urls=adressen[TASSENFARBE][:_MAX_BILDER], quantity=menge,
                aspects={**merkmale, "Farbe": [TASSENFARBE]}, brand=marke(s), mpn=mpn(design.id, p))
            angebot_id = await ebay.create_offer(nummer, price_eur=betrag,
                                                 category_id=p.kategorie_id, quantity=menge,
                                                 listing_description=text)
            listing_id = await ebay.publish_listing(angebot_id, title=t, category_id=p.kategorie_id)
            bestand = menge
    except Exception as exc:
        eintrag.status = "fehler"
        eintrag.note = f"eBay: {exc}"[:500]
        db.commit()
        raise

    url = f"https://www.ebay.de/itm/{listing_id}"
    hinweise = []
    if ergaenzt:
        hinweise.append("Von eBay vorgegebene Merkmale pruefen: " + ", ".join(ergaenzt))
    if fotos["fehlt"]:
        hinweise.append("Fotos: " + "; ".join(fotos["fehlt"]))
    eintrag.status = "active"
    eintrag.title = t
    eintrag.target_price_eur = betrag
    eintrag.note = (" | ".join(hinweise))[:500] or None
    db.add(PodListing(product_id=eintrag.id, channel=KANAL, external_id=listing_id,
                      status="active", price_eur=betrag, quantity_available=bestand, url=url))
    db.commit()
    logger.info("Motiv bei eBay eingestellt",
                extra={"design": design.id, "produkt": p.key, "listing": listing_id,
                       "fotos": fotos["quelle"], "gerendert": fotos["gerendert"]})
    return {"produkt": p.key, "listing_id": listing_id, "url": url, "schon_vorhanden": False,
            "automatisch_ergaenzt": ergaenzt, "bildquelle": fotos["quelle"],
            "fehlende_vorlagen": fotos["fehlt"], "gerendert": fotos["gerendert"]}


def ansage_nach_erzeugung(design: Any, *, s: Settings, bildordner: Path) -> str | None:
    """Ein Satz fuer die Meldung nach dem Erzeugen - was mit eBay passiert."""
    if not s.ebay_auto_veroeffentlichen:
        return None
    if s.use_mock("ebay"):
        return "eBay: Probebetrieb (MOCK_EBAY) – nicht automatisch eingestellt, Knopf „eBay“ nutzen."
    b = pruefe(design, s=s, bildordner=bildordner)
    if not b.bereit:
        return "eBay: noch nicht bereit – " + "; ".join(b.fehlt)
    return "eBay: wird automatisch eingestellt."


async def auto_nach_erzeugung(design_id: int) -> None:
    """Hintergrundaufgabe nach dem Erzeugen: alle Produkte. Stirbt nie laut - sie vermerkt."""
    from app.database import SessionLocal
    from app.studio import service

    s = get_settings()
    if not s.ebay_auto_veroeffentlichen:
        return
    bildordner = Path(s.studio_image_dir)
    db = SessionLocal()
    try:
        design = service.get_design(db, design_id)
        if design is None:
            return
        if s.use_mock("ebay"):
            for p in PRODUKTE.values():
                vermerke(db, design, p, "wartet",
                         "Probebetrieb (MOCK_EBAY=true): automatisch nicht bei eBay eingestellt.")
            return
        b = pruefe(design, s=s, bildordner=bildordner)
        if not b.bereit:
            for p in PRODUKTE.values():
                vermerke(db, design, p, "wartet", "eBay nicht bereit: " + "; ".join(b.fehlt))
            return
        from app.integrations.ebay import RealEbayClient

        ebay = RealEbayClient(s)
        try:
            for p in PRODUKTE.values():
                try:
                    await veroeffentliche(db, design, produkt_key=p.key, ebay=ebay, s=s,
                                          bildordner=bildordner)
                except Exception as exc:  # noqa: BLE001 - Fehler steht schon am Produkt
                    logger.error("automatisches eBay-Einstellen gescheitert",
                                 extra={"design": design_id, "produkt": p.key,
                                        "error": str(exc)[:200]})
        finally:
            if ebay._client is not None:
                await ebay._client.aclose()
    finally:
        db.close()
