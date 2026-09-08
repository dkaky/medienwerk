"""Eigenbeleg Gastronomie (Bewirtung) als PDF – gebaut mit der Standardbibliothek.

WARUM OHNE FREMDBIBLIOTHEK (reportlab & Co.): der Live-VPS bekommt beim Deploy nur
den Code (`git push` + `systemctl restart`, siehe .github/workflows/deploy.yml) – es
laeuft dort KEIN `pip install`. Eine neue Abhaengigkeit waere lokal und in der CI
gruen, auf dem Live-Server aber nicht installiert: der Endpoint wuerde erst beim
echten Nutzer 500ern. Deshalb erzeugt dieses Modul das PDF selbst; benutzt werden
nur `zlib` und `unicodedata` aus der Standardbibliothek.

Das Blatt ist der Vorlage des Nutzers nachgebaut (eigener Satz, kein fremdes
Copyright): links die Pflichtangaben-Tabelle (§ 4 Abs. 5 Satz 1 Nr. 2 EStG), rechts
der Kasten mit der Bewirtungsquittung, darunter die Unterschriftszeile. Die Zeilen
wachsen mit, wenn ein Text laenger ist – anders als auf dem festen Vordruck.

Das Programm fuellt hier NUR ein, was der Nutzer eingegeben hat. Nichts wird
geschaetzt, ergaenzt oder erfunden (Projektregel 14).
"""
from __future__ import annotations

import unicodedata
import zlib

# --- Seitenmasse (A4 in Punkt) --------------------------------------------
SEITE_B, SEITE_H = 595.28, 841.89
RAND = 57.0

# Tabelle links: Beschriftungs-Spalte + Wert-Spalte
TAB_X0 = RAND
TAB_X1 = RAND + 133.0          # Trennlinie Beschriftung | Wert
TAB_X2 = RAND + 293.0          # rechter Rand der Tabelle
TAB_TOP = 735.0

# Quittungs-Kasten rechts ("hier aufkleben")
BOX_X0, BOX_X1 = 368.0, SEITE_B - RAND
BOX_TOP, BOX_BOTTOM = 735.0, 210.0
BOX_PAD = 7.0

# Wird die Quittung im Kasten schmaler als das hier gezeichnet (langer, schmaler
# Kassenbon), waere die Schrift auf dem Ausdruck nicht mehr lesbar -> sie kommt
# zusaetzlich gross auf Seite 2. Nutzerwunsch.
MIN_LESBARE_BREITE = 130.0

# Alpha-PNGs muessen in Python entpackt werden (langsam). Oberhalb dieser
# Pixelzahl wird abgelehnt, statt den Server minutenlang zu blockieren.
MAX_PIXEL_ALPHA_PNG = 2_500_000


class BildFehler(ValueError):
    """Quittungs-Bild ist nicht verwertbar – Meldung ist fuer den Nutzer gedacht."""


# --- Zeichenbreiten (Helvetica, 1/1000 pt) --------------------------------
# Ohne diese Tabelle koennte der Zeilenumbruch nicht rechnen und langer Text
# (z.B. ein ausfuehrlicher Anlass) wuerde aus den Kaesten laufen.
_HELV = (278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
         556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
         1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
         667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
         333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
         556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584)
_HELV_FETT = (278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
              556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
              975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
              667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
              333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
              611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584)
_SONDERBREITE = {"ß": 556, "§": 556, "€": 556, "·": 278, "–": 556, "—": 1000,
                 "„": 333, "“": 333, "”": 333, "‚": 191, "‘": 191, "’": 191, "…": 1000}


def _zeichenbreite(ch: str, fett: bool) -> int:
    tab = _HELV_FETT if fett else _HELV
    o = ord(ch)
    if 32 <= o <= 126:
        return tab[o - 32]
    if ch in _SONDERBREITE:
        return _SONDERBREITE[ch]
    # Akzentbuchstabe so breit wie sein Grundbuchstabe (Ä wie A, ü wie u)
    basis = unicodedata.normalize("NFD", ch)[:1]
    o = ord(basis)
    if 32 <= o <= 126:
        return tab[o - 32]
    return 556


def _breite(text: str, groesse: float, fett: bool = False) -> float:
    return sum(_zeichenbreite(c, fett) for c in text) * groesse / 1000.0


def _umbruch(text: str, max_breite: float, groesse: float, fett: bool = False) -> list[str]:
    """Text auf die Kastenbreite umbrechen. Ein einzelnes ueberlanges Wort
    (z.B. eine lange E-Mail-Adresse) wird hart getrennt statt ueberzulaufen."""
    text = " ".join(str(text or "").split())
    if not text:
        return [""]
    zeilen: list[str] = []
    zeile = ""
    for wort in text.split(" "):
        probe = f"{zeile} {wort}".strip()
        if _breite(probe, groesse, fett) <= max_breite or not zeile:
            if _breite(probe, groesse, fett) <= max_breite:
                zeile = probe
                continue
            # einzelnes Wort passt nicht -> zeichenweise trennen
            rest = wort
            while rest and _breite(rest, groesse, fett) > max_breite:
                schnitt = len(rest)
                while schnitt > 1 and _breite(rest[:schnitt], groesse, fett) > max_breite:
                    schnitt -= 1
                zeilen.append(rest[:schnitt])
                rest = rest[schnitt:]
            zeile = rest
        else:
            zeilen.append(zeile)
            zeile = wort
    if zeile:
        zeilen.append(zeile)
    return zeilen or [""]


# --- PDF-Grundbausteine ---------------------------------------------------

# Buchstaben, die cp1252 nicht kennt und die auch keine Unicode-Zerlegung haben –
# ohne diese Liste wuerde aus "Yılmaz" ein "Y?lmaz" (Gaeste-Namen!).
_ERSATZ_BUCHSTABEN = {"ı": "i", "İ": "I", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D",
                      "ħ": "h", "Ħ": "H", "ŧ": "t", "Ŧ": "T", "ĸ": "k", "ŉ": "n"}


def _cp1252(text: str) -> bytes:
    """Text fuer die Standard-Schrift (WinAnsiEncoding) kodieren. Zeichen, die
    cp1252 nicht kennt (z.B. tuerkisches ı), werden auf ihren Grundbuchstaben
    zurueckgefuehrt statt zu '?' zu werden."""
    raus = bytearray()
    for ch in str(text or ""):
        for kandidat in (ch, unicodedata.normalize("NFD", ch)[:1],
                         _ERSATZ_BUCHSTABEN.get(ch, "?")):
            try:
                raus += kandidat.encode("cp1252")
                break
            except (UnicodeEncodeError, LookupError):
                continue
    return bytes(raus)


def _pdf_str(text: str) -> bytes:
    roh = _cp1252(text)
    for alt, neu in ((b"\\", b"\\\\"), (b"(", b"\\("), (b")", b"\\)")):
        roh = roh.replace(alt, neu)
    return b"(" + roh + b")"


def _z(wert: float) -> str:
    """Zahl kurz und ohne Exponentialschreibweise ins PDF."""
    return f"{wert:.2f}".rstrip("0").rstrip(".") or "0"


class _Dokument:
    """Minimaler PDF-Schreiber: Objekte sammeln, am Ende xref + trailer setzen."""

    def __init__(self) -> None:
        self._objekte: list[bytes | None] = [None]      # Index 0 bleibt leer (1-basiert)

    def platz(self) -> int:
        self._objekte.append(None)
        return len(self._objekte) - 1

    def setze(self, nummer: int, inhalt: bytes) -> int:
        self._objekte[nummer] = inhalt
        return nummer

    def add(self, inhalt: bytes) -> int:
        return self.setze(self.platz(), inhalt)

    def bytes(self, *, root: int) -> bytes:
        buf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0] * len(self._objekte)
        for i in range(1, len(self._objekte)):
            inhalt = self._objekte[i]
            if inhalt is None:
                raise RuntimeError(f"PDF-Objekt {i} wurde reserviert, aber nie gefuellt")
            offsets[i] = len(buf)
            buf += f"{i} 0 obj\n".encode("latin-1") + inhalt + b"\nendobj\n"
        start = len(buf)
        buf += f"xref\n0 {len(self._objekte)}\n".encode("latin-1")
        buf += b"0000000000 65535 f \n"
        for i in range(1, len(self._objekte)):
            buf += f"{offsets[i]:010d} 00000 n \n".encode("latin-1")
        buf += (f"trailer\n<< /Size {len(self._objekte)} /Root {root} 0 R >>\n"
                f"startxref\n{start}\n%%EOF\n").encode("latin-1")
        return bytes(buf)


class _Inhalt:
    """Zeichenbefehle einer Seite."""

    def __init__(self) -> None:
        self._teile: list[bytes] = []

    def text(self, x: float, y: float, s: str, *, groesse: float = 10.0,
             fett: bool = False, grau: float | None = None) -> None:
        if not str(s or "").strip():
            return
        font = b"/F2" if fett else b"/F1"
        farbe = f"{_z(grau)} {_z(grau)} {_z(grau)} rg\n".encode("latin-1") if grau is not None else b"0 0 0 rg\n"
        self._teile.append(farbe + b"BT " + font + f" {_z(groesse)} Tf {_z(x)} {_z(y)} Td ".encode("latin-1")
                           + _pdf_str(s) + b" Tj ET\n")

    def linie(self, x0: float, y0: float, x1: float, y1: float, *, staerke: float = 0.7,
              grau: float = 0.15) -> None:
        self._teile.append(
            f"{_z(grau)} {_z(grau)} {_z(grau)} RG {_z(staerke)} w "
            f"{_z(x0)} {_z(y0)} m {_z(x1)} {_z(y1)} l S\n".encode("latin-1"))

    def rechteck(self, x: float, y: float, b: float, h: float, *, staerke: float = 0.7,
                 grau: float = 0.15, gestrichelt: bool = False) -> None:
        strich = b"[3 3] 0 d " if gestrichelt else b"[] 0 d "
        self._teile.append(
            strich + f"{_z(grau)} {_z(grau)} {_z(grau)} RG {_z(staerke)} w "
            f"{_z(x)} {_z(y)} {_z(b)} {_z(h)} re S\n".encode("latin-1") + b"[] 0 d ")

    def bild(self, name: str, x: float, y: float, b: float, h: float) -> None:
        self._teile.append(f"q {_z(b)} 0 0 {_z(h)} {_z(x)} {_z(y)} cm /{name} Do Q\n".encode("latin-1"))

    def daten(self) -> bytes:
        return b"".join(self._teile)


# --- Bilder einbetten -----------------------------------------------------

def _jpeg_info(data: bytes) -> tuple[int, int, int]:
    """(Breite, Hoehe, Farbkanaele) aus dem JPEG-Kopf lesen."""
    i, n = 2, len(data)
    while i < n - 1:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xD8 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xD9 or marker == 0xDA:      # Bildende / Beginn der Bilddaten
            break
        if i + 4 > n:
            break
        laenge = int.from_bytes(data[i + 2:i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if i + 10 > n:
                break
            hoehe = int.from_bytes(data[i + 5:i + 7], "big")
            breite = int.from_bytes(data[i + 7:i + 9], "big")
            return breite, hoehe, data[i + 9]
        i += 2 + laenge
    raise BildFehler("Das Bild sieht nicht wie ein gueltiges JPG aus.")


def _png_entfiltern(roh: bytes, hoehe: int, bpp: int, stride: int) -> bytes:
    """PNG-Zeilenfilter rueckgaengig machen (nur noetig, wenn das Bild einen
    Transparenz-Kanal hat – sonst uebernimmt das der PDF-Betrachter selbst)."""
    raus = bytearray()
    vorher = bytearray(stride)
    pos = 0
    for _ in range(hoehe):
        if pos + 1 + stride > len(roh):
            raise BildFehler("Das PNG ist unvollstaendig.")
        typ = roh[pos]
        pos += 1
        zeile = bytearray(roh[pos:pos + stride])
        pos += stride
        if typ == 1:
            for i in range(bpp, stride):
                zeile[i] = (zeile[i] + zeile[i - bpp]) & 0xFF
        elif typ == 2:
            for i in range(stride):
                zeile[i] = (zeile[i] + vorher[i]) & 0xFF
        elif typ == 3:
            for i in range(stride):
                links = zeile[i - bpp] if i >= bpp else 0
                zeile[i] = (zeile[i] + ((links + vorher[i]) >> 1)) & 0xFF
        elif typ == 4:
            for i in range(stride):
                a = zeile[i - bpp] if i >= bpp else 0
                b = vorher[i]
                c = vorher[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                zeile[i] = (zeile[i] + pr) & 0xFF
        elif typ != 0:
            raise BildFehler("Das PNG benutzt einen unbekannten Filter.")
        raus += zeile
        vorher = zeile
    return bytes(raus)


def _png_xobject(data: bytes) -> tuple[dict[str, str], bytes, int, int]:
    """PNG als PDF-Bildobjekt vorbereiten.

    Ohne Alpha-Kanal (Graustufen/RGB/Palette) wandert der komprimierte Strom
    unveraendert ins PDF – der PDF-Betrachter kann den PNG-Zeilenfilter selbst
    (`/Predictor 15`). Nur bei Transparenz muss Python entpacken und den Kanal
    auf Weiss verrechnen.
    """
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise BildFehler("Das Bild sieht nicht wie ein gueltiges PNG aus.")
    pos = 8
    ihdr = None
    palette = b""
    idat = bytearray()
    while pos + 8 <= len(data):
        laenge = int.from_bytes(data[pos:pos + 4], "big")
        typ = data[pos + 4:pos + 8]
        inhalt = data[pos + 8:pos + 8 + laenge]
        pos += 12 + laenge
        if typ == b"IHDR":
            ihdr = inhalt
        elif typ == b"PLTE":
            palette = inhalt
        elif typ == b"IDAT":
            idat += inhalt
        elif typ == b"IEND":
            break
    if not ihdr or len(ihdr) < 13 or not idat:
        raise BildFehler("Das PNG ist unvollstaendig.")
    breite = int.from_bytes(ihdr[0:4], "big")
    hoehe = int.from_bytes(ihdr[4:8], "big")
    tiefe, farbtyp, _komp, _filt, interlace = ihdr[8], ihdr[9], ihdr[10], ihdr[11], ihdr[12]
    if tiefe != 8:
        raise BildFehler("Nur 8-Bit-PNGs werden unterstuetzt – bitte als JPG hochladen.")
    if interlace:
        raise BildFehler("Interlaced-PNGs werden nicht unterstuetzt – bitte als JPG hochladen.")
    if not breite or not hoehe:
        raise BildFehler("Das PNG hat keine gueltige Groesse.")

    if farbtyp in (0, 2, 3):
        # Schnellweg: Strom bleibt wie er ist, der Betrachter entfiltert.
        kanaele = {0: 1, 2: 3, 3: 1}[farbtyp]
        if farbtyp == 3:
            if not palette:
                raise BildFehler("Dem PNG fehlt die Farbtabelle.")
            farbraum = (f"[/Indexed /DeviceRGB {len(palette) // 3 - 1} "
                        f"<{palette.hex()}>]")
        else:
            farbraum = "/DeviceGray" if farbtyp == 0 else "/DeviceRGB"
        parms = (f"<< /Predictor 15 /Colors {kanaele} /BitsPerComponent 8 "
                 f"/Columns {breite} >>")
        return ({"ColorSpace": farbraum, "Filter": "/FlateDecode",
                 "DecodeParms": parms}, bytes(idat), breite, hoehe)

    if farbtyp not in (4, 6):
        raise BildFehler("Dieses PNG-Format wird nicht unterstuetzt – bitte als JPG hochladen.")
    if breite * hoehe > MAX_PIXEL_ALPHA_PNG:
        raise BildFehler("Das PNG ist zu gross (Transparenz muss umgerechnet werden) – "
                         "bitte als JPG hochladen.")
    kanaele = 2 if farbtyp == 4 else 4
    roh = _png_entfiltern(zlib.decompress(bytes(idat)), hoehe, kanaele, breite * kanaele)
    # Transparenz auf weissem Papier verrechnen und als RGB neu packen.
    rgb = bytearray(breite * hoehe * 3)
    for i in range(breite * hoehe):
        q = i * kanaele
        alpha = roh[q + kanaele - 1] / 255.0
        if farbtyp == 4:
            werte = (roh[q], roh[q], roh[q])
        else:
            werte = (roh[q], roh[q + 1], roh[q + 2])
        z = i * 3
        for k in range(3):
            rgb[z + k] = int(werte[k] * alpha + 255 * (1 - alpha))
    return ({"ColorSpace": "/DeviceRGB", "Filter": "/FlateDecode"},
            zlib.compress(bytes(rgb), 6), breite, hoehe)


def bild_vorbereiten(data: bytes) -> tuple[dict[str, str], bytes, int, int]:
    """Quittungs-Foto -> (PDF-Bildangaben, Datenstrom, Breite, Hoehe).

    JPG wandert unveraendert ins PDF (PDF versteht JPEG direkt), PNG siehe oben.
    Alles andere lehnen wir mit Klartext ab, statt einen kaputten Beleg zu bauen.
    """
    if not data:
        raise BildFehler("Es wurde kein Foto der Quittung hochgeladen.")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return _png_xobject(data)
    if data[:2] == b"\xff\xd8":
        breite, hoehe, kanaele = _jpeg_info(data)
        if kanaele == 1:
            farbraum = "/DeviceGray"
        elif kanaele == 3:
            farbraum = "/DeviceRGB"
        else:
            raise BildFehler("Dieses JPG hat ein ungewoehnliches Farbprofil (CMYK) – "
                             "bitte das Foto neu aufnehmen oder als PNG speichern.")
        return ({"ColorSpace": farbraum, "Filter": "/DCTDecode"}, data, breite, hoehe)
    if data[:4] == b"%PDF":
        raise BildFehler("Die Quittung liegt als PDF vor. Fuer den Eigenbeleg brauche ich "
                         "ein Foto (JPG oder PNG) – dann kann ich es aufs Blatt kleben.")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        raise BildFehler("WEBP kann nicht in ein PDF eingebettet werden – "
                         "bitte das Foto als JPG oder PNG hochladen.")
    raise BildFehler("Unbekanntes Bildformat – bitte ein Foto als JPG oder PNG hochladen.")


# --- Das Blatt ------------------------------------------------------------

def _eur(wert) -> str:
    try:
        zahl = float(wert)
    except (TypeError, ValueError):
        return ""
    return f"{zahl:,.2f} €".replace(",", "#").replace(".", ",").replace("#", ".")


def _zeilen_hoehe(beschriftung_zeilen: int, wert_zeilen: int, mindest: float) -> float:
    """Zeilenhoehe = Platz fuer die Textzeilen + Luft, damit Unterlaengen (g, j, ß)
    nicht auf der Trennlinie sitzen."""
    return max(mindest, 10.0 + 12.0 * max(beschriftung_zeilen, wert_zeilen))


def build_eigenbeleg(*, datum: str, ort: str, gastgeber: str, gaeste: str, anlass: str,
                     betrag: float, trinkgeld: float = 0.0, art: str = "",
                     quittung: bytes | None = None) -> bytes:
    """Fertigen Eigenbeleg als PDF-Bytes bauen.

    `datum` wird so uebernommen, wie es hereinkommt (das Dashboard liefert
    TT.MM.JJJJ). `betrag` ist der Rechnungsbetrag OHNE Trinkgeld – der
    Gesamtbetrag wird daraus berechnet, nicht geraten.
    """
    gesamt = float(betrag or 0) + float(trinkgeld or 0)
    doc = _Dokument()
    inhalt = _Inhalt()

    # Kopf
    inhalt.text(RAND, 782, "Eigenbeleg Gastronomie", groesse=19, fett=True)
    inhalt.text(RAND, 766, "Bewirtungsaufwendungen nach § 4 Abs. 5 Satz 1 Nr. 2 EStG",
                groesse=8.5, grau=0.4)
    if art:
        inhalt.text(RAND, 752, art, groesse=8.5, grau=0.4)

    # Tabelle links
    felder = [
        ("Datum Bewirtung", datum, 26.0, False),
        ("Ort der Bewirtung\n(Restaurant, etc.)", ort, 38.0, False),
        ("Gastgeber\n(bewirtende Person)", gastgeber, 38.0, False),
        ("Gäste bzw.\nbewirtete Personen", gaeste, 52.0, False),
        ("Genauer Anlass der\nBewirtung", anlass, 62.0, False),
        ("Betrag der\nBewirtungsrechnung\n(in Euro)", _eur(betrag), 46.0, False),
        ("Trinkgeld\n(in Euro)", _eur(trinkgeld), 30.0, False),
        ("Gesamtbetrag\n(in Euro)", _eur(gesamt), 30.0, True),
    ]
    b_breite = TAB_X1 - TAB_X0 - 14.0
    w_breite = TAB_X2 - TAB_X1 - 14.0
    y = TAB_TOP
    kanten = [TAB_TOP]
    for beschriftung, wert, mindest, fett in felder:
        b_zeilen = [z for teil in beschriftung.split("\n")
                    for z in _umbruch(teil, b_breite, 9.0, fett)]
        w_zeilen = _umbruch(wert, w_breite, 10.0, fett)
        hoehe = _zeilen_hoehe(len(b_zeilen), len(w_zeilen), mindest)
        for i, zeile in enumerate(b_zeilen):
            inhalt.text(TAB_X0 + 7, y - 15 - 12 * i, zeile, groesse=9, fett=fett)
        for i, zeile in enumerate(w_zeilen):
            inhalt.text(TAB_X1 + 7, y - 15 - 12 * i, zeile, groesse=10, fett=fett)
        y -= hoehe
        kanten.append(y)
    tab_unten = y
    for kante in kanten:
        inhalt.linie(TAB_X0, kante, TAB_X2, kante)
    for x in (TAB_X0, TAB_X1, TAB_X2):
        inhalt.linie(x, TAB_TOP, x, tab_unten)

    # KEINE Unterschriftszeile (Nutzer-Vorgabe 27.07.): unterschrieben wird direkt auf
    # der Quittung, die auf diesem Blatt klebt. Eine zweite Zeile waere doppelt.

    # Quittungs-Kasten rechts. Liegt ein Foto vor, schmiegt sich der gestrichelte
    # Rahmen ans Bild (statt halbleer dazustehen); ohne Foto bleibt die volle
    # Klebeflaeche fuer die Original-Quittung stehen.
    bild = None
    zweite_seite = False
    kasten_unten = BOX_BOTTOM
    if quittung:
        angaben, strom, b_px, h_px = bild_vorbereiten(quittung)
        nutz_b = (BOX_X1 - BOX_X0) - 2 * BOX_PAD
        nutz_h = (BOX_TOP - BOX_BOTTOM) - 2 * BOX_PAD
        skala = min(nutz_b / b_px, nutz_h / h_px)
        mal_b, mal_h = b_px * skala, h_px * skala
        inhalt.bild("Im0", BOX_X0 + BOX_PAD + (nutz_b - mal_b) / 2,
                    BOX_TOP - BOX_PAD - mal_h, mal_b, mal_h)
        kasten_unten = BOX_TOP - mal_h - 2 * BOX_PAD
        zweite_seite = mal_b < MIN_LESBARE_BREITE
        if zweite_seite:
            inhalt.text(BOX_X0, kasten_unten - 14,
                        "Quittung zusätzlich in Originalgröße auf Seite 2",
                        groesse=8, grau=0.4)
        bild = (angaben, strom, b_px, h_px)
    else:
        for i, zeile in enumerate(("Bewirtungsquittung hier", "aufkleben oder auf Rückseite",
                                   "anbringen")):
            inhalt.text(BOX_X0 + 12, BOX_TOP - 40 - 12 * i, zeile, groesse=9, grau=0.45)
    inhalt.rechteck(BOX_X0, kasten_unten, BOX_X1 - BOX_X0, BOX_TOP - kasten_unten,
                    staerke=0.6, grau=0.55, gestrichelt=True)

    # KEINE Fusszeile mit dem 70-%-Hinweis (Nutzer-Vorgabe 27.07.): der Beleg soll die
    # Angaben tragen, nicht den Gesetzeskommentar dazu – die 70 % rechnet der
    # Steuerberater. Nicht "hilfsbereit" wieder einbauen.

    # --- PDF zusammensetzen ---
    katalog = doc.platz()
    seiten = doc.platz()
    font_n = doc.add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                     b"/Encoding /WinAnsiEncoding >>")
    font_f = doc.add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
                     b"/Encoding /WinAnsiEncoding >>")
    bild_nr = 0
    if bild:
        angaben, strom, b_px, h_px = bild
        kopf = (f"<< /Type /XObject /Subtype /Image /Width {b_px} /Height {h_px} "
                f"/BitsPerComponent 8 /ColorSpace {angaben['ColorSpace']} "
                f"/Filter {angaben['Filter']} ")
        if angaben.get("DecodeParms"):
            kopf += f"/DecodeParms {angaben['DecodeParms']} "
        kopf += f"/Length {len(strom)} >>\nstream\n"
        bild_nr = doc.add(kopf.encode("latin-1") + strom + b"\nendstream")

    def _seite(befehle: bytes, mit_bild: bool) -> int:
        gepackt = zlib.compress(befehle, 6)
        strom_nr = doc.add(f"<< /Length {len(gepackt)} /Filter /FlateDecode >>\nstream\n"
                           .encode("latin-1") + gepackt + b"\nendstream")
        xobj = f"/XObject << /Im0 {bild_nr} 0 R >> " if (mit_bild and bild_nr) else ""
        return doc.add(
            f"<< /Type /Page /Parent {seiten} 0 R /MediaBox [0 0 {_z(SEITE_B)} {_z(SEITE_H)}] "
            f"/Resources << /Font << /F1 {font_n} 0 R /F2 {font_f} 0 R >> {xobj}>> "
            f"/Contents {strom_nr} 0 R >>".encode("latin-1"))

    seiten_nrn = [_seite(inhalt.daten(), True)]

    if zweite_seite and bild:
        _angaben, _strom, b_px, h_px = bild
        zwei = _Inhalt()
        zwei.text(RAND, 782, "Bewirtungsquittung", groesse=15, fett=True)
        zwei.text(RAND, 766, "Anlage zum Eigenbeleg Gastronomie – Originalgröße",
                  groesse=8.5, grau=0.4)
        nutz_b, nutz_h = SEITE_B - 2 * RAND, 690.0
        skala = min(nutz_b / b_px, nutz_h / h_px)
        mal_b, mal_h = b_px * skala, h_px * skala
        zwei.bild("Im0", RAND + (nutz_b - mal_b) / 2, 745 - mal_h, mal_b, mal_h)
        seiten_nrn.append(_seite(zwei.daten(), True))

    kids = " ".join(f"{n} 0 R" for n in seiten_nrn)
    doc.setze(seiten, f"<< /Type /Pages /Count {len(seiten_nrn)} /Kids [{kids}] >>"
              .encode("latin-1"))
    doc.setze(katalog, f"<< /Type /Catalog /Pages {seiten} 0 R >>".encode("latin-1"))
    return doc.bytes(root=katalog)
