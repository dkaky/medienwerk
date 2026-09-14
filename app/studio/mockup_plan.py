"""Welche Produktfotos je Motiv entstehen - Farben, Ansichten, Vorlagen.

Entscheidungen des Betreibers (13.09.2026):

* Rohlinge aus dem eigenen Einkauf:
  B&C #E190 T-Shirt, B&C ID.001 Polo, Build Your Brand Heavy Oversize Tee,
  B&C ID.333 Hoodie.
* **Sparsamer Fotoplan, rund 42 Bilder je Motiv** (Pro-Tarif: ~300 Bilder im Monat):
  je Textil ein realistischer Mann, eine realistische Frau und je Farbe ein
  echtes Produktfoto von vorne
  (2 + 8 = 10), mal 4 Textilien, plus 2 Tassenbilder.

**Die Farbtoene sind Naeherungen.** B&C veroeffentlicht keine Hex-Werte (Suche
beim Hersteller und bei Haendlern, 13.09.2026). Sie steuern nur, wie die Ware auf
dem Foto eingefaerbt wird. Wer ein Muster in der Hand hat, sollte sie danach
nachstellen - sonst sieht der Kaeufer ein anderes Rot, als er bekommt.

**Material:** B&C #E190 ist 100 % Baumwolle (ringgesponnen, 185 g/m²), AUSSER Sport Grey (85 % Baumwolle,
15 % Viskose, laut Haendlerangabe, geprueft 14.09.2026). Deshalb traegt die Farbe ihren Materialhinweis
selbst, statt dass ein Angebot pauschal "100 % Baumwolle" behauptet.

Die Zuordnung Produkt -> Ansicht -> Vorlage steht in einer JSON-Datei
(``MOCKUP_VORLAGEN_DATEI``), weil die Vorlagen im eigenen Dynamic-Mockups-Konto
ausgesucht werden und sich aendern koennen, ohne dass Code geaendert wird::

    {
      "tshirt": {
        "mann":  {"mockup_uuid": "...", "motiv_objekt": "...", "farb_objekt": "..."},
        "frau":  {"mockup_uuid": "...", "motiv_objekt": "...", "farb_objekt": "..."},
        "vorne": {"mockup_uuid": "...", "motiv_objekt": "...", "farb_objekt": "..."}
      },
      "tasse": {"vorne": {...}, "seite": {...}}
    }

Fuer Textilien sind diese Vorlagen PFLICHT. Gezeichnete Textilbilder waren nur
ein Entwicklungs-Notbehelf und werden nicht mehr zu eBay hochgeladen.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Farbe:
    name: str            # Variantenwert bei eBay, so sieht ihn der Kaeufer
    hersteller: str      # Name bei B&C
    hex: str             # NAEHERUNG fuer die Einfaerbung im Foto
    material: str        # Materialangabe fuer diese Farbe


FARBEN: tuple[Farbe, ...] = (
    Farbe("Weiß", "White", "#FFFFFF", "100 % Baumwolle"),
    Farbe("Schwarz", "Black", "#1B1B1B", "100 % Baumwolle"),
    Farbe("Grau meliert", "Sport Grey", "#A4A6A9", "85 % Baumwolle, 15 % Viskose"),
    Farbe("Navy", "Navy", "#1F2A44", "100 % Baumwolle"),
    Farbe("Rot", "Red", "#C21E2B", "100 % Baumwolle"),
    Farbe("Royalblau", "Royal Blue", "#1F4E9C", "100 % Baumwolle"),
    Farbe("Flaschengrün", "Bottle Green", "#1F4A2E", "100 % Baumwolle"),
    Farbe("Sand", "Sand", "#D6C4A6", "100 % Baumwolle"),
)

#: Ansicht -> welche Farben gerendert werden. "haupt" = nur die Hauptfarbe.
ANSICHTEN_TEXTIL: dict[str, str] = {"mann": "haupt", "frau": "haupt", "vorne": "alle"}
#: Tassen gibt es nur in Weiss.
ANSICHTEN_TASSE: tuple[str, ...] = ("vorne", "seite")


def farbe(name: str) -> Farbe:
    for f in FARBEN:
        if f.name.lower() == (name or "").strip().lower():
            return f
    raise ValueError(f"Unbekannte Farbe '{name}'. Moeglich: {', '.join(f.name for f in FARBEN)}")


@dataclass(frozen=True)
class Auftrag:
    """Ein zu renderndes Foto."""

    produkt: str
    ansicht: str
    farbe: Farbe | None
    mockup_uuid: str
    motiv_objekt: str
    farb_objekt: str | None

    @property
    def label(self) -> str:
        teil = f"-{self.farbe.hersteller.replace(' ', '')}" if self.farbe else ""
        return f"{self.produkt}-{self.ansicht}{teil}"


@dataclass
class Plan:
    auftraege: list[Auftrag]
    fehlt: list[str]            # Ansichten ohne Vorlage -> kein Foto, wird gemeldet


def lade_vorlagen(pfad: Path | str) -> dict:
    """Zuordnung aus der JSON-Datei. Fehlt die Datei, gibt es (noch) keine Vorlagen."""
    datei = Path(pfad)
    if not datei.is_file():
        return {}
    daten = json.loads(datei.read_text(encoding="utf-8"))
    return daten if isinstance(daten, dict) else {}


def plane(produkt_key: str, *, textil: bool, vorlagen: dict, hauptfarbe: str) -> Plan:
    """Die Fotos fuer EIN Produkt. Rechnet nichts, ruft nichts ab."""
    eintraege = vorlagen.get(produkt_key) or {}
    auftraege: list[Auftrag] = []
    fehlt: list[str] = []

    if textil:
        haupt = farbe(hauptfarbe)
        for ansicht, umfang in ANSICHTEN_TEXTIL.items():
            v = eintraege.get(ansicht)
            if not v or not v.get("mockup_uuid") or not v.get("motiv_objekt"):
                fehlt.append(f"{produkt_key}: Vorlage '{ansicht}' fehlt")
                continue
            farben = FARBEN if umfang == "alle" else (haupt,)
            if umfang == "alle" and not v.get("farb_objekt"):
                # Ohne Farbebene laesst sich die Ware nicht umfaerben - lieber ein
                # ehrliches Foto in der Vorlagenfarbe als acht falsche.
                fehlt.append(f"{produkt_key}: Vorlage '{ansicht}' hat keine Farbebene - "
                             "nur ein Foto in der Vorlagenfarbe")
                farben = (None,)
            for f in farben:
                auftraege.append(Auftrag(produkt_key, ansicht, f, v["mockup_uuid"],
                                         v["motiv_objekt"], v.get("farb_objekt")))
    else:
        for ansicht in ANSICHTEN_TASSE:
            v = eintraege.get(ansicht)
            if not v or not v.get("mockup_uuid") or not v.get("motiv_objekt"):
                fehlt.append(f"{produkt_key}: Vorlage '{ansicht}' fehlt")
                continue
            auftraege.append(Auftrag(produkt_key, ansicht, None, v["mockup_uuid"],
                                     v["motiv_objekt"], None))
    return Plan(auftraege, fehlt)


def textil_vollstaendig(produkt_key: str, *, vorlagen: dict, hauptfarbe: str) -> list[str]:
    """Fehlende Textil-Fotovorgaben. Leer heisst: echte Fotos koennen entstehen."""
    plan = plane(produkt_key, textil=True, vorlagen=vorlagen, hauptfarbe=hauptfarbe)
    benoetigt = {"mann", "frau", "vorne"}
    vorhanden = {a.ansicht for a in plan.auftraege}
    fehlt = list(plan.fehlt)
    for ansicht in sorted(benoetigt - vorhanden):
        meldung = f"{produkt_key}: Vorlage '{ansicht}' fehlt"
        if meldung not in fehlt:
            fehlt.append(meldung)
    return fehlt


def bilder_je_motiv(anzahl_textilien: int = 4) -> int:
    """Wie viele Credits ein vollstaendig eingestelltes Motiv kostet."""
    je_textil = sum(len(FARBEN) if u == "alle" else 1 for u in ANSICHTEN_TEXTIL.values())
    return je_textil * anzahl_textilien + len(ANSICHTEN_TASSE)
