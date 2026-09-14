"""Professionelle Produktfotos ohne Fremddienst: eigene Vorlagen, lokale Montage.

Entscheidung des Betreibers (13.09.2026): statt Dynamic Mockups eigene
KI-Vorlagen und lokale Montage.

**Vorlagen** (``data/mockup_vorlagen/<produkt>-<ansicht>.png``) sind einmalig mit
GPT Image erzeugte, fotorealistische Leerbilder - Model Mann, Model Frau, flach
von vorne, Tasse -, bei denen die Ware in **Chroma-Key-Gruen** (#00B140) ist. Das
Gruen hat zwei Aufgaben:

* **Maske:** Wo gruen ist, ist Stoff. Haut, Haare, Hose und Hintergrund bleiben
  unberuehrt. Bei freigestellten Flachbildern zaehlt zusaetzlich der
  Alphakanal - GPT Image laesst in den transparenten Bereichen gruene Farbwerte
  stehen (gemessen am Pilot: 38 % transparent, trotzdem 52 % Gruen am Rand).
* **Schattierung:** Die Helligkeit des Gruens traegt Falten und Licht. Beim
  Umfaerben bleiben sie erhalten, beim Montieren folgt das Motiv ihnen
  (Verdunkeln in Falten, leichter Verzug entlang der Faltenkanten).

**Das Motiv wird nicht neu gezeichnet.** Es wird Pixel fuer Pixel eingesetzt -
anders als bei KI-Fotos, die ein Motiv nachmalen und vom gelieferten Druck
abweichen. Die Farbtoene der Ware sind Naeherungen (siehe ``mockup_plan``).

Kosten: nur die Vorlagen (einmalig). Das Montieren ist lokal und kostenlos, die
Ergebnisse werden je Motiv zwischengespeichert.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from functools import lru_cache
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

logger = logging.getLogger("app.studio.mockup_montage")

ORDNER = Path("data/mockup_vorlagen")
FELDER_DATEI = "druckfelder.json"
HINTERGRUND = (236.0, 236.0, 236.0)
LANGE_KANTE = 1600

#: Reihenfolge der Bilder: Ware, Mann, Frau - erst vorne, dann dieselben drei von hinten.
ANSICHTEN_TEXTIL: tuple[str, ...] = ("vorne", "mann", "frau", "hinten", "mann_hinten", "frau_hinten")
ANSICHTEN_TASSE: tuple[str, ...] = ("vorne", "seite")


class MontageFehler(ValueError):
    """Die Vorlage taugt nicht zum Montieren."""


@dataclass(frozen=True)
class Feld:
    """Druckfeld relativ zur Ware: Start unter der Oberkante, Breite, max. Hoehe."""

    start: float
    breite: float
    max_hoehe: float
    band: tuple[float, float] = (0.15, 0.45)   # Hoehenbereich fuer Mitte und Breite


#: Relativ zur gruenen Flaeche. Polo tiefer und schmaler (Knopfleiste), Hoodie
#: ueber der Kaengurutasche, Tasse fast die ganze Wand. ``<produkt>-<ansicht>``
#: gilt vor ``<produkt>``: beim flach gelegten Hoodie gehoert die Kapuze zur
#: gruenen Flaeche, die Brust beginnt deshalb deutlich tiefer.
FELDER: dict[str, Feld] = {
    "tshirt": Feld(0.20, 0.62, 0.42),
    "polo": Feld(0.30, 0.46, 0.34),
    "oversize": Feld(0.18, 0.58, 0.42),
    "hoodie": Feld(0.24, 0.50, 0.30),
    "hoodie-vorne": Feld(0.36, 0.40, 0.28, band=(0.40, 0.60)),
    # Ruecken: gilt fuer die flache Rueckseite und die Models von hinten. Der
    # Rueckendruck sitzt hoeher und groesser; beim Hoodie liegt die Kapuze oben auf.
    "tshirt-hinten": Feld(0.14, 0.62, 0.46),
    "polo-hinten": Feld(0.22, 0.55, 0.40),
    "oversize-hinten": Feld(0.13, 0.58, 0.46),
    "hoodie-hinten": Feld(0.34, 0.52, 0.36, band=(0.40, 0.65)),
    "tasse": Feld(0.14, 0.78, 0.62),
}


@dataclass(frozen=True)
class Druckfeld:
    mitte_x: int
    oben_y: int
    breite: int
    hoehe: int


@dataclass
class Vorlage:
    name: str
    rgb: np.ndarray        # HxWx3 float32, Hintergrund bereits eingesetzt
    maske: np.ndarray      # HxW 0..1, 1 = Stoff
    schatten: np.ndarray   # HxW, ~1 = normal beleuchteter Stoff


# --------------------------------------------------------------------------
# Grundbausteine
# --------------------------------------------------------------------------
def _hex(hexwert: str) -> np.ndarray:
    h = (hexwert or "").lstrip("#")
    if len(h) != 6:
        raise MontageFehler(f"Ungueltige Farbe '{hexwert}'")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], np.float32)


def farbcode(name: str) -> str:
    ascii_ = unicodedata.normalize("NFKD", name.replace("ß", "ss")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-") or "farbe"


def gruenmaske(rgb: np.ndarray, alpha: np.ndarray | None = None) -> np.ndarray:
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    maske = np.clip((g - np.maximum(r, b) - 25.0) / 35.0, 0.0, 1.0)
    if alpha is not None:
        maske = maske * np.clip((alpha - 0.5) / 0.4, 0.0, 1.0)
    return maske


def schattierung(rgb: np.ndarray, maske: np.ndarray) -> np.ndarray:
    v = rgb.max(axis=2)
    kern = maske > 0.8
    ref = float(np.percentile(v[kern], 92)) if kern.any() else 255.0
    return np.clip(v / max(ref, 1.0), 0.0, 1.25)


def _weich(maske: np.ndarray, radius: float) -> np.ndarray:
    bild = Image.fromarray(np.clip(maske * 255, 0, 255).astype(np.uint8))
    return np.asarray(bild.filter(ImageFilter.GaussianBlur(radius)), np.float32) / 255.0


def lade_vorlage(pfad: Path | str, *, lange_kante: int = LANGE_KANTE) -> Vorlage:
    roh = Image.open(pfad).convert("RGBA")
    faktor = lange_kante / max(roh.size)
    if abs(faktor - 1.0) > 1e-3:
        roh = roh.resize((round(roh.width * faktor), round(roh.height * faktor)), Image.LANCZOS)
    arr = np.asarray(roh, np.float32)
    rgb, alpha = arr[..., :3].copy(), arr[..., 3] / 255.0
    transparent = float(np.mean(alpha < 0.5)) > 0.02

    maske = _weich(gruenmaske(rgb, alpha if transparent else None), 0.8)
    if float(np.mean(maske > 0.5)) < 0.02:
        raise MontageFehler(f"{Path(pfad).name}: kein gruener Stoff gefunden")
    schatten = schattierung(rgb, maske)

    if transparent:
        a3 = alpha[..., None]
        rgb = rgb * a3 + np.array(HINTERGRUND, np.float32) * (1.0 - a3)
    # Gruener Saum an weichen Kanten: dort das Gruen auf die anderen Kanaele begrenzen,
    # sonst bleibt nach dem Umfaerben ein gruener Rand um die Ware.
    saum = maske > 0.01
    rgb[..., 1] = np.where(saum, np.minimum(rgb[..., 1], np.maximum(rgb[..., 0], rgb[..., 2]) + 8),
                           rgb[..., 1])
    return Vorlage(Path(pfad).stem, rgb, maske, schatten)


def einfaerben(vorlage: Vorlage, hexwert: str) -> np.ndarray:
    """Die Ware in die Zielfarbe bringen - Falten und Licht bleiben."""
    ziel = _hex(hexwert)
    s = vorlage.schatten
    hell = float((0.2126 * ziel[0] + 0.7152 * ziel[1] + 0.0722 * ziel[2]) / 255.0)
    if hell > 0.8:            # Weiss, Sand: sanfte Schatten
        stoff = ziel * (1.0 - 0.55 * (1.0 - np.clip(s, 0, 1)))[..., None]
    elif hell > 0.45:         # mittlere Toene
        stoff = ziel * (0.45 + 0.55 * np.clip(s, 0, 1.1))[..., None] + 35 * np.clip(s - 0.95, 0, 0.3)[..., None]
    else:                     # dunkle Toene: kraeftige Schattierung, leichte Glanzlichter
        stoff = ziel * (0.30 + 0.70 * np.clip(s, 0, 1.1))[..., None] + 55 * np.clip(s - 0.95, 0, 0.3)[..., None]
    m = vorlage.maske[..., None]
    return vorlage.rgb * (1.0 - m) + np.clip(stoff, 0, 255) * m


def druckfeld(vorlage: Vorlage, produkt: str, ansicht: str | None = None) -> Druckfeld:
    """Druckfeld aus der gruenen Flaeche ableiten: Rumpfmitte, unter dem Kragen."""
    f = (FELDER.get(f"{produkt}-{ansicht}")
         or (FELDER.get(f"{produkt}-hinten") if ansicht and ist_hinten(ansicht) else None)
         or FELDER.get(produkt, FELDER["tshirt"]))
    ys, xs = np.nonzero(vorlage.maske > 0.5)
    y0, y1 = int(ys.min()), int(ys.max())
    hoehe = max(1, y1 - y0)
    band = (ys > y0 + f.band[0] * hoehe) & (ys < y0 + f.band[1] * hoehe)
    if not band.any():
        band = np.ones_like(ys, dtype=bool)
    if produkt == "tasse":
        # Der Henkel liegt rechts und hat ein Loch: Koerper = laengster Spaltenblock,
        # der in (fast) allen Zeilen des Bandes gruen ist.
        zeilen = vorlage.maske[int(y0 + 0.15 * hoehe):int(y0 + 0.75 * hoehe)] > 0.5
        voll = np.concatenate([[False], zeilen.mean(axis=0) > 0.95, [False]])
        kanten = np.flatnonzero(np.diff(voll.astype(np.int8)))
        laeufe = list(zip(kanten[::2], kanten[1::2]))
        if laeufe:
            links, rechts = max(laeufe, key=lambda l: l[1] - l[0])
        else:
            links, rechts = np.percentile(xs[band], [4, 70])
        mitte = int((links + rechts) / 2)
    else:
        links, rechts = np.percentile(xs[band], [10, 90])
        mitte = int(np.median(xs[band]))
    return Druckfeld(mitte, int(y0 + f.start * hoehe),
                     max(40, int((rechts - links) * f.breite)), max(40, int(f.max_hoehe * hoehe)))


def _zylinder(motiv: np.ndarray, staerke: float = 0.9) -> np.ndarray:
    """Tassenwoelbung: zum Rand hin gestaucht, als liefe das Motiv um die Tasse."""
    h, w = motiv.shape[:2]
    theta = np.arcsin(staerke)
    u = np.linspace(-1.0, 1.0, w)
    quelle = np.arcsin(u * np.sin(theta)) / theta          # Rand: groessere Schritte
    spalten = np.clip(((quelle + 1.0) / 2.0 * (w - 1)).round().astype(int), 0, w - 1)
    return motiv[:, spalten]


def produktflaeche(vorlage: Vorlage, produkt: str, ansicht: str | None = None) -> tuple[int, int, int, int]:
    """Die ganze Ware als Gestaltungsflaeche: (links, oben, breite, hoehe) in Pixeln.

    Textil: der Rahmen um den gruenen Stoff - flach ist das die Ware samt Aermeln,
    am Model das getragene Kleidungsstueck. Tasse: der Koerper ohne Henkel, volle Hoehe.
    """
    ys, xs = np.nonzero(vorlage.maske > 0.5)
    y0, y1 = int(ys.min()), int(ys.max())
    x0, x1 = int(xs.min()), int(xs.max())
    if produkt == "tasse":
        hoehe = y1 - y0
        zeilen = vorlage.maske[int(y0 + 0.15 * hoehe):int(y0 + 0.85 * hoehe)] > 0.5
        voll = np.concatenate([[False], zeilen.mean(axis=0) > 0.95, [False]])
        kanten = np.flatnonzero(np.diff(voll.astype(np.int8)))
        laeufe = list(zip(kanten[::2], kanten[1::2]))
        if laeufe:
            links, rechts = max(laeufe, key=lambda l: l[1] - l[0])
            x0, x1 = int(links), int(rechts) - 1
    return x0, y0, max(1, x1 - x0 + 1), max(1, y1 - y0 + 1)


#: Aufloesung, in der die Gestaltungsflaeche vermessen und fuer den Editor gezeigt wird.
FLAECHE_KANTE = 1200


def flaechen_vorlage(produkt: str, seite: str, *, textil: bool, ordner: Path = ORDNER) -> Path:
    """Die Vorlage, die im Editor als Flaeche dient: die flache Ware von vorne bzw. hinten."""
    ansicht = "hinten" if textil and seite == "hinten" else "vorne"
    for quelle in (ordner / f"{produkt}-{ansicht}.png", ordner / f"{produkt}-vorne.png"):
        if quelle.is_file():
            return quelle
    raise MontageFehler(f"{produkt}: keine Vorlage fuer die Gestaltungsflaeche ({ordner})")


@lru_cache(maxsize=64)
def _vermessen(pfad: str, stand: int, produkt: str) -> tuple[int, int, int, int]:
    return produktflaeche(lade_vorlage(pfad, lange_kante=FLAECHE_KANTE), produkt)


def seitenverhaeltnis(produkt: str, seite: str, *, textil: bool, ordner: Path = ORDNER) -> float:
    """Breite : Hoehe der Gestaltungsflaeche dieser Seite."""
    quelle = flaechen_vorlage(produkt, seite, textil=textil, ordner=ordner)
    _, _, breite, hoehe = _vermessen(str(quelle), int(quelle.stat().st_mtime), produkt)
    return breite / hoehe


def flaechenbild(produkt: str, seite: str, name: str, hexwert: str, *, textil: bool,
                 ziel_ordner: Path, ordner: Path = ORDNER) -> Path:
    """Die leere Ware in der Farbe, auf die Gestaltungsflaeche zugeschnitten - Hintergrund im Editor."""
    quelle = flaechen_vorlage(produkt, seite, textil=textil, ordner=ordner)
    ziel = ziel_ordner / f"{produkt}-{seite}-{farbcode(name)}-{int(quelle.stat().st_mtime)}.jpg"
    if not ziel.is_file():
        v = lade_vorlage(quelle, lange_kante=FLAECHE_KANTE)
        x, y, w, h = produktflaeche(v, produkt)
        bild = Image.fromarray(np.clip(einfaerben(v, hexwert), 0, 255).astype(np.uint8))
        ziel_ordner.mkdir(parents=True, exist_ok=True)
        bild.crop((x, y, x + w, y + h)).save(ziel, "JPEG", quality=88)
    return ziel


def montiere(vorlage: Vorlage, motiv: Image.Image, hexwert: str, feld: Druckfeld,
             *, zylinder: bool = False, ganzflaeche: bool = False,
             flaeche: tuple[int, int, int, int] | None = None) -> Image.Image:
    bild = einfaerben(vorlage, hexwert)
    m = motiv.convert("RGBA")
    if ganzflaeche:
        # Druckbild aus dem Editor: es steht fuer die ganze Ware. Textil: auf die Hoehe
        # der Ware in dieser Ansicht, mittig. Tasse: auf die Breite des Koerpers.
        fx, fy, fw, fh = flaeche or produktflaeche(vorlage, "tasse" if zylinder else "")
        if zylinder:
            m = m.resize((fw, max(1, round(fw * m.height / m.width))), Image.LANCZOS)
        else:
            m = m.resize((max(1, round(fh * m.width / m.height)), fh), Image.LANCZOS)
    else:
        rahmen = m.getbbox()
        if rahmen:
            m = m.crop(rahmen)
        m.thumbnail((feld.breite, feld.hoehe), Image.LANCZOS)
    marr = np.asarray(m, np.float32)
    if zylinder:
        marr = _zylinder(marr)
    mh, mw = marr.shape[:2]

    hoehe, breite = bild.shape[:2]
    if ganzflaeche:
        x0, y0 = fx + fw // 2 - mw // 2, fy
    else:
        x0, y0 = feld.mitte_x - mw // 2, feld.oben_y
    ax0, ay0 = max(0, x0), max(0, y0)
    ax1, ay1 = min(breite, x0 + mw), min(hoehe, y0 + mh)
    if ax1 <= ax0 or ay1 <= ay0:
        raise MontageFehler("Druckfeld liegt ausserhalb der Vorlage")
    aus = (slice(ay0, ay1), slice(ax0, ax1))

    # Verzug entlang der Falten: Verschiebung aus dem Gradienten der Schattierung.
    glatt = _weich(np.clip(vorlage.schatten / 1.25, 0, 1), 6.0) * 1.25
    gy, gx = np.gradient(glatt)
    dx = np.clip(gx[aus] * 90.0, -8, 8)
    dy = np.clip(gy[aus] * 90.0, -8, 8)
    yy, xx = np.mgrid[ay0 - y0:ay1 - y0, ax0 - x0:ax1 - x0].astype(np.float32)
    qx = np.clip(xx - dx, 0, mw - 1).astype(int)
    qy = np.clip(yy - dy, 0, mh - 1).astype(int)
    stueck = marr[qy, qx]

    licht = np.clip(vorlage.schatten[aus], 0, 1.15)[..., None]
    druck = stueck[..., :3] * (0.62 + 0.38 * licht)                     # Falten dunkeln den Druck
    deckung = stueck[..., 3:] / 255.0 * vorlage.maske[aus][..., None] * 0.96
    bild[aus] = bild[aus] * (1.0 - deckung) + druck * deckung
    return Image.fromarray(np.clip(bild, 0, 255).astype(np.uint8))


# --------------------------------------------------------------------------
# Vorlagen verwalten und rendern
# --------------------------------------------------------------------------
def ansichten(textil: bool) -> tuple[str, ...]:
    return ANSICHTEN_TEXTIL if textil else ANSICHTEN_TASSE


def vorlagen_pfade(produkt: str, *, textil: bool, ordner: Path = ORDNER) -> dict[str, Path]:
    return {a: ordner / f"{produkt}-{a}.png" for a in ansichten(textil)
            if (ordner / f"{produkt}-{a}.png").is_file()}


def fehlende_vorlagen(produkt: str, *, textil: bool, ordner: Path = ORDNER) -> list[str]:
    vorhanden = vorlagen_pfade(produkt, textil=textil, ordner=ordner)
    return [f"{produkt}: Vorlage '{a}' fehlt ({ordner / f'{produkt}-{a}.png'})"
            for a in ansichten(textil) if a not in vorhanden]


def ist_hinten(ansicht: str) -> bool:
    return ansicht.endswith("hinten")


def folge(produkt: str, *, textil: bool, ordner: Path = ORDNER, vorne_leer: bool = False) -> list[str]:
    """Bildreihenfolge: erst vorne (Ware, Mann, Frau), dann hinten.

    Ist nur die Rueckseite bedruckt, kommt sie zuerst - ein leeres Shirt als
    erstes Bild wuerde das Motiv verstecken.
    """
    vorhanden = list(vorlagen_pfade(produkt, textil=textil, ordner=ordner))
    vorn = [a for a in vorhanden if not ist_hinten(a)]
    hin = [a for a in vorhanden if ist_hinten(a)]
    return hin + vorn if vorne_leer else vorn + hin


def _kennung(pfad: Path | None) -> str:
    """Teil des Dateinamens im Zwischenspeicher - wechselt mit dem gewaehlten Motiv."""
    if pfad is None:
        return "leer"
    pfad = Path(pfad)
    return f"{int(pfad.stat().st_mtime)}{zlib.crc32(str(pfad.resolve()).encode()) % 1000:03d}"


def _feste_felder(ordner: Path) -> dict[str, Druckfeld]:
    """Handverstellte Druckfelder aus druckfelder.json (Pixel bei 1600er Kante)."""
    datei = ordner / FELDER_DATEI
    if not datei.is_file():
        return {}
    daten = json.loads(datei.read_text(encoding="utf-8"))
    return {k: Druckfeld(**v) for k, v in daten.items() if isinstance(v, dict)}


def rendere(motiv_pfad: Path | None, *, produkt: str, textil: bool, farben: list[tuple[str, str]],
            ziel_ordner: Path, ordner: Path = ORDNER, hinten: Path | None = None,
            lange_kante: int | None = None, ganzflaeche: bool = False) -> dict[str, list[Path]]:
    """Je Farbe die Fotos aller vorhandenen Ansichten, in der Reihenfolge von ``folge``.

    ``motiv_pfad`` sitzt vorne, ``hinten`` auf dem Ruecken; eine Seite ohne Motiv
    wird unbedruckt gezeigt. Die Tasse hat keine Rueckseite und nimmt das Motiv,
    das es gibt. Bereits Gerendertes wird wiederverwendet.
    """
    seiten = {"vorne": motiv_pfad, "hinten": hinten} if textil else {"vorne": motiv_pfad or hinten, "hinten": None}
    bilder = {k: (Image.open(v) if v is not None else None) for k, v in seiten.items()}
    kennung = {k: _kennung(v) for k, v in seiten.items()}
    feste = _feste_felder(ordner)
    pfade = vorlagen_pfade(produkt, textil=textil, ordner=ordner)
    reihe = folge(produkt, textil=textil, ordner=ordner,
                  vorne_leer=seiten["vorne"] is None and seiten["hinten"] is not None)
    geladen: dict[str, tuple] = {}
    ziel_ordner.mkdir(parents=True, exist_ok=True)

    ergebnis: dict[str, list[Path]] = {}
    for name, hexwert in farben:
        liste: list[Path] = []
        for ansicht in reihe:
            quelle = pfade[ansicht]
            seite = "hinten" if ist_hinten(ansicht) else "vorne"
            fassung = f"{int(quelle.stat().st_mtime)}-{kennung[seite]}"
            ziel = ziel_ordner / f"{produkt}-{ansicht}-{farbcode(name)}-{fassung}.jpg"
            if not ziel.is_file():
                if ansicht not in geladen:
                    v = lade_vorlage(quelle, lange_kante=lange_kante or LANGE_KANTE)
                    geladen[ansicht] = (v, feste.get(f"{produkt}-{ansicht}") or druckfeld(v, produkt, ansicht),
                                        produktflaeche(v, produkt, ansicht))
                v, feld, flaeche = geladen[ansicht]
                if bilder[seite] is None:
                    bild = Image.fromarray(np.clip(einfaerben(v, hexwert), 0, 255).astype(np.uint8))
                else:
                    bild = montiere(v, bilder[seite], hexwert, feld, zylinder=not textil,
                                    ganzflaeche=ganzflaeche, flaeche=flaeche)
                bild.save(ziel, "JPEG", quality=92)
                # Aeltere Fassungen derselben Ansicht und Farbe braucht niemand mehr.
                for alt in ziel_ordner.glob(f"{produkt}-{ansicht}-{farbcode(name)}-*.jpg"):
                    if alt != ziel:
                        alt.unlink(missing_ok=True)
            liste.append(ziel)
        ergebnis[name] = liste
    return ergebnis


def rendere_leer(produkt: str, ansicht: str, name: str, hexwert: str, *, ziel_ordner: Path,
                 ordner: Path = ORDNER, lange_kante: int | None = None) -> Path:
    """Die leere Vorlage in einer Farbe - zum Anschauen im Dashboard, ohne Motiv."""
    quelle = ordner / f"{produkt}-{ansicht}.png"
    if not quelle.is_file():
        raise MontageFehler(f"{produkt}: Vorlage '{ansicht}' fehlt ({quelle})")
    ziel = ziel_ordner / f"{produkt}-{ansicht}-{farbcode(name)}-{int(quelle.stat().st_mtime)}.jpg"
    if not ziel.is_file():
        ziel_ordner.mkdir(parents=True, exist_ok=True)
        v = lade_vorlage(quelle, lange_kante=lange_kante or LANGE_KANTE)
        Image.fromarray(np.clip(einfaerben(v, hexwert), 0, 255).astype(np.uint8)).save(
            ziel, "JPEG", quality=90)
    return ziel
