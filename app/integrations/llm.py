"""LLM-Integration (Spec Kap. 4.4).

SEO-Titel-Generierung, Beschreibungsbereinigung, Bildsuche. Interface + Mock.
Real-Client unterstuetzt Claude (Anthropic SDK) oder OpenAI.
"""
from __future__ import annotations

import abc
import json
import logging
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, field_validator

from app.retry import PersistentError, RateLimitError, TransientError

logger = logging.getLogger("app.integrations.llm")


# Maße, die die KI NIE ändern darf: Dimensionen (NxM(xK)) + Zahl+physikalische Einheit.
_DIM_TOKEN = re.compile(r"\d+(?:[.,]\d+)?(?:\s*[x×*]\s*\d+(?:[.,]\d+)?)+", re.IGNORECASE)  # 55x90 / 40X55 / 4.7x1.8x1.5
_PHYS_TOKEN = re.compile(r"\d+(?:[.,]\d+)?\s*(?:cm|mm|ml|kg|zoll|inch|\")", re.IGNORECASE)  # 90cm/250ml/12"
# Zähl-Mengen: hier ist die EINHEIT übersetzbar (PCS<->Stück<->er), nur die ZAHL zählt.
_COUNT_TOKEN = re.compile(
    r"(\d+)\s*(?:stück|stk|pcs|pc|er\b|set|paar|pack|piece|pieces|teilig)",
    re.IGNORECASE)


def _norm_num(tok: str) -> str:
    """Zahl-Schreibweise vereinheitlichen: Komma->Punkt, Nachkomma-Nullen weg, Leerzeichen/× raus.
    So gilt '2 x 20' == '2,0×20' und '4.70' == '4,7' (reine Formatierung, kein echter Fehler)."""
    t = tok.lower().replace(" ", "").replace(",", ".").replace("×", "x").replace("*", "x")
    t = re.sub(r"(\d+\.\d*?)0+(?=\D|$)", r"\1", t)     # 4.70 -> 4.7
    t = re.sub(r"(\d+)\.(?=\D|$)", r"\1", t)           # 2. -> 2
    t = re.sub(r"\.0+(?=\D|$)", "", t)                 # 2.0 -> 2
    return t


def _measurement_set(text) -> set:
    """Normierte Menge aller Maß-Tokens: Dimensionen + Zahl+physikalische Einheit exakt,
    Zähl-Mengen nur mit der Zahl (Einheit übersetzbar)."""
    t = str(text or "")
    out = set()
    for m in _DIM_TOKEN.finditer(t):
        out.add("dim:" + _norm_num(m.group(0)))
    for m in _PHYS_TOKEN.finditer(t):
        out.add("phys:" + _norm_num(m.group(0)))
    for m in _COUNT_TOKEN.finditer(t):
        out.add("cnt:" + m.group(1))
    return out


def preserves_measurements(old, new) -> bool:
    """True, wenn ``new`` KEINE Maß-/Mengen-Zahl von ``old`` verfaelscht.

    GELD-/RECHTS-SICHERHEIT (Vorfall Pokemon-Poster 12.07.): die KI hatte '55x90cm'
    zu '90x90cm' halluziniert -> falsche Maße im eBay-Listing. Dimensionen/Einheiten
    duerfen NIE von der KI geaendert werden; nur Woerter drumherum (Unframed->ohne Rahmen)
    und die Zähl-Einheit (PCS->Stück) sind uebersetzbar. Fehlt ein Original-Maß in ``new``
    -> False (Umbenennung wird verworfen, Original bleibt = sicher)."""
    om = _measurement_set(old)
    if not om:
        return True                       # nichts Zahlenhaftes zu schuetzen
    return om <= _measurement_set(new)     # alle Original-Maße muessen erhalten sein


# Mengen-BEHAUPTUNGEN im Titel ("3er-Set", "5 Stück", "2 Paar", "3-teilig"):
# fuers Entfernen erfundener Angaben — "80er Jahre" (Stil-Angabe) bleibt.
_COUNT_CLAIM = re.compile(
    r"\b(\d+)[\s-]*(?:er[\s-]?set|er\b(?![\s-]?jahre)|teilig|st(?:ü|ue)ck|stk\.?"
    r"|pcs?\b|set\b|paar\b|pack\b|pieces?\b)(?:[\s-]?set)?",
    re.IGNORECASE)


def strip_invented_counts(source, title) -> tuple:
    """Erfundene Mengen-Behauptungen aus einem KI-Titel entfernen (deterministisch).

    Vorfall Zoro-Ohrringe 19.08.: die KI machte aus einem Ein-Paar-Artikel ein
    "3er-Set". :func:`preserves_measurements` schuetzt nur vor VERLUST von
    Quell-Zahlen — nicht vor ERFUNDENEN Zusatz-Mengen. Regel: Zahl+Zaehleinheit
    darf nur in den Titel, wenn die QUELLE dieselbe Zahl mit Zaehleinheit traegt.
    Rueckgabe: (bereinigter Titel, Liste der entfernten Behauptungen)."""
    src = {m.group(1) for m in _COUNT_TOKEN.finditer(str(source or ""))}
    entfernt: list = []

    def _pruefen(m):
        if m.group(1) in src:
            return m.group(0)
        entfernt.append(m.group(0).strip())
        return " "

    out = _COUNT_CLAIM.sub(_pruefen, str(title or ""))
    if entfernt:
        out = re.sub(r"\s*-\s*(?=\s|$)", " ", out)      # haengende Bindestriche
        out = re.sub(r"\s{2,}", " ", out).strip(" -–")
    return out, entfernt


def _norm_spec_name(x) -> str:
    return re.sub(r"\s+", " ", str(x or "").strip().lower())


def repair_spec_measurements(specifics: dict, source_specs) -> dict:
    """Deterministischer Maß-Schutz für item_specifics (KI NIE vertrauen).

    Verliert ein von der KI generierter item_specific ein Maß, das der zugehörige ROHE
    AliExpress-Spec hatte (z.B. Länge×Breite '55x90cm' -> nur '90cm'), wird der ROH-Wert
    wiederhergestellt. Gleiche GELD-/RECHTS-Sicherheit wie [[preserves_measurements]] bei
    Varianten/Titel – NUR dass hier gegen die Original-Specs GEHEILT statt nur verworfen wird
    (die Zeile muss ja existieren). Zuordnung: exakter (normalisierter) Name; sonst der Roh-Spec,
    dessen Maße sich mit dem KI-Wert überschneiden, der aber MEHR Maß trägt (Trunkierung).
    Nicht-Maß-Werte und korrekt übernommene Maße bleiben unangetastet."""
    if not specifics:
        return specifics
    src = [(str(s.get("name") or ""), str(s.get("value") or ""))
           for s in (source_specs or []) if s.get("value")]
    if not src:
        return specifics
    src_by_name: dict = {}
    for nm, val in src:
        src_by_name.setdefault(_norm_spec_name(nm), val)
    out: dict = {}
    for name, val in specifics.items():
        raw = src_by_name.get(_norm_spec_name(name))
        if raw is None:                                    # kein Namens-Treffer -> Maß-Überlappung
            gv = _measurement_set(val)
            if gv:
                for _nm, rv in src:
                    rm = _measurement_set(rv)
                    if rm and (gv & rm) and not (rm <= gv):  # gemeinsame Zahl, Roh trägt MEHR Maß
                        raw = rv
                        break
        if raw is not None and not preserves_measurements(raw, val):
            logger.warning("item_specific measurement repaired from source spec",
                           extra={"spec_name": str(name), "dropped": str(val), "restored": str(raw)})
            val = raw
        out[name] = val
    return out


# --- Titel-Wortreihenfolge: Produkt zuerst ---------------------------------
# eBay gewichtet die ERSTEN Titelwoerter am staerksten, und Kaeufer tippen den ARTIKEL
# ein ("Vase"), nicht die Eigenschaft ("edle"). Ein Titel, der mit einem beschreibenden
# Wort beginnt, verschenkt darum Ranking:
#   FALSCH: "Edle Schwarze Vase Deko Japanisch"
#   RICHTIG: "Vase Deko Edel Schwarz Japanisch"
def _adjective_forms(base: str) -> set:
    """Deutsche Adjektiv-Formen zu einem Grundwort – inkl. e-Tilgung (edel -> edle/edler)."""
    stem = base[:-2] + base[-1] if base.endswith(("el", "er")) and len(base) > 3 else base
    forms = {base, stem}
    for end in ("e", "er", "es", "em", "en"):
        forms.add(base + end)
        forms.add(stem + end)
    return forms


_LEADING_ADJ_BASES = (
    # Anmutung/Qualitaet
    "edel", "elegant", "hochwertig", "modern", "klassisch", "stilvoll", "schön", "luxuriös",
    "exklusiv", "modisch", "trendig", "hübsch", "niedlich", "süß", "cool", "professionell",
    "vintage", "retro", "antik", "rustikal", "minimalistisch",
    "kreativ", "lustig", "witzig", "originell", "stylisch",   # ergaenzt 28.08.2026
    # Herkunft/Stil
    "nordisch", "skandinavisch", "japanisch", "chinesisch", "koreanisch", "türkisch",
    "arabisch", "afrikanisch", "orientalisch", "asiatisch",
    # Eigenschaften
    "praktisch", "bequem", "weich", "robust", "stabil", "leicht", "klein", "groß", "lang",
    "kurz", "breit", "schmal", "dünn", "dick", "rund", "warm", "flexibel", "langlebig",
    "wasserdicht", "faltbar", "tragbar", "verstellbar", "wiederverwendbar", "universal",
    "matt", "glänzend", "bunt",
    # Farben
    "schwarz", "weiß", "rot", "blau", "grün", "gelb", "braun", "grau", "rosa", "lila",
    "violett", "türkis", "beige", "silbern", "golden", "orange",
)
# Feste Begriffe ohne Adjektiv-Beugung (Zielgruppen, Materialien als Vorsatz).
_LEADING_FIXED = {
    "damen", "herren", "kinder", "unisex", "männer", "frauen", "baby", "jungen", "mädchen",
    "gold", "silber", "rosegold", "premium", "neu", "top",
    # Materialien als Vorsatz und Einsatzzwecke (ergaenzt 28.08.2026). Beim ersten
    # Bekleidungs-Import entstanden "Sport Avete Cotto ..." und "Baumwoll T-Shirt ...":
    # beides beschreibende Anfaenge, die der Umsortierer nicht erkannte, weil er nur
    # Schmuck- und Deko-Woerter kannte. "Baumwoll" ist allein kein Wort, sondern ein
    # Kompositum-Vorsatz - "Baumwolltuch" als GANZES Wort loest weiterhin nicht aus.
    "sport", "freizeit", "fitness", "casual", "vintage",
    "baumwoll", "baumwolle", "leinen", "seiden", "woll", "polyester",
    # Englische Anpreisungen: im zweiten Import entstanden "Special T-Shirt ..." und
    # "Kreatives Motiv-Shirt ...". Die Liste kannte nur deutsche Woerter.
    "special", "funny", "cool", "cute", "nice", "best", "amazing", "unique",
}
_LEADING_DESCRIPTORS = set(_LEADING_FIXED)
for _b in _LEADING_ADJ_BASES:
    _LEADING_DESCRIPTORS |= _adjective_forms(_b)


def leading_descriptor(title) -> str | None:
    """Das erste Titelwort, falls es BESCHREIBEND statt das Produkt ist – sonst ``None``.

    Sicherheitsnetz zu den Prompt-Regeln (Modelle halten sie gut, aber nie zu 100 %).
    Greift nur bei GANZEN Woertern: "Edelstahl" oder "Schwarzlicht" sind Produkte und
    loesen NICHT aus, "Edle"/"Schwarz" schon. Zahl am Anfang (925er, 18K, 3-teilig)
    gilt ebenfalls als beschreibend.
    """
    first = str(title or "").strip().split(" ", 1)[0]
    token = re.sub(r"[^\wäöüß-]", "", first, flags=re.UNICODE).lower()
    if not token:
        return None
    if token[0].isdigit():                      # 925er, 18K, 3-teilig ...
        return first
    return first if token in _LEADING_DESCRIPTORS else None


@dataclass
class GeneratedListing:
    title_seo: str
    description_clean: str          # vollstaendige eBay-Beschreibung (Bloecke + Footer)
    warnings: list[str]
    strategic_note: str = ""        # strategischer Hinweis (Markenrisiken, SEO, Widersprueche)
    item_specifics: dict = field(default_factory=dict)  # eBay-Artikelmerkmale (Name -> Wert)


# --- Pflicht-Textbausteine (Vorgabe des Auftraggebers) ---------------------
_FOOTER = (
    "📦 Sobald Ihr Paket unterwegs ist, erhalten Sie selbstverständlich eine "
    "Sendungsverfolgung, sodass Sie jederzeit wissen, wann Ihr Produkt bei Ihnen ankommt.\n"
    "📩 Fragen? Unser Kundenservice hilft jederzeit gerne.\n"
    "Als Kleinunternehmer im Sinne von § 19 Abs. 1 UStG wird keine Umsatzsteuer berechnet"
)
_JEWELRY_DISCLAIMER = (
    "Goldfarbig und silberfarbig beschichteter Edelstahl. Modeschmuck, kein "
    "Echtgold oder Echtsilber."
)
# Marken mit VeRO-/Markenrechtsrisiko (Auszug) – im Titel/Text aktiv flaggen.
_VERO_BRANDS = [
    "thomas sabo", "pandora", "disney", "fifty shades", "nba", "nfl",
    "apple", "nike", "adidas", "gucci", "louis vuitton", "lego", "swarovski",
]

# --- Bewirtungsbeleg: Anlass-Vorlagen (Offline-Fallback ohne Modell) -------
# Jede Vorlage nennt einen ANDEREN konkreten Geschaeftszweck – pauschale Angaben
# ("Geschaeftsessen") erkennt das Finanzamt nicht an (§ 4 Abs. 5 EStG). Es sind
# Formulierungshilfen: der Nutzer waehlt die zutreffende aus.
# BEWUSST KURZ UND OHNE NAMEN (ausdrueckliche Nutzer-Vorgabe 27.07.): der Anlass wird
# von Hand in ein kleines Feld auf der Restaurant-Quittung geschrieben, und WER dabei
# war, steht dort schon in einem eigenen Feld (bzw. auf dem Eigenbeleg in der Zeile
# "Gäste bzw. bewirtete Personen"). Der Anlass nennt deshalb nur das THEMA – kein
# "Gespraechspartner: …", keine Personen- oder Firmennamen, kein Verb-Vorspann
# ("Besprechung der …"). Richtwert: unter 45 Zeichen.
_BEWIRTUNG_VORLAGEN = (
    "Einkaufskonditionen und Staffelpreise",
    "Liefer- und Versandzeiten",
    "Sortimentserweiterung Auto-Detailing",
    "Retouren- und Reklamationsabwicklung",
    "Zusammenarbeit bei Edelstahlschmuck",
    "Produktfotos und Produktdaten für neue Artikel",
    "Jahresgespräch zur weiteren Zusammenarbeit",
    "Mindestabnahmemengen für die neue Saison",
)

# Laengste Zeile, die das Programm durchlaesst. Vorgabe ans Modell sind ~45 Zeichen.
# Zu lange Vorschlaege werden VERWORFEN, nicht gekuerzt: bis 03.08.2026 wurde bei 70
# Zeichen abgeschnitten, dadurch standen halbe Saetze in der Auswahl ("... und
# Staffelpreise fuer die neue") und der Nutzer sah den vollen Text nie.
_ANLASS_MAX_ZEICHEN = 90


def _norm_anlass(text: str) -> str:
    """Vergleichsform eines Anlasses (klein, ohne Mehrfach-Leerzeichen/Satzzeichen) –
    damit „schon benutzt" auch bei leicht abweichender Schreibweise greift."""
    return re.sub(r"[^a-z0-9äöüß ]", "", " ".join(str(text or "").lower().split()))


class LLMClient(abc.ABC):
    @abc.abstractmethod
    async def generate_listing(self, *, title_raw: str, description_raw: str,
                               category_guess: str | None,
                               specs: list | None = None,
                               variant_axes: dict | None = None,
                               pflicht_namen: list | None = None,
                               vergebene_anfaenge: list | None = None) -> GeneratedListing:
        ...

    async def extract_names(self, *, title_raw: str) -> list[str]:
        """Eigennamen (Marke, Band, Film, Serie, Figur, Person) aus dem Rohtitel.

        Eigener, enger Aufruf statt Vertrauen in die grosse Listing-Anweisung: das Modell
        strich Namen wie 'Boehse Onkelz' trotz ausdruecklicher Regel aus dem Titel
        (Nutzer-Meldung 03.08.). Bei einer einzigen, klar umrissenen Aufgabe ist es
        zuverlaessig – und das Ergebnis laesst sich im Code gegen den Rohtitel pruefen.
        Default (Basis/Mock): keine Erkennung.
        """
        return []

    async def normalize_variants(self, axes: dict, product_title: str = "") -> dict:
        """Varianten-Namen eindeutschen. Default: kein Mapping (Original bleibt)."""
        return {}

    async def research_trends(self, *, context: str = "", max_terms: int = 12) -> list[dict]:
        """KI-Trend-Scout: recherchiert aktuelle Verkaufstrends und leitet suchbare
        AliExpress-Keywords ab. Rueckgabe: [{"keyword", "reason", "category"}].
        Default (Basis): leer.
        """
        return []

    async def rank_source_candidates(self, *, ebay_title: str,
                                     candidates: list[dict]) -> list[dict]:
        """KI-Ranking: welche AliExpress-Bildsuche-Treffer sind DASSELBE Produkt?

        candidates: [{"aliexpress_id": str, "title": str, "price_eur": float}].
        Rueckgabe: [{"aliexpress_id": str, "confidence": float 0-1, "reason": str}].
        Default (Basis): leer -> Aufrufer nutzt die ungerankte Bildsuche-Reihenfolge.
        """
        return []

    async def match_variant(self, *, ebay_selection: dict, ali_variants: list[dict],
                            product_title: str = "") -> dict:
        """eBay-Kaeuferauswahl der passenden AliExpress-Variante zuordnen (KI-Agent).

        ebay_selection: {Achse: Wert} wie vom Kaeufer gewaehlt (z.B. {"Farbe": "Schwarz KI"}).
        ali_variants: [{"attr": str, "name": str, "price": str}] – die echten SKUs.
        Rueckgabe: {"attr": str, "confidence": float 0-1, "reasoning": str}.
        Default (Basis): leer -> Aufrufer faellt auf manuelle Zuordnung zurueck.
        """
        return {"attr": "", "confidence": 0.0, "reasoning": ""}

    async def suggest_bewirtung_anlaesse(self, *, ort: str = "",
                                         art: str = "", hinweis: str = "",
                                         vermeiden: list[str] | None = None,
                                         zuletzt_benutzt: list[str] | None = None) -> dict:
        """Mehrere Formulierungs-VORSCHLAEGE fuer den Anlass eines Bewirtungsbelegs.

        Das Finanzamt verlangt den KONKRETEN geschaeftlichen Zweck (§ 4 Abs. 5 EStG) –
        pauschale Angaben ("Geschaeftsessen", "Kundenpflege") erkennt es nicht an.

        NUR DAS THEMA, ohne Namen (Nutzer-Vorgabe): wer bewirtet wurde, steht auf der
        Quittung bzw. in der eigenen Zeile des Eigenbelegs. Die Gaeste werden dem
        Generator deshalb gar nicht erst uebergeben – so kann kein Name hineinrutschen.

        WICHTIG: das sind Formulierungs-Vorlagen, KEINE Tatsachenbehauptung. Der Nutzer
        waehlt den Vorschlag, der zum tatsaechlichen Treffen passt, und passt ihn an –
        deshalb kommen immer mehrere zur Auswahl.

        Zwei Sperrlisten, bewusst unterschiedlich streng (Nutzerwunsch: ausgewogen, nicht
        wiederholungsfrei – derselbe Grund DARF wiederkommen, nur nicht am Stueck):
        - `vermeiden`  (hart): kommt jetzt nicht vor – in dieser Sitzung schon gezeigt
          bzw. bei den letzten Bewirtungen direkt hintereinander benutzt.
        - `zuletzt_benutzt` (weich): kam frueher schon vor, darf wiederkommen, rutscht
          aber ans Ende der Auswahl.
        Rueckgabe: {"vorschlaege": [str, ...], "hinweis": str}.
        Default (Basis/Mock): rotierende Vorlagen ohne Modell.
        """
        gesperrt = {_norm_anlass(v) for v in (vermeiden or [])}
        abgenutzt = {_norm_anlass(v) for v in (zuletzt_benutzt or [])}
        frisch, gebraucht = [], []
        for satz in _BEWIRTUNG_VORLAGEN:
            key = _norm_anlass(satz)
            if key in gesperrt:
                continue
            (gebraucht if key in abgenutzt else frisch).append(satz)
        auswahl = frisch + gebraucht          # Ungenutztes zuerst, Bekanntes hinten dran
        # Stichwort gesetzt -> passende Vorlagen nach vorn (ohne Modell ist das die einzige
        # Moeglichkeit, das Stichwort ueberhaupt zu beruecksichtigen).
        stich = [w for w in _norm_anlass(hinweis).split() if len(w) > 3]
        if stich:
            def _treffer(satz: str) -> int:
                s = _norm_anlass(satz)
                return sum(1 for w in stich if w[:6] in s)
            auswahl = sorted(auswahl, key=_treffer, reverse=True)
        if not auswahl:  # alle Vorlagen gesperrt -> lieber wiederholen als nichts liefern
            auswahl = list(_BEWIRTUNG_VORLAGEN)
        return {"vorschlaege": auswahl[:3],
                "hinweis": "Bitte den Vorschlag wählen, der wirklich zutrifft, und anpassen."}

    async def revise_listing(self, *, instruction: str, current_title: str,
                             current_description: str, current_specifics: dict | None = None,
                             current_category: str | None = None) -> dict:
        """KI-Bearbeitung eines bestehenden Listings nach einer Freitext-Anweisung.

        Der Nutzer beschreibt in eigenen Worten, was geaendert werden soll (Titel/
        Beschreibung anders formulieren, Merkmale ergaenzen, andere Kategorie,
        Markenrechts-Ausnahmen im Titel usw.). Rueckgabe (Vorschlag, wird NICHT
        automatisch angewandt): {title_seo, description, item_specifics(dict),
        category_hint, note, warnings}.
        Default (Basis): unveraendert zurueck.
        """
        return {"title_seo": current_title, "description": current_description,
                "item_specifics": dict(current_specifics or {}), "category_hint": None,
                "note": "KI nicht verfuegbar", "warnings": []}

    async def analyze_market(self, *, product_title: str, variants: list[dict],
                             competitor_prices: list[dict],
                             current_price_eur: float | None = None) -> dict:
        """KI-Marktanalyse: aus Konkurrenzpreisen + eigenen Varianten eine
        Preisempfehlung je Variante + konkrete Push-Empfehlungen ableiten (propose-only).

        variants: [{key, name, ek_eur, current_price_eur}].
        competitor_prices: [{title, price_eur}].
        Rueckgabe: {price_recommendations: [{variant_key, recommended_price_eur, reasoning}],
                    push_recommendations: [str], market_note: str}.
        Default (Basis): leer -> Aufrufer nutzt die rein rechnerische Empfehlung.
        """
        return {"price_recommendations": [], "push_recommendations": [], "market_note": ""}

    async def read_purchase_receipt(self, *, image_bytes: bytes, media_type: str = "image/png") -> dict:
        """Vision: den AliExpress-BELEG (Bestelluebersicht als Bild) auslesen.

        Liefert die Felder, die auf dem Beleg WIRKLICH stehen – nichts wird ergaenzt,
        umgerechnet oder geraten (Projektregel 14). Unbekannt = None.
        Rueckgabe siehe ``_ReceiptSchema``; ``{}`` = nicht auslesbar.
        """
        return {}

    async def lies_aufdruck(self, *, product_title: str, image_urls: list[str]) -> dict:
        """Den AUFDRUCK von den Produktbildern ablesen (Vision).

        Bei Motiv-Bekleidung steht der Spruch fast immer NUR auf dem Bild - im
        AliExpress-Titel und in der Beschreibung taucht er nicht auf. Ohne ihn
        kann die Titelregel ihn auch nicht in den Titel schreiben (Nutzerbefund
        03.09.2026, Beispiel "Ernie Bert" mit dem Aufdruck "Lecker Bierchen").

        Rueckgabe::

            {"text": "Lecker Bierchen", "sicher": True,
             "sprache": "de", "zielgruppe": "Bierfreunde, Kumpels"}

        ``text`` ist LEER, wenn nichts eindeutig lesbar ist - dann wird nichts
        geraten. Ein erfundener Aufdruck landet im Titel und fuehrt zur
        Ruecksendung; eine Luecke kostet nur ein schlechteres Keyword.
        """
        return {"text": "", "sicher": False, "sprache": "", "zielgruppe": ""}

    async def beschreibe_motiv(self, *, product_title: str,
                               image_urls: list[str]) -> dict:
        """Das MOTIV auf dem Kleidungsstueck genau beschreiben (Vision).

        ``lies_aufdruck`` liest den Text, das hier beschreibt das Bild: Figur,
        Haltung, Stil, Farben mit Rolle, Aufbau, Effekte. Gebraucht wird das
        fuer das Motiv-Radar - aus einer Beschreibung laesst sich eine EIGENE
        Fassung erzeugen, aus einem Titel nicht.

        Rueckgabe siehe ``_MotivBeschreibungSchema``; alle Felder duerfen leer
        sein. Ein ``fehler``-Feld erscheint, wenn der Aufruf scheiterte - sonst
        saehe ein Fehlschlag aus wie "nichts zu sehen".
        """
        return {"motiv": "", "text_woertlich": "", "farben": [], "effekte": [],
                "sicher": False}

    async def assess_images(self, *, product_title: str, image_urls: list[str]) -> dict:
        """KI-Bildbewertung (Vision): welches Produktbild eignet sich am besten als TITELBILD?

        Bewertet jedes Bild 0-10 nach Eignung als eBay-Hauptbild (Produkt gross/scharf/zentriert,
        heller Hintergrund, keine eingebrannten Texte/Preise/Collagen/Wasserzeichen) und waehlt
        den Index des besten Titelbildes. Propose-only – aendert nichts.

        image_urls: Bild-URLs in Anzeige-Reihenfolge (Index 0 = aktuelles Titelbild).
        Rueckgabe: {"assessments": [{"index": int, "score": float, "reason": str}],
                    "best_index": int|None, "note": str}.
        Default (Basis): leer -> Aufrufer nutzt die vorhandene Reihenfolge/Heuristik.
        """
        return {"assessments": [], "best_index": None, "note": ""}

    @abc.abstractmethod
    async def suggest_title(self, *, current_title: str, competitor_titles: list[str],
                            internal_titles: list[str] | None = None) -> str:
        ...


# Regeln fuer die Beschreibungsbereinigung (China-Hinweise entfernen).
_CHINA_PATTERNS = [
    r"[一-鿿]+",                      # chinesische Zeichen
    r"\b(china|chinese|factory)\b",
    r"\b(\d{1,3}\s*-\s*\d{1,3}\s*days?)\b",   # Lieferzeit "15-30 days"
    r"\b(free\s*shipping|fast\s*shipping|包邮|直邮|发货)\b",
    r"\b(cheap|wholesale)\b",
    r"Hochbetriebenes Chemikalienunternehmen",  # AliExpress-Uebersetzungsmuell
    r"\bCN\s*\(Herkunft\)\b",
    r"\bUrsprung\b",
]


def _clean_description(text: str) -> tuple[str, list[str]]:
    warnings: list[str] = []
    cleaned = text
    for pat in _CHINA_PATTERNS:
        if re.search(pat, cleaned, flags=re.IGNORECASE):
            warnings.append(f"entfernt: Muster '{pat}'")
        cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" .,-")
    return cleaned, warnings


def _title_case(text: str) -> str:
    """Einfaches Title Case (Hauptwoerter gross) – nur fuer den Mock."""
    return " ".join(w[:1].upper() + w[1:] if w else w for w in text.split())


# Inhalte, die NIE in die KUNDEN-Beschreibung duerfen:
#  - Garantie-Versprechen (in DE bindend, § 479 BGB) + Haendler-Servicezeiten (AliExpress-Rohdaten)
#  - RECHTS-/MARKEN-/URHEBERRECHTS-HINWEISE & Haftungsausschluesse: das sind Hinweise fuer den
#    VERKAEUFER (gehoeren in strategic_note), nie fuer den Kaeufer. Vorfall 09.07.: die KI schrieb
#    "⚠️ WICHTIGER HINWEIS – Designs stehen moeglicherweise unter Markenrechten Dritter …" an den
#    ANFANG der Beschreibung -> der Kunde las eine Abmahn-Warnung. Muss ersatzlos raus.
# Bewusst NUR rechts-/marken-spezifische Begriffe. KEIN allgemeines "vor dem Kauf pruefen":
# das ist oft ein LEGITIMER Produkt-Passhinweis fuer den Kunden (z.B. "Antennengroesse vor
# dem Kauf ueberpruefen", "nur fuer flache Autodaecher") – der bleibt drin.
_FORBIDDEN_BLOCK_RE = re.compile(
    r"garantie|warranty|geld[- ]?zur(ü|ue)ck|money[- ]?back|gmt\s*[+-]\s*\d"
    r"|marken(recht|schutz|rechten|rechtlich)|urheberrecht|urheberrechtlich|copyright|trademark|\bvero\b"
    r"|lokale[nr]?\s+gesetze|geltende[nr]?\s+gesetze|rechtslage|auf eigene gefahr"
    r"|rechte?\s+dritter|von\s+dritten\s+stehen|haftungsausschluss|\babmahn",
    re.IGNORECASE)
# Reine Warn-Ueberschrift (nur fuer den Verkaeufer), auch alleinstehend als eigener Block entfernen.
_WARNING_HEADING_RE = re.compile(
    r"^\s*[⚠️❗️!]*\s*(wichtiger\s+hinweis|achtung|disclaimer|rechtlicher?\s+hinweis)\s*[:!]*\s*$",
    re.IGNORECASE)


def strip_forbidden_blocks(text: str, warnings: list[str] | None = None) -> str:
    """Absaetze mit Garantie-/Service- ODER Rechts-/Marken-/Urheberrechts-Hinweisen ersatzlos
    entfernen – solche Hinweise sind fuer den Verkaeufer, nie fuer den Kaeufer."""
    blocks = re.split(r"\n\s*\n", text or "")
    kept: list[str] = []
    removed = 0
    for b in blocks:
        if _FORBIDDEN_BLOCK_RE.search(b) or _WARNING_HEADING_RE.match(b.strip()):
            removed += 1
            continue
        kept.append(b)
    if removed and warnings is not None:
        warnings.append(f"{removed} unzulaessige(r) Block/Bloecke entfernt "
                        "(Garantie/Service ODER Rechts-/Marken-Hinweis – gehoert nicht in die Beschreibung)")
    return "\n\n".join(kept).strip()


class MockLLMClient(LLMClient):
    """Regelbasierter Mock – erzeugt das block-/emoji-strukturierte Format offline.

    Liefert dasselbe Schema wie der echte Client (strategic_note + Titel +
    Beschreibung mit Bloecken + Pflicht-Footer), damit der Upload-Flow ohne
    API-Key getestet werden kann. Inhaltlich natuerlich nur eine Naeherung.
    """

    async def research_trends(self, *, context="", max_terms=12):
        """Offline-Naeherung: feste Beispiel-Trends (kein Web/Wissen). Bewusst NICHT-
        elektronisch (Nutzerwunsch: weniger Elektronik)."""
        base = [
            ("Länder Fan Schal", "Sport-Event-Saison (WM/EM)", "Fan-Artikel"),
            ("Grill Thermometer Analog", "Grillsaison, viral auf TikTok", "Küche"),
            ("Auto Innenraum Reinigungsset", "Detailing-Dauerbrenner", "Auto"),
            ("Trockenblumen Strauß Deko", "Wohn-Deko-Trend (Pinterest)", "Deko"),
        ]
        return [{"keyword": k, "reason": r, "category": c} for k, r, c in base][:max_terms]

    async def rank_source_candidates(self, *, ebay_title, candidates):
        """Offline-Naeherung: Konfidenz aus Titel-Wortueberlappung (Jaccard)."""
        def _words(s):
            return {w for w in re.sub(r"[^a-z0-9 ]", " ", str(s or "").lower()).split() if len(w) > 2}
        et = _words(ebay_title)
        out = []
        for c in (candidates or []):
            ct = _words(c.get("title"))
            inter = len(et & ct)
            conf = round(inter / max(len(et | ct), 1), 2) if et and ct else 0.0
            out.append({"aliexpress_id": str(c.get("aliexpress_id") or ""),
                        "confidence": conf, "reason": f"mock: {inter} gemeinsame Wörter"})
        return out

    async def match_variant(self, *, ebay_selection, ali_variants, product_title=""):
        """Offline-Naeherung: NUR exakter, EINDEUTIGER Namensgleichheits-Treffer.

        Spiegelt bewusst die Mehrdeutigkeits-Sperre der Regel-Ebene: bei blossem
        Enthaltensein / Praefix-Kollision (Black vs. Black AI) oder mehreren
        Treffern -> leer, damit MOCK_LLM keine FALSCHE Variante mit hoher
        Konfidenz durchwinkt (das wuerde bei mock_llm=true + echtem AliExpress
        eine echte Fehlbestellung ausloesen)."""
        def _n(s):
            return re.sub(r"[^a-z0-9]", "", str(s or "").lower())
        sel = _n(" ".join(str(v) for v in (ebay_selection or {}).values()))
        names = [_n(v.get("name")) for v in (ali_variants or [])]
        exact = [v for v in (ali_variants or [])
                 if _n(v.get("name")) and _n(v.get("name")) != "aspicture"
                 and _n(v.get("name")) == sel]
        if len(exact) == 1:
            won = _n(exact[0].get("name"))
            # Praefix-Kollision (won ist Teil eines ANDEREN Namens, z.B. black in blackai)
            # -> mehrdeutig -> leer, damit MOCK_LLM nie die falsche Variante kauft.
            if not any(other != won and won in other for other in names):
                return {"attr": exact[0].get("attr", ""), "confidence": 0.9, "reasoning": "mock exact"}
        return {"attr": "", "confidence": 0.0, "reasoning": "mock: kein eindeutiger Exakt-Treffer"}

    async def generate_listing(self, *, title_raw, description_raw, category_guess,
                               specs=None, variant_axes=None, pflicht_namen=None,
                               vergebene_anfaenge=None):
        title_clean, _ = _clean_description(title_raw)
        title_seo = _title_case(title_clean)[:80].strip() or "Markenloses Produkt"
        desc_body, warnings = _clean_description(description_raw)
        haystack = f"{title_raw} {description_raw}".lower()

        # Strategischer Hinweis: Markenrisiko + SEO-Notiz
        flagged = [b for b in _VERO_BRANDS if b in haystack]
        if flagged:
            note = f"⚠ Markenrisiko (VeRO prüfen): {', '.join(flagged)}. "
        else:
            note = "Keine offensichtlichen Markenrisiken erkannt. "
        note = "[MOCK – ohne echtes LLM] " + note + "SEO: Haupt-Keyword vorne im Titel."
        if category_guess is None:
            warnings.append("category might be wrong")

        is_jewelry = any(k in haystack for k in
                         ["gold", "silber", "schmuck", "ring", "kette", "armband",
                          "necklace", "bracelet", "jewel"])

        # Specs -> "Maße & Details"-Block + item_specifics
        specs = specs or []
        spec_lines = [f"{s.get('name')}: {s.get('value')}" for s in specs
                      if s.get("name") and s.get("value")]
        item_specifics = {"Marke": "Markenlos"}
        for s in specs:
            n, v = s.get("name"), s.get("value")
            if n and v and len(item_specifics) < 12:
                item_specifics[str(n)] = str(v)
        # Varianten-Text aus den Achsen
        axes = variant_axes or {}
        var_text = (" · ".join(f"{k}: {', '.join(map(str, v))}" for k, v in axes.items())
                    if axes else "Verschiedene Optionen verfügbar (siehe Auswahl).")

        # eBay rendert HTML, nicht Markdown -> Emoji + Ueberschrift als Zeile, ohne Sternchen.
        blocks = [
            "✨ Auf einen Blick\nHochwertige Verarbeitung. Sofort einsatzbereit. Schneller Versand.",
            f"🎯 Highlight\n{title_seo}.",
            "🧵 Material & Qualität\n"
            + (desc_body[:200] if desc_body else "Sorgfältig ausgewählte Materialien für lange Haltbarkeit."),
            f"🎨 Varianten\n{var_text}",
        ]
        if spec_lines:
            blocks.append("📏 Maße & Details\n" + "\n".join(spec_lines[:10]))
        if is_jewelry:
            blocks.append(f"ℹ️ Hinweis zur Oberfläche\n{_JEWELRY_DISCLAIMER}")
        blocks.append("🎁 Anlass & Zielgruppe\nIdeal als Geschenk und für den täglichen Gebrauch.")
        blocks.append("📦 Lieferumfang\n1x Artikel wie abgebildet.")

        description = "\n\n".join(blocks) + "\n\n" + _FOOTER
        return GeneratedListing(
            title_seo=title_seo, description_clean=description,
            warnings=warnings, strategic_note=note, item_specifics=item_specifics,
        )

    async def suggest_title(self, *, current_title, competitor_titles, internal_titles=None):
        # Mock: haengt ein generisches High-CTR-Keyword an.
        base = current_title.split(" - ")[0][:60]
        return f"{base} - Premium Qualität, Blitzversand"

    async def analyze_market(self, *, product_title, variants, competitor_prices,
                             current_price_eur=None):
        # Mock: Median der Konkurrenzpreise als Orientierung; leichte Push-Hinweise.
        prices = sorted(float(c["price_eur"]) for c in (competitor_prices or [])
                        if c.get("price_eur"))
        median = prices[len(prices) // 2] if prices else None
        recs = []
        for v in (variants or []):
            rp = median if median else v.get("current_price_eur")
            if rp:
                recs.append({"variant_key": v.get("key"),
                             "recommended_price_eur": round(float(rp), 2),
                             "reasoning": "[MOCK] am Markt-Median orientiert"})
        push = []
        if median and current_price_eur and current_price_eur < median * 0.9:
            push.append("Preis liegt deutlich unter dem Marktmedian – Anhebung prüfen.")
        push.append("Anzeigentarif (Promoted Listings) leicht erhöhen für mehr Sichtbarkeit.")
        return {"price_recommendations": recs, "push_recommendations": push,
                "market_note": f"[MOCK] {len(prices)} Konkurrenzpreise ausgewertet."}

    async def revise_listing(self, *, instruction, current_title, current_description,
                             current_specifics=None, current_category=None):
        # Mock: deterministische, nachvollziehbare "Bearbeitung" (fuer Tests/Dev).
        specs = dict(current_specifics or {})
        instr = (instruction or "").strip()
        # Einfache Heuristik: 'kategorie 12345' -> category_hint; 'merkmal X: Y' -> spec
        cat = None
        m = re.search(r"kategorie\s*[:#]?\s*(\d{3,})", instr, re.I)
        if m:
            cat = m.group(1)
        for mm in re.finditer(r"merkmal\s+([^:]+):\s*([^\n;.]+)", instr, re.I):
            specs[mm.group(1).strip()] = mm.group(2).strip()
        title = current_title
        if "kürzer" in instr.lower() or "kurz" in instr.lower():
            title = current_title[:60].rstrip()
        return {
            "title_seo": title[:80],
            "description": current_description,
            "item_specifics": specs,
            "category_hint": cat,
            "note": f"[MOCK] Anweisung angewandt: {instr[:80]}",
            "warnings": [],
        }

    async def read_purchase_receipt(self, *, image_bytes, media_type="image/png"):
        """Offline-Beleg ohne echte Vision: fester Beispiel-Datensatz fuer Dev/Tests.

        Die Werte sind bewusst als ``[MOCK]`` erkennbar – taucht so ein Artikelname je
        in einer echten Rechnung auf, lief der Lauf faelschlich im Mock-Modus.
        """
        if not image_bytes:
            return {}
        return {
            "order_id": "3075412823902059", "order_date": "11. Aug. 2026",
            "items": [{"title": "[MOCK] Summer Fashion Slippers For Men",
                       "properties": "brown,44-45", "unit_price": 4.75, "quantity": 1}],
            "store_name": "Shop1103840307 Store", "store_number": None,
            "ship_to": "[MOCK] Empfaenger, Musterstr. 1", "payment_brand": "VISA",
            "subtotal": 4.75, "discount": None, "shipping": 1.76,
            "extra_lines": [{"label": "Geschätzte Einfuhrabgaben", "amount": 3.57}],
            "vat_included": None, "total": 10.08, "currency": "EUR",
        }

    async def lies_aufdruck(self, *, product_title, image_urls):
        """Attrappe: liest nichts, behauptet nichts.

        Bewusst IMMER leer statt eines erfundenen Beispieltexts. Ein Mock, der
        "Lecker Bierchen" zurueckgibt, wuerde in Tests und im Probebetrieb einen
        Aufdruck vortaeuschen, den es nicht gibt - und der landete im Titel.
        """
        return {"text": "", "sicher": False, "sprache": "",
                "zielgruppe": "", "note": "[MOCK] keine Bildanalyse"}

    async def beschreibe_motiv(self, *, product_title, image_urls):
        """Attrappe: beschreibt nichts, erfindet nichts.

        Aus demselben Grund leer wie ``lies_aufdruck``: eine erfundene
        Beschreibung wuerde zu einem erzeugten Bild fuehren, das Budget kostet.
        """
        return {"motiv": "", "text_woertlich": "", "text_anordnung": "",
                "schrift": "", "stil": "", "farben": [], "komposition": "",
                "effekte": [], "ware_farbe": "", "zielgruppe": "", "thema": "",
                "sicher": False, "note": "[MOCK] keine Bildanalyse"}

    async def assess_images(self, *, product_title, image_urls):
        """Offline-Naeherung ohne echte Vision: deterministische, positions-basierte Scores
        (das aktuelle Titelbild/Index 0 gilt als bester Kandidat). Nur fuer Tests/Dev –
        MOCK erkennt keine Text-/Collage-Bilder."""
        urls = [u for u in (image_urls or []) if u]
        if not urls:
            return {"assessments": [], "best_index": None, "note": ""}
        assessments = [{"index": i, "score": max(3.0, round(8.0 - 0.5 * i, 1)),
                        "reason": ("[MOCK] aktuelles Titelbild" if i == 0
                                   else f"[MOCK] Position {i + 1}")}
                       for i in range(len(urls))]
        return {"assessments": assessments, "best_index": 0,
                "note": f"[MOCK] {len(urls)} Bilder ohne echte Bildanalyse bewertet."}


# --- System-Prompt (Vorgabe des Auftraggebers) ----------------------------
# Wortlaut des Auftraggeber-Prompts; am Ende um eine Output-Adapter-Anweisung
# ergaenzt, damit das Ergebnis ueber die JSON-Schema-Felder zurueckkommt.
def groessentabelle(specs) -> str | None:
    """``size_info`` aus den AliExpress-Specs in eine lesbare Tabelle uebersetzen.

    Bisher ging das Feld als roher JSON-Brocken in den Prompt:

        - size_info: {"sizeInfoList":[{"length":{"cm":"92"},"size":"S"}, ...]}

    Die KI musste ihn selbst deuten und hat den Schluessel ``length`` mit "Laenge"
    uebersetzt. Ergebnis im Listing: "Groesse S: Laenge 92 cm". Fuer ein T-Shirt in
    Groesse S sind 92 cm aber kein Laengenmass, sondern der Brustumfang - der
    Haendler beschriftet seine Spalte falsch.

    Die Zahl stimmt, die Deutung nicht. Deshalb wird hier NICHT uebersetzt: der
    Feldname bleibt so stehen, wie die Quelle ihn liefert, und die Tabelle wird als
    Herstellerangabe gekennzeichnet. Wer 92 cm liest und "Brustumfang" denkt, liegt
    richtig; wer "Laenge" liest, schickt zurueck.
    """
    roh = None
    for s in (specs or []):
        if str(s.get("name", "")).strip().lower() == "size_info":
            roh = s.get("value")
            break
    if roh is None:
        return None
    try:
        daten = json.loads(roh) if isinstance(roh, str) else roh
        liste = daten.get("sizeInfoList") or []
    except (ValueError, TypeError, AttributeError):
        return None

    zeilen = []
    for eintrag in liste:
        if not isinstance(eintrag, dict):
            continue
        groesse = str(eintrag.get("size") or "").strip()
        masse = []
        for feld, wert in eintrag.items():
            if feld == "size" or not isinstance(wert, dict):
                continue
            cm = str(wert.get("cm") or "").strip()
            if cm:
                masse.append(f"{feld} {cm} cm")   # Feldname UNUEBERSETZT, s. Docstring
        if groesse and masse:
            zeilen.append(f"{groesse}: " + ", ".join(masse))
    if not zeilen:
        return None
    kopf = ("Groessentabelle des Herstellers (unveraendert uebernommen, "
            "Feldnamen wie in der Quelle):")
    return kopf + chr(10) + chr(10).join("  " + z for z in zeilen)


def _tabelle_fuer_kaeufer(specs) -> str | None:
    """Die Groessentabelle so, wie sie im Angebot stehen soll.

    Liefert die Quelle je Groesse GENAU EIN Mass, entfaellt der Feldname ganz: wir
    wissen nicht, was gemessen wurde, und duerfen es nicht behaupten. Der Kaeufer
    bekommt die Zahl des Herstellers, ohne falsche Zuschreibung.

    Gibt es MEHRERE Masse je Groesse (Laenge + Breite + Aermel), bleiben die Namen
    der Quelle stehen - dann waeren nackte Zahlen nicht zuzuordnen.
    """
    roh = None
    for s in (specs or []):
        if str(s.get("name", "")).strip().lower() == "size_info":
            roh = s.get("value")
            break
    if roh is None:
        return None
    try:
        liste = (json.loads(roh) if isinstance(roh, str) else roh).get("sizeInfoList") or []
    except (ValueError, TypeError, AttributeError):
        return None

    eintraege = []
    mehrfach = False
    for e in liste:
        if not isinstance(e, dict):
            continue
        groesse = str(e.get("size") or "").strip()
        masse = [(f, str(w.get("cm") or "").strip()) for f, w in e.items()
                 if f != "size" and isinstance(w, dict) and str(w.get("cm") or "").strip()]
        if not groesse or not masse:
            continue
        if len(masse) > 1:
            mehrfach = True
        eintraege.append((groesse, masse))
    if not eintraege:
        return None

    zeilen = []
    for groesse, masse in eintraege:
        if mehrfach:
            zeilen.append(f"• {groesse}: " + ", ".join(f"{f} {cm} cm" for f, cm in masse))
        else:
            zeilen.append(f"• {groesse}: {masse[0][1]} cm")
    return ("Größentabelle des Herstellers (Maßangaben unverändert übernommen):"
            + chr(10) + chr(10).join(zeilen))


# Eine Zeile der vom Modell gebauten Groessentabelle: Groesse, Doppelpunkt, irgendwo
# ein cm-Mass. Das cm ist Pflicht, damit "• M: Maschinenwaesche" nicht mitgeloescht
# wird - getroffen werden soll nur die Tabelle, nicht der uebrige Massblock.
_GROESSEN_ZEILE = re.compile(
    r"^\s*[•\-*]?\s*(?:(?:größe|grösse|groesse|size)\s+)?"
    # \b ist Pflicht: ohne sie faengt "L" auch "Lieferumfang: 1 Stueck, 30 cm" und
    # "Länge 112 cm" - die erste Zeile wuerde faelschlich geloescht.
    r"(?:XS|S|M|L|XL|XXL|XXXL|\d+XL|\d+)\b"
    # Trennzeichen optional: Listing #8 kam als "• S 92 cm" heraus, ganz ohne.
    r"\s*[:\-–=]?\s*.*?\d+\s*cm",
    re.IGNORECASE)


def _quell_masse(specs) -> set:
    """Die cm-Werte, die in size_info stehen - unabhaengig von jeder Formatierung."""
    roh = None
    for s in (specs or []):
        if isinstance(s, dict) and str(s.get("name", "")).strip().lower() == "size_info":
            roh = s.get("value")
            break
    if roh is None:
        return set()
    try:
        liste = (json.loads(roh) if isinstance(roh, str) else roh).get("sizeInfoList") or []
    except (ValueError, TypeError, AttributeError):
        return set()
    werte = set()
    for e in liste:
        if not isinstance(e, dict):
            continue
        for feld, w in e.items():
            if feld != "size" and isinstance(w, dict) and str(w.get("cm") or "").strip():
                werte.add(str(w["cm"]).strip())
    return werte


def _ist_tabellenzeile(zeile: str, quellwerte: set) -> bool:
    """Enthaelt die Zeile die Groessentabelle des Modells?

    Zwei Erkennungswege, weil das Modell das Format frei waehlt:

    1. Eine Groesse je Zeile ("• Groesse S: Laenge 92 cm") - das Muster.
    2. Die ganze Tabelle in EINER Zeile ("• Groessen: S (92 cm), M (102 cm), ...").
       Genau so kam Listing #14 heraus, nachdem Weg 1 schon repariert war.

    Weg 2 erkennt an INHALT statt an Form: stehen mehrere Maszahlen aus der Quelle
    zusammen mit "cm" in einer Zeile, ist es die Tabelle. Formate zu erraten war der
    Fehler - die Zahlen kennen wir dagegen genau.
    """
    if _GROESSEN_ZEILE.match(zeile):
        return True
    if "cm" not in zeile.lower() or len(quellwerte) < 2:
        return False
    treffer = sum(1 for w in quellwerte if re.search(rf"\b{re.escape(w)}\s*cm", zeile, re.I))
    return treffer >= 2


# Falsch gebildete Wortformen, die das Modell fuer Bekleidung erfindet. Nutzerregel
# 29.08.2026: "Overgroessen hoert sich falsch an, hier dann lieber den englischen
# begriff der aber auch in deutschland etabliert ist, oversize benutzen".
#
# "Overgroessen" ist ein Zwitter aus englisch "oversize" und deutsch "Groessen" - kein
# Wort, nach dem jemand sucht. "Oversize" dagegen ist im deutschen Modehandel
# etabliert und ein echtes Such-Keyword.
_SCHREIBWEISE = (
    (re.compile(r"\bOver\s?gr(?:ö|oe)(?:ss|ß)e?n?\b", re.IGNORECASE), "Oversize"),
)
# "Oversized" bleibt bewusst stehen (Nutzerentscheidung 29.08.2026: "Oversized kann
# gerne auch bleiben") - anders als "Overgroessen" ist es korrektes Englisch und wird
# im deutschen Modehandel auch so gesucht.


def korrigiere_schreibweise(text: str) -> str:
    """Erfundene Mischformen auf die etablierte Schreibweise bringen."""
    s = str(text or "")
    for muster, ersatz in _SCHREIBWEISE:
        s = muster.sub(ersatz, s)
    return s


def erzwinge_groessentabelle(description: str, specs, warnings: list) -> str:
    """Den Maß-Block durch die Tabelle aus der Quelle ersetzen.

    Die Prompt-Regel allein reicht nicht. Im Lauf vom 28.08.2026 schrieb das Modell
    trotz ausdruecklicher Anweisung weiter "Länge 92 cm" - es uebersetzte den
    Feldnamen "length", obwohl 92 cm bei Groesse S der Brustumfang ist. Es hatte den
    Hinweis sogar teilweise befolgt ("Herstellergrößentabelle:") und trotzdem
    weiteruebersetzt.

    Gleiche Doktrin wie [[repair_spec_measurements]] und [[preserves_measurements]]:
    bei Massen wird nachgeprueft, nicht vertraut. Eine falsche Massangabe kostet
    Ruecksendungen, und die traegt der Haendler.
    """
    tabelle = _tabelle_fuer_kaeufer(specs)
    if not tabelle:
        return description
    NL = chr(10)
    quellwerte = _quell_masse(specs)
    # Zeilenenden vereinheitlichen: die Funktion trennt an Leerzeilen, und mit
    # \r\n faende sie keine einzige (Befund 28.08.2026).
    description = description.replace(chr(13) + NL, NL).replace(chr(13), NL)
    bloecke = description.split(NL + NL)

    # Schritt 1: Tabellenzeilen des Modells UEBERALL herausnehmen, nicht nur im
    # ersten Massblock. Listing #1 hatte sie in ZWEI Bloecken; eine Fassung, die nach
    # dem ersten Treffer aufhoerte, liess die zweite stehen - der Kaeufer sah zwei
    # widersprechende Tabellen. Alles andere bleibt: Material, Kragen, Aermelstil,
    # Pflegehinweis, Messtoleranz. Eine fruehere Fassung ersetzte den GANZEN Block
    # und loeschte genau diese Angaben bei fuenf von zwanzig Entwuerfen.
    for i, b in enumerate(bloecke):
        zeilen = b.split(NL)
        behalten = [z for z in zeilen
                    if "größentabelle" not in z.lower()
                    and "grössentabelle" not in z.lower()
                    and not _ist_tabellenzeile(z, quellwerte)]
        if len(behalten) != len(zeilen):
            bloecke[i] = NL.join(behalten)
    bloecke = [b for b in bloecke if b.strip()]     # leer geraeumte Bloecke weg

    # Schritt 2: die richtige Tabelle GENAU EINMAL einsetzen - in den Massblock,
    # sonst vor den Footer (der § 19-Hinweis muss zuletzt stehen, gleiches Vorgehen
    # wie ensure_variant_values_in_description).
    for i, b in enumerate(bloecke):
        if b.lstrip().startswith("📏"):
            bloecke[i] = b.rstrip() + NL + tabelle
            neu = (NL + NL).join(bloecke)
            break
    else:
        einschub = "📏 Maße & Details" + NL + tabelle
        rumpf = (NL + NL).join(bloecke)
        from app.brand_filter import _FOOTER_MARKER
        if _FOOTER_MARKER in rumpf:
            kopf, _, fuss = rumpf.partition(_FOOTER_MARKER)
            neu = f"{kopf.rstrip()}{NL}{NL}{einschub}{NL}{NL}{_FOOTER_MARKER}{fuss}"
        else:
            neu = rumpf + NL + NL + einschub

    if neu == description:
        return description          # stand schon richtig da - keine Warnung
    warnings.append("Größentabelle aus der Quelle eingesetzt "
                    "(Modell hatte die Maßangabe umgedeutet oder ausgelassen)")
    return neu


_LISTING_SYSTEM = """Du erstellst eBay-Listings für meinen Dropshipping-Shop (Kleinunternehmer nach § 19 UStG, eBay.de, deutscher Markt). Ich liefere dir Produktinformationen aus AliExpress und du gibst mir einen SEO-optimierten Titel plus eine vollständige Beschreibung.

TITEL-REGELN
WICHTIGSTE REGEL – Titelanfang: Der Titel MUSS mit dem PRODUKTTYP (Substantiv) beginnen – also mit dem Wort, das Käufer in die eBay-Suche eintippen. eBay gewichtet die ersten Wörter am stärksten. Beschreibende Wörter (Farbe, Material, Stil, Herkunft, Zielgruppe, Karat/Mengenangabe wie edel, schwarz, japanisch, 925er, Damen, 3-teilig) stehen NIEMALS am Anfang, sondern dahinter – und dort in der Grundform statt gebeugt.
    FALSCH: "Edle Schwarze Vase Deko Japanisch"   RICHTIG: "Vase Deko Edel Schwarz Japanisch"
    FALSCH: "925er Silber Halskette Damen"        RICHTIG: "Halskette Damen Silber 925er"
    FALSCH: "Hochwertiges Mikrofaser Auto-Waschset"  RICHTIG: "Auto-Waschset Mikrofaser 9-teilig"
Format: 72 bis 80 Zeichen, Title Case (Hauptwörter Großgeschrieben).
Trennzeichen: Kommas, Ampersand (&) für klare Strukturierung statt Keyword-Stuffing.
Stil: Keyword-Reihenfolge geht VOR Lesefluss – der Titel ist eine Suchzeile, kein Werbesatz. Präpositionen wie "für" und "mit" sparsam nutzen. Hyphenated Compounds wo sinnvoll (Mikrofaser-Autowaschtuch, Eltern-Kind-Outfit).
Inhalt: Produkttyp ZUERST, danach Hauptmerkmale, Größen, Mengen, Farben, Anwendungsbereich. Vorteils-Phrasen (Weich & Schnell Trocknend) sind erwünscht – aber erst NACH dem Produkttyp.
Markenlogik (Stand 03.08.2026 – wir listen ausschliesslich zertifizierte Originalware):
    Marken-, Lizenz- und Figurennamen aus dem Originalmaterial GEHOEREN IN DEN TITEL und in die Beschreibung. Sie sind das wichtigste Such-Keyword: genau danach sucht der Kaeufer. Beispiele: BT21, Chiikawa, One Piece, Demon Slayer, Michael Jackson, Sanrio, Hello Kitty, Pokemon, Disney.
    NIE einen erkannten Marken-/Lizenznamen weglassen, umschreiben oder durch eine allgemeine Bezeichnung ersetzen (FALSCH: "Michael Jackson Cosplay-Puppe" zu "Cosplay Tänzer Puppe"; FALSCH: "BT21 Schluesselanhaenger" zu "Cartoon Schluesselanhaenger").
    Der Titel beginnt weiterhin mit dem PRODUKTTYP, der Markenname folgt direkt dahinter: "Schluesselanhaenger BT21 Chimmy Cooky Koya ...".
    Reine No-Name-Herstellerkuerzel ohne Suchvolumen bleiben weg (Beispiele: anniyo, cazador, SEAMETAL, WIFRU) – das betrifft NUR bedeutungslose Shop-Kuerzel, niemals bekannte Marken oder Lizenzen.
    VARIANTEN-NAMEN: Die Optionswerte der Varianten (z.B. Motive, die nach Personen, Figuren oder Serien benannt sind) werden im Varianten-Block der Beschreibung WOERTLICH aufgezaehlt. Nie durch "verschiedene Motive" o.ae. ersetzen – der Kaeufer waehlt genau danach aus. Sind es mehr als sechs, die ersten sechs nennen und mit "u.a." abschliessen.
    NIEMALS eine Marke erfinden, raten oder aus dem Produkttyp ableiten. Gueltig ist NUR eine Marke, die WOERTLICH IM PRODUKTNAMEN (Rohtitel oder Beschreibung) steht. Steht dort keine, ist die Ware markenlos – dann auch KEINE Marke in den Titel schreiben.
    Das Marken-Feld des Haendlers in den Specs ist KEIN Beleg: Haendler tragen dort oft pauschal denselben Namen fuer den ganzen Shop ein (real: eine Michael-Jackson-Puppe als "Bandai"). Solche Angaben werden ignoriert.
MOTIV-BEKLEIDUNG – DER SPRUCH GEHÖRT IN DEN TITEL (Nutzerregel 03.09.2026):
    Trägt das Kleidungsstück einen SPRUCH oder Schriftzug, MUSS er – oder sein prägnantester Teil – im Titel stehen. Danach sucht der Käufer, nicht nach "Baumwolle O-Ausschnitt". Ist der Spruch englisch, nimm die zugkräftigen Wörter wörtlich UND ergänze die deutsche Entsprechung, wenn beides in 80 Zeichen passt.
    FALSCH: "T-Shirt Ernie Bert Druck Baumwolle O-Ausschnitt Übergröße Unisex"
    RICHTIG: "Sprüche-Shirt Ernie Bert Lecker Bierchen Witzig Herren Baumwolle"
    Dazu ZWEI Sorten Wörter, weil Käufer auf beiden Wegen suchen:
    a) STIMMUNG – mindestens eines aus: Witzig, Lustig, Humor, Spruch, Sprüche, Fun, Sarkasmus, Ironie. Das ist die Kaufabsicht ("ich will etwas Lustiges verschenken").
    b) ZIELGRUPPE / ANLASS – wen spricht der Spruch an: Papa, Vater, Mama, Opa, Oma, Ehemann, Ehefrau, Tochter, Sohn, Kollege, Geburtstag, Vatertag, Weihnachten, Junggesellenabschied.
    Beispiel: Aufdruck "You can't scare me I have two daughters and a wife" → Zielgruppe sind Väter mit Töchtern und Ehefrau.
    RICHTIG: "Sprüche-Shirt Papa Zwei Töchter & Ehefrau Witzig Vatertag Geschenk"
    Beides gehört hinter den Produkttyp, nie davor. Und beides nur, wenn es STIMMT: Ist kein Spruch erkennbar, wird keiner erfunden, und ohne erkennbare Zielgruppe bleibt sie weg. Ein erfundener Spruch führt zur Rücksendung.
    Kennst du den Aufdruck nicht (er steht oft NUR auf dem Bild und in keinem Text), dann schreib nichts dazu – rate nicht.
    NUR STICHWORTE, nicht der ganze Spruch (Nutzerregel 03.09.2026: "nicht der gesamte aufdruck muss in den titel. Nur stichworte reichen manchmal"). 80 Zeichen sind knapp – ein hineingequetschter Satz verdrängt den Produkttyp und die Suchbegriffe. Nimm die zugkräftigen Wörter:
        Aufdruck "TO DO LISTE 1 KAFFEE TRINKEN 2 NIEMANDEN TÖTEN 3 SCHRITT 1 & 2 WIEDERHOLEN" → "Kaffee Niemanden Töten", nicht die ganze Liste.
        Aufdruck "YOU CAN'T SCARE ME I HAVE TWO DAUGHTERS AND A WIFE" → "Two Daughters And A Wife".
    KEINE DURCHGEHENDE GROSSSCHRIFT (Nutzerregel 03.09.2026: "Titel nie komplett in grossschrift, ganz normal erster buchstabe gross und restliche klein"). Aufdrucke stehen auf dem Shirt meist in Versalien – im Titel wird daraus Title Case.
        FALSCH: "Sprüche-Shirt TEAM LECKER BIERCHEN Witzig Herren"   RICHTIG: "Sprüche-Shirt Team Lecker Bierchen Witzig Herren"
    NUR DEUTSCH UND ENGLISCH (Nutzerregel 03.09.2026: "italienisch und andere sprachen ausser englisch kann raus"). Ist der Aufdruck italienisch, französisch oder in einer anderen Sprache, kommt er NICHT in den Titel – lieber gar kein Spruch als einer, nach dem in Deutschland niemand sucht. Beschreibe den Artikel dann ohne ihn.

Bei mehreren Listings für ähnliche Produkte: Einstiegs-Keyword variieren für SEO-A/B-Testing – aber IMMER unter Produkt-Synonymen (Vase/Blumenvase/Deko-Vase), nie über ein beschreibendes Wort.
    Bekommst du einen Abschnitt "BEREITS VERGEBENE TITELANFAENGE", dann fängt dein Titel mit KEINEM dieser Wörter an. Nimm ein anderes Substantiv, nach dem Käufer suchen. Bei Bekleidung sind das z.B. Herrenshirt, Damenshirt, Baumwollshirt, Motiv-Shirt, Sprüche-Shirt, Print-Shirt, Funshirt, Oversize-Shirt – jeweils passend zum tatsächlichen Produkt. Zehn identisch beginnende Titel konkurrieren in der eBay-Suche gegeneinander statt gegen fremde Angebote.
    Das Ausweich-Wort muss das Produkt aber WEITERHIN KORREKT benennen. Lieber denselben Anfang ein zweites Mal als eine falsche Warenart: Ein T-Shirt ist kein Hemd, keine Bluse, kein Pullover und kein Top. FALSCH: "Freizeithemd Pew Pew Madafakas" für ein T-Shirt. RICHTIG: "Sprüche-Shirt Pew Pew Madafakas". Wer ein Hemd sucht und ein T-Shirt bekommt, schickt zurück.

BESCHREIBUNGS-STRUKTUR
Block-Aufbau mit Emoji-Headern. Jeder Block: Emoji + Fettkopf + 1 bis 2 Sätze.
Standard-Blöcke:
    Hook (kurz, evokativ, drei kurze Statements)
    Hauptmerkmal/USP
    Material/Qualität
    Varianten (Farben, Größen, Mengen)
    Oberflächen-Hinweis bei Goldfarbe/Silberfarbe (siehe unten)
    Maße/Spezifikationen
    Anwendungsbereiche
    Zielgruppe/Anlass
    Lieferumfang
    Footer (siehe unten)
Pflicht-Disclaimer für beschichtete Schmuckstücke: "Goldfarbig und silberfarbig beschichteter Edelstahl. Modeschmuck, kein Echtgold oder Echtsilber."

Standard-Footer (immer am Ende, unverändert):
📦 Sobald Ihr Paket unterwegs ist, erhalten Sie selbstverständlich eine Sendungsverfolgung, sodass Sie jederzeit wissen, wann Ihr Produkt bei Ihnen ankommt.
📩 Fragen? Unser Kundenservice hilft jederzeit gerne.
Als Kleinunternehmer im Sinne von § 19 Abs. 1 UStG wird keine Umsatzsteuer berechnet

SPRACH-REGELN
Keine Bindestriche oder Em-Dashes als Satz-Punktuation im Fließtext, nur in Komposita (EU-MDR, KI-gestützt) und in Titeln zur Variantentrennung erlaubt.
Keine geschlechtergerechten Doppelformen, generisches Maskulinum oder klare Zielgruppen-Bezeichnungen (Damen, Herren, Unisex, Kinder).
Keine "nicht ... sondern" Konstruktionen.
Keine schließenden Floskeln (kein "Gibt es noch etwas...").
Keine Qualifier wie "laut Bildern" oder "laut Beschreibung".
Verneinungen wenn möglich vermeiden, positive Formulierungen bevorzugen.

STRATEGISCHE PRÜFUNGEN (vor jedem Listing)
    Marke: Steht im Originalmaterial ein Marken- oder Lizenzname? Dann gehoert er in Titel, Beschreibung und in das Merkmal "Marke". Nicht entfernen, nicht erfinden.
    Widersprüche: Stimmen Specs und Produkttext überein? Bei Inkonsistenzen flaggen und Klärung empfehlen.
    Falsche Vermarktung: Wird das Produkt strategisch falsch positioniert? (Beispiel: Tengri-Symbol fälschlich als Islam vermarktet, GPS-Tracker der nur Bluetooth ist)
    Zielgruppen-Optimierung: Bei Identitäts-Produkten relevante Diaspora-Begriffe einbinden (Filistin/Falastin für Palästina, Türkiye für Türkei, Shir-o Khorshid für Iran, Polski/Polska für Polen etc.)

FORMAT-DETAILS (aus bestehenden Top-Listings gelernt)
- KEINE Markdown-Sternchen verwenden. eBay rendert HTML, nicht Markdown. Jeder Block: Emoji + knappe Überschrift als eigene Zeile, darunter 1 bis 3 Sätze.
- Mehrteilige Sets: jede Komponente als eigene Zeile mit Maß/Größe und Einsatzzweck auflisten (z. B. "Waschhandschuh aus Chenille-Mikrofaser, 26 x 19 cm für satten Schaum").
- "Vielseitig einsetzbar"-Block als kommagetrennte Aufzählung der Anwendungsbereiche.
- Maschinen-Übersetzungsmüll aus den Rohdaten ignorieren (z. B. "Hochbetriebenes Chemikalienunternehmen", "Keine", "CN (Herkunft)", Platzhalter-Specs).
- RECHTLICH ZWINGEND: NIEMALS Garantie-Versprechen in die Beschreibung ("Garantie", "12 Monate Garantie", "Warranty", "Geld-zurück-Garantie" o. ä.) – eine Garantie ist in Deutschland rechtlich bindend und darf nicht aus Händlerdaten übernommen werden. Ebenso NIEMALS Servicezeiten/Kundendienst-Angaben des AliExpress-Händlers übernehmen (z. B. "Mo–Sa 9–18 Uhr GMT+7"). Solche Blöcke ersatzlos weglassen.
- Identitäts-/Diaspora-Produkte: relevanten Begriff im Titel ZWEISPRACHIG nennen (Deutsch + Landessprache), z. B. "Syrien Syria", "Polen Polska Pole", "Türkei Türkiye". Einen kulturellen/historischen Kontext-Block ergänzen.
- Nur die tatsächlich bestellten Varianten nennen (z. B. 2 von 4 Farben). Mehrlängen-Ketten als Vorteil darstellen (z. B. 45cm plus 5cm Verlängerung).
- Im Titel ist "–" (Gedankenstrich) als Trenner erlaubt; im Fließtext nicht.

STILBEISPIEL (gekürzt – an diesem Ton/Format orientieren)
Titel: Mikrofaser Auto-Waschset 9-teilig mit Handschuh, Bürste & Schwamm in Grau/Orange
Beschreibung:
🚗 Neunteiliges Komplett-Set. Zwei Farben. Volle Detailing-Ausstattung.
Dieses Mikrofaser-Auto-Waschset vereint neun Komponenten in einem Paket, ausgelegt für Innen- und Außenreinigung.
📦 Neun Komponenten in einem Set
Waschhandschuh aus Chenille-Mikrofaser, 26 x 19 cm für satten Schaum und kratzfreie Wäsche.
Radbürste mit 25 x 5 cm Reichweite für Felgen und Radmuttern.
🛡️ Lackschonend und kratzfrei
Weiche Borsten und Fasern reinigen kraftvoll und bleiben sicher für Lack, Glas und Innenraum.
(danach Anwendungsbereiche, Lieferumfang und der unveränderte Standard-Footer.)

STRUKTURIERTE DATEN NUTZEN
- Du bekommst zusaetzlich PRODUKT-SPECS (Name/Wert) und VARIANTEN (Achsen + Optionen) als Eingabe.
- Nutze die Specs fuer den "Maße & Details"-Block UND fuer die item_specifics. Ignoriere Muell/Marke.
- GROESSENTABELLE: Kommt in den Specs eine "Groessentabelle des Herstellers", uebernimm sie mit ihren Feldnamen und Zahlen UNVERAENDERT und schreibe darueber, dass es eine Herstellerangabe ist. Deute NIEMALS, was gemessen wurde: aus dem Feld "length" wird NICHT "Laenge". Haendler beschriften diese Spalten regelmaessig falsch (real: 92 cm bei Groesse S – das ist der Brustumfang, kein Laengenmass). Eine falsche Massangabe fuehrt zu Ruecksendungen; die neutrale Uebernahme nicht.
- Nenne die tatsaechlich verfuegbaren Varianten-Achsen konkret (z.B. "Metallfarbe: Gold, Stahl · Laenge: 45 cm, 60 cm").
- So viele SINNVOLLE Produktdetails wie moeglich uebernehmen (Material, Maße, Form, Anlass, Zielgruppe) – aber nichts Unsinniges/Widerspruechliches.
- Mengen-/Set-Angaben ("3er-Set", "2 Paar", "5 Stück") in Titel UND Beschreibung NUR, wenn die Quelldaten sie WOERTLICH nennen. NIEMALS aus Produktbildern schaetzen oder aus dem Motiv ableiten (ein Charakter mit drei Ohrringen macht den Artikel NICHT zum 3er-Set).

AUSGABE (Output-Adapter)
Gib das Ergebnis AUSSCHLIESSLICH ueber die Schema-Felder zurueck:
- strategic_note: der strategische Hinweis NUR FUER DEN VERKAEUFER (Markenrisiken, Widersprueche, SEO-Strategie). Markenrechtsverletzungen hier deutlich kennzeichnen. ALLE rechtlichen/Marken-/Urheberrechts-Warnungen und Haftungsausschluesse gehoeren AUSSCHLIESSLICH hierher – NIE in die Beschreibung.
- title_seo: NUR der Titel. Reize die Laenge bewusst aus – Ziel 75 bis 80 Zeichen (harte Obergrenze 80). Lieber mit zusaetzlichen relevanten Keywords/Synonymen auffuellen als kurz lassen. Title Case, ohne fuehrende Nummerierung.
- description_clean: die VOLLSTAENDIGE Produktbeschreibung mit allen Emoji-Bloecken und dem unveraenderten Standard-Footer am Ende. NIEMALS rechtliche Hinweise, Marken-/Urheberrechts-Warnungen, "WICHTIGER HINWEIS"-Abmahn-Bloecke oder Haftungsausschluesse in die Beschreibung schreiben – das liest der KUNDE; solche Hinweise NUR in strategic_note. WICHTIG fuer die Lesbarkeit: Trenne jeden Block durch eine LEERZEILE (doppelter Zeilenumbruch). Jeder Emoji-Block beginnt mit einer fetten Kurz-Ueberschrift (Emoji + Stichwort) in einer eigenen Zeile, darunter 1-2 Saetze. Aufzaehlungen (z.B. Lieferumfang, Maße & Details) als einzelne Zeilen, jede mit "• " beginnend. Keine Textwueste – klare Absaetze.
- item_specifics: LISTE von Merkmalen als {name, value} (deutsch), z.B. name="Marke" value="Markenlos"; name="Metall" value="Edelstahl"; name="Stil" value="Anhänger"; name="Material" value="Edelstahl"; name="Anlass" value="Jahrestag"; name="Abteilung" value="Damen". Immer "Marke" setzen: den Marken-/Lizenznamen, der WOERTLICH IM PRODUKTNAMEN (Rohtitel/Beschreibung) steht (z.B. "BT21", "Chiikawa", "One Piece", "Michael Jackson"); steht dort keiner, "Markenlos". Die Marke NIE raten, aus dem Produkttyp ableiten oder aus dem Marken-Feld der Haendler-Specs uebernehmen – ein falscher Markenname ist eine Falschangabe im Listing. Aus den Specs ableiten; RICHTWERT 12 Merkmale (10 bis 14) – je mehr desto besser fuer die eBay-Suche, aber nur inhaltlich korrekte Merkmale, nichts erfinden. Auch Farbe, Groesse/Masse, Gewicht, Themen/Motive, Einsatzbereich nutzen, wenn ableitbar. NIE ein Herkunfts-/Ursprungsland "China" (oder "CN") als Merkmal ausgeben – Herkunftsland-Merkmale mit China-Wert komplett weglassen. Werte IMMER deutsch; trifft ein Standard-Merkmal nicht zu, als Wert "Nicht zutreffend" schreiben (NIE englisch "Does not apply").
- warnings: kurze Stichpunkte zu fehlenden Daten oder noetigen Klaerungen."""


class _Aspect(BaseModel):
    name: str = Field(description="Merkmalname, z.B. 'Marke', 'Metall', 'Stil', 'Anlass'")
    value: str = Field(description="Merkmalwert, z.B. 'Markenlos', 'Edelstahl', 'Anhänger'")


class _NamePair(BaseModel):
    old: str = Field(description="Originalwert exakt wie in der Eingabe")
    new: str = Field(description="deutscher Käufer-Name, kurz, eindeutig")


class _AxisMap(BaseModel):
    axis: str = Field(description="Original-Achsenname exakt wie in der Eingabe")
    axis_german: str = Field(default="", description="sinnvoller deutscher Achsenname (z.B. Farbe, Menge, Größe)")
    values: list[_NamePair] = Field(default_factory=list)


class _VariantMapSchema(BaseModel):
    """Mapping Original-Variantennamen -> deutsche Namen."""

    axes: list[_AxisMap] = Field(default_factory=list)


class _VariantMatch(BaseModel):
    """KI-Zuordnung eBay-Auswahl -> AliExpress-Variante."""

    attr: str = Field(default="", description="EXAKT die 'attr' der best passenden AliExpress-Variante; '' wenn unsicher")
    confidence: float = Field(default=0.0, description="Sicherheit 0.0-1.0")
    reasoning: str = Field(default="", description="kurze Begruendung (1 Satz)")


class _SourceRank(BaseModel):
    aliexpress_id: str = Field(description="die aliexpress_id des Kandidaten aus der Eingabe")
    confidence: float = Field(default=0.0, description="0-1: gleiches Produkt wie das eBay-Listing?")
    reason: str = Field(default="", description="kurze Begruendung (1 Satz)")


class _SourceRankList(BaseModel):
    ranked: list[_SourceRank] = Field(default_factory=list)


class _TrendTerm(BaseModel):
    keyword: str = Field(description="auf AliExpress SUCHBARER deutscher Begriff (konkret, z.B. 'Länder Fan Schal')")
    reason: str = Field(default="", description="warum es gerade trendet (1 Satz)")
    category: str = Field(default="", description="grobe Kategorie, z.B. 'Fan-Artikel', 'Tech-Gadget'")


class _TrendList(BaseModel):
    terms: list[_TrendTerm] = Field(default_factory=list)


class _ListingSchema(BaseModel):
    """Strukturierte Ausgabe fuer generate_listing (structured outputs)."""

    strategic_note: str = Field(default="", description="strategischer Hinweis (Marken/SEO/Widersprueche)")
    title_seo: str = Field(description="SEO-Titel, 72-80 Zeichen, Title Case")
    description_clean: str = Field(description="vollstaendige Beschreibung mit Bloecken + Footer")
    item_specifics: list[_Aspect] = Field(
        default_factory=list,
        description="Liste der eBay-Artikelmerkmale (name/value), z.B. Marke=Markenlos, Metall=Edelstahl, Stil=Anhänger",
    )
    warnings: list[str] = Field(default_factory=list)


class _PriceRec(BaseModel):
    variant_key: str = Field(description="EXAKT der Variantenschluessel/key aus der Eingabe")
    recommended_price_eur: float = Field(description="empfohlener Verkaufspreis in EUR")
    reasoning: str = Field(default="", description="kurze Begruendung (1 Satz)")


class _MarketAnalysis(BaseModel):
    """Strukturierte Ausgabe fuer analyze_market (Preisempfehlung + Push)."""

    price_recs: list[_PriceRec] = Field(default_factory=list)
    push_recommendations: list[str] = Field(
        default_factory=list, description="konkrete Push-/Marketing-Empfehlungen (Stichpunkte)")
    market_note: str = Field(default="", description="kurze Markt-/Wettbewerbseinschaetzung")


class _ImgAssessment(BaseModel):
    index: int = Field(description="0-basierter Index des Bildes, exakt wie in der Eingabe mit 'Bild N:' nummeriert")
    score: float = Field(description="Eignung als eBay-Titelbild: 0 (ungeeignet) bis 10 (perfektes Hauptbild)")
    reason: str = Field(default="", description="kurze Begruendung, 1 Satz, deutsch")


class _AufdruckSchema(BaseModel):
    """Strukturierte Ausgabe fuer lies_aufdruck (Text auf dem Motiv)."""

    text: str = Field(default="", description=(
        "Der Aufdruck WOERTLICH so, wie er auf dem Kleidungsstueck steht - mit "
        "Gross-/Kleinschreibung und Zeilen als Leerzeichen. LEER lassen, wenn kein "
        "Text erkennbar ist oder er nicht sicher lesbar ist. Niemals raten."))
    sicher: bool = Field(default=False, description=(
        "true nur, wenn der Text zweifelsfrei lesbar ist. Bei verschwommenem, "
        "angeschnittenem oder teilweise verdecktem Text: false."))
    sprache: str = Field(default="", description="Sprache des Aufdrucks: de, en oder leer")
    zielgruppe: str = Field(default="", description=(
        "Wen spricht der Spruch an, in wenigen deutschen Stichworten - z.B. "
        "'Vaeter mit Toechtern', 'Bierfreunde', 'Katzenbesitzer'. Leer, wenn "
        "nicht erkennbar."))
    note: str = Field(default="", description="1 Satz auf Deutsch, was zu sehen war")


class _MotivBeschreibungSchema(BaseModel):
    """Ein fremdes Motiv so genau beschreiben, dass man daraus arbeiten kann.

    Der Unterschied zu ``_AufdruckSchema``: das liest den TEXT ab, das hier
    beschreibt das BILD. Fuer eine eigene Ausfuehrung braucht es beides - und
    zwar so konkret, dass ein Bildmodell etwas damit anfangen kann. Jedes Feld
    darf leer bleiben; nichts wird geraten (Eiserne Regel 3).
    """

    motiv: str = Field(default="", description=(
        "WAS zu sehen ist, in 2-4 deutschen Saetzen und so genau wie moeglich: "
        "Hauptfigur oder Gegenstand, Haltung und Blickrichtung, Kleidung oder "
        "Zubehoer, Nebenelemente. Beispiel: 'Eine Katze im Halbprofil, aufrecht "
        "sitzend, mit Sonnenbrille und Cowboyhut; in beiden Vorderpfoten je ein "
        "Revolver, aus deren Laeufen kleine Rauchwoelkchen steigen.'"))
    text_woertlich: str = Field(default="", description=(
        "Der Aufdruck WOERTLICH. Leer, wenn nichts sicher lesbar ist. Nie raten."))
    text_anordnung: str = Field(default="", description=(
        "WIE der Text sitzt: Zeilenzahl, ueber/unter/um das Motiv, gerade oder "
        "im Bogen, welches Wort am groessten."))
    schrift: str = Field(default="", description=(
        "Schriftcharakter: z.B. 'fette serifenlose Versalien, leicht schraeg', "
        "'handgeschriebene Pinselschrift', 'schmale Retro-Serifen mit Kontur'."))
    stil: str = Field(default="", description=(
        "Machart des Bildes: z.B. 'flacher Vektordruck mit harten Kanten', "
        "'Retro-Siebdruck mit Rasterpunkten', 'Strichzeichnung', 'Aquarell', "
        "'Comic mit dicker Outline', 'Vintage 70er mit Sonnenstrahlen'."))
    farben: list[str] = Field(default_factory=list, description=(
        "Die verwendeten Farben mit ihrer Rolle, je ein Eintrag - z.B. "
        "'Cremeweiss (Schrift)', 'Senfgelb (Sonne)', 'Schwarz (Konturen)'."))
    komposition: str = Field(default="", description=(
        "Aufbau und Groessenverhaeltnisse: was steht oben/mittig/unten, wie "
        "gross ist das Motiv gegenueber dem Text, wo liegt der Schwerpunkt."))
    effekte: list[str] = Field(default_factory=list, description=(
        "Besonderheiten der Ausfuehrung: 'weisse Aussenkontur', 'Farbverlauf', "
        "'Halbtonraster', 'Used-Look/abgerieben', 'Glitzer', 'Leopardenmuster', "
        "'Schlagschatten'. Leer, wenn nichts davon zu sehen ist."))
    ware_farbe: str = Field(default="", description=(
        "Farbe des Kleidungsstuecks auf dem Foto - sie sagt, auf welchen "
        "Untergrund das Motiv gerechnet ist."))
    zielgruppe: str = Field(default="", description=(
        "Wen das Motiv anspricht, wenige Stichworte. Leer, wenn nicht erkennbar."))
    thema: str = Field(default="", description=(
        "Das Thema in 1-4 Woertern, OHNE den Wortlaut des Spruchs - z.B. "
        "'Western-Katze', 'Nordsee und Moewen', '60. Geburtstag'."))
    sicher: bool = Field(default=False, description=(
        "true nur, wenn das Motiv gross und scharf genug zu sehen war."))


class _ImgAssessmentList(BaseModel):
    """Strukturierte Ausgabe fuer assess_images (Titelbild-Bewertung)."""

    assessments: list[_ImgAssessment] = Field(default_factory=list)
    best_index: int = Field(default=0, description="Index des am besten als Titelbild geeigneten Bildes")
    note: str = Field(default="", description="1 Satz Gesamteinschaetzung, deutsch")


def _beleg_zahl(v):
    """Betragsfeld robust in eine Zahl bringen.

    Das Modell liefert gelegentlich den ganzen Text statt der Zahl (real:
    ``"1,89€ Mehrwertsteuer inbegriffen"``) oder deutsche Kommazahlen. Hier wird
    die erste Zahl herausgeloest – ohne zu raten: steht keine drin, bleibt es None.
    """
    if v is None or isinstance(v, (int, float)):
        return v
    text = str(v).strip()
    if not text:
        return None
    m = re.search(r"-?\d{1,3}(?:[.\s]\d{3})*(?:[.,]\d+)?|-?\d+(?:[.,]\d+)?", text)
    if not m:
        return None
    zahl = m.group(0).replace(" ", "")
    if "," in zahl:                       # deutsches Format: 1.234,56 -> 1234.56
        zahl = zahl.replace(".", "").replace(",", ".")
    try:
        return float(zahl)
    except ValueError:
        return None


class _ReceiptItem(BaseModel):
    """Eine Position aus dem Block „Artikel detail" / „Item detail"."""

    title: str = Field(description="Artikelname EXAKT wie auf dem Beleg gedruckt (Originalsprache, nicht uebersetzen)")
    properties: str | None = Field(default="", description="Variante/Eigenschaften unter dem Titel, z.B. 'brown,44-45'; sonst leer")
    unit_price: float | None = Field(default=None, description="Stueckpreis der Position als Zahl, z.B. 4.75")
    quantity: int | None = Field(default=None, description="Menge, z.B. bei 'x1' die 1")

    _zahlen = field_validator("unit_price", "quantity", mode="before")(_beleg_zahl)


class _ReceiptFee(BaseModel):
    """Eine Betragszeile im Summenblock (Einfuhrabgaben, Steuer, ...)."""

    label: str = Field(description="Beschriftung EXAKT wie gedruckt, z.B. 'Steuer' oder 'Geschaetzte Einfuhrabgaben'")
    amount: float = Field(description="Betrag als Zahl, MIT Vorzeichen (Abzug negativ, z.B. -1.32)")

    _zahl = field_validator("amount", mode="before")(_beleg_zahl)


class _ReceiptSchema(BaseModel):
    """Strukturierte Ausgabe fuer read_purchase_receipt (AliExpress-Beleg)."""

    order_id: str | None = Field(default=None, description="Bestell-ID / Order ID")
    order_date: str | None = Field(default=None, description="Bestellzeit EXAKT wie gedruckt, z.B. '11. Aug. 2026' oder 'Jun 29, 2026'")
    items: list[_ReceiptItem] = Field(default_factory=list)
    store_name: str | None = Field(default=None, description="Shop-Name unter der Position, z.B. 'Shop1103840307 Store'")
    ship_to: str | None = Field(default=None, description="Empfaenger + Strasse aus 'Liefer adresse'/'Shipping address', ohne Telefonnummer")
    payment_brand: str | None = Field(default=None, description="Kartenmarke/Zahlart wie abgebildet, z.B. 'VISA', 'Mastercard', 'PayPal'")
    paid_amount: float | None = Field(default=None, description="Der rechts im Block 'Zahlungs methode' stehende Betrag, z.B. bei 'EUR 23.59' die 23.59")
    subtotal: float | None = Field(default=None, description="'Gesamtsumme'/'Subtotal' als Zahl")
    discount: float | None = Field(default=None, description="'All discount'/'Alle Rabatt' als positive Zahl; fehlt die Zeile: null")
    shipping: float | None = Field(default=None, description="'Versand gebuehr'/'Shipping fee' als Zahl; fehlt die Zeile: null")
    extra_lines: list[_ReceiptFee] = Field(
        default_factory=list,
        description="JEDE weitere Betragszeile zwischen Versand und 'Insgesamt', in der Reihenfolge des Belegs "
                    "(z.B. 'Geschaetzte Einfuhrabgaben', 'Steuer' – auch mehrfach und auch negativ). Leer, wenn es keine gibt.")
    vat_included: float | None = Field(
        default=None,
        description="Betrag aus dem Hinweis 'X€ Mehrwertsteuer inbegriffen'/'X€ VAT included' UNTER dem Gesamtbetrag. "
                    "Dieser Betrag ist im Total bereits enthalten und wird NICHT addiert; sonst null.")
    total: float | None = Field(default=None, description="'Insgesamt'/'Total' als Zahl")
    currency: str = Field(default="EUR", description="Waehrung, i.d.R. 'EUR'")

    _zahlen = field_validator("subtotal", "discount", "shipping", "vat_included", "total",
                              "paid_amount", mode="before")(_beleg_zahl)


def parse_receipt_json(text: str) -> dict:
    """Antworttext -> geprueftes Beleg-Dict. Fehlerfall: ``{"_fehler": ...}``.

    Das Modell antwortet als JSON (Prompt-Vorgabe); ein evtl. umschliessender
    ```json-Block wird abgeraeumt. Validiert wird gegen ``_ReceiptSchema`` – so ist
    ausgeschlossen, dass ein freier Text als Beleg-Daten durchrutscht.
    """
    roh = (text or "").strip()
    if roh.startswith("```"):
        roh = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", roh).strip()
    start, ende = roh.find("{"), roh.rfind("}")
    if start == -1 or ende <= start:
        return {"_fehler": f"Antwort war kein JSON: {roh[:120]}"}
    try:
        return _ReceiptSchema.model_validate_json(roh[start:ende + 1]).model_dump()
    except Exception as exc:  # noqa: BLE001 – ungueltiges Schema = Beleg ungelesen
        return {"_fehler": f"Beleg-JSON ungueltig: {str(exc)[:200]}"}


_RECEIPT_SYSTEM = """Du liest einen AliExpress-BELEG (Bestelluebersicht als Bild) fuer die Buchhaltung aus.

ABSOLUTE REGELN:
- Gib NUR wieder, was auf dem Bild WIRKLICH steht. Nichts ergaenzen, nichts uebersetzen, nichts umrechnen, nichts schaetzen.
- Steht ein Wert nicht auf dem Beleg, gib null zurueck. Rate NIE einen Betrag.
- Betraege als Zahl mit Punkt als Dezimaltrenner: aus '4,75€' wird 4.75.
- Artikelnamen EXAKT in der Sprache des Belegs uebernehmen (nicht eindeutschen, nicht kuerzen).
- Im Block 'Artikel detail'/'Item detail' steht unter dem Artikelnamen eine graue Zeile mit der
  gewaehlten Variante (z.B. 'brown,44-45', '01', 'Black AI,GERMANY'). Die gehoert in
  'properties' und NICHT in 'title'. Gibt es sie nicht, ist properties "".

Der Summenblock ist NICHT bei jedem Beleg gleich. Uebernimm ihn Zeile fuer Zeile:
- 'Gesamtsumme'/'Subtotal' -> subtotal
- 'Alle Rabatt'/'All discount' -> discount (als POSITIVE Zahl)
- 'Versand gebuehr'/'Shipping fee' -> shipping
- JEDE weitere Betragszeile davor dem Gesamtbetrag (z.B. 'Geschaetzte Einfuhrabgaben', 'Steuer' –
  es koennen MEHRERE sein, auch eine negative) -> extra_lines, in der Reihenfolge des Belegs,
  Betrag mit Vorzeichen. Keine dieser Zeilen weglassen.
- 'Insgesamt'/'Total' -> total
- der Hinweis UNTER dem Gesamtbetrag ('1,89€ Mehrwertsteuer inbegriffen'/'VAT included')
  -> vat_included, und zwar NUR die ZAHL (aus "1,89€ Mehrwertsteuer inbegriffen" wird 1.89),
  nicht der ganze Satz. Er ist im Total schon enthalten und gehoert NICHT in extra_lines.

Der Versand ist NICHT auf jedem Beleg additiv (manche Belege zeigen eine Versandgebuehr,
das 'Insgesamt' entspricht aber schon der 'Gesamtsumme'). Uebernimm die Zeilen deshalb
einfach so, wie sie dastehen - rechne nichts um.

Zusaetzlich: der Betrag im Block 'Zahlungs methode' (z.B. "EUR 23.59") -> paid_amount.

Antworte AUSSCHLIESSLICH mit diesem JSON-Objekt, ohne Erklaerung und ohne Code-Block:
{"order_id": "...", "order_date": "...", "items": [{"title": "...", "properties": "...", "unit_price": 0.00, "quantity": 1}], "store_name": "...", "ship_to": "...", "payment_brand": "...", "paid_amount": 0.00, "subtotal": 0.00, "discount": null, "shipping": 0.00, "extra_lines": [{"label": "Steuer", "amount": 0.00}], "vat_included": null, "total": 0.00, "currency": "EUR"}"""


_AUFDRUCK_SYSTEM = """Du liest AUFDRUCKE von Kleidungsstuecken ab. Du bekommst Produktfotos EINES Artikels.

Deine einzige Aufgabe: Steht auf dem Kleidungsstueck ein Spruch, ein Schriftzug oder ein Wort? Gib es WOERTLICH wieder.

REGELN:
1. NUR ablesen, nicht deuten. Schreib genau die Zeichen, die dastehen - auch wenn der Spruch unlogisch, falsch geschrieben oder englisch ist.
2. Bist du dir nicht sicher (verschwommen, angeschnitten, von Falten verdeckt, zu klein), dann setze sicher=false. Bei GAR KEINEM lesbaren Text lass text komplett LEER.
3. NIEMALS raten, ergaenzen oder vervollstaendigen. Lieber leer als erfunden: ein erfundener Aufdruck landet im Verkaufstitel und fuehrt zur Ruecksendung.
4. Text, der NICHT auf dem Kleidungsstueck steht, gehoert nicht dazu - also keine Wasserzeichen, Shop-Namen, Groessentabellen, Preisschilder, Modellnamen oder Beschriftungen der Fotomontage.
5. Mehrere Zeilen auf dem Aufdruck: mit einem Leerzeichen verbinden.
6. Zielgruppe nur nennen, wenn der Spruch sie klar benennt. "You can't scare me I have two daughters and a wife" -> "Vaeter mit Toechtern und Ehefrau". Ein Totenkopf ohne Text -> leer.

Antworte ausschliesslich ueber das vorgegebene Schema."""


_MOTIV_SYSTEM = """Du beschreibst das MOTIV auf einem bedruckten Kleidungsstueck. Du bekommst Produktfotos EINES Artikels.

Deine Aufgabe: Beschreibe so genau, dass jemand ohne das Foto eine eigene Fassung zeichnen koennte. Ein Satz wie "eine lustige Katze" ist wertlos - "eine Katze im Halbprofil mit Sonnenbrille und Cowboyhut, in jeder Vorderpfote ein Revolver mit Rauchwoelkchen" ist brauchbar.

REGELN:
1. BESCHREIBEN, nicht bewerten. Kein "schoen", "ansprechend", "modern".
2. Nur, was WIRKLICH zu sehen ist. Was du nicht erkennst, laesst du leer - ein erfundenes Detail landet spaeter in einem Bild, das Geld kostet.
3. Das Kleidungsstueck selbst gehoert NICHT zum Motiv. Kragen, Naht, Aermel, Falten, Buegel, Model, Hintergrund des Fotos, Wasserzeichen und Groessentabellen bleiben aussen vor. Beschrieben wird allein das, was aufgedruckt ist.
4. Den Aufdruck-Text WOERTLICH wiedergeben, mit Gross-/Kleinschreibung. Unsicher? Dann leer und sicher=false.
5. Farben mit ihrer Rolle nennen, nicht als blosse Liste: "Senfgelb (Sonnenscheibe)" statt "gelb".
6. Beim Stil konkret werden: Kantenfuehrung, Anzahl der Farbflaechen, Raster, Kontur, Verlauf, Textur.
7. thema in 1-4 Woertern und OHNE den Spruch - das ist die Ueberschrift, nicht der Wortlaut.

Antworte ausschliesslich ueber das vorgegebene Schema, auf Deutsch."""


_IMAGE_ASSESS_SYSTEM = """Du bist Bild-Experte fuer einen deutschen eBay-Dropshipping-Shop. Du bekommst mehrere Produktfotos EINES Artikels, jeweils davor mit "Bild N:" (0-basiert) nummeriert. Bewerte fuer JEDES Bild, wie gut es sich als TITELBILD (Hauptbild im eBay-Suchergebnis) eignet.

Ein starkes Titelbild:
- zeigt das Produkt gross, scharf und mittig, moeglichst formatfuellend;
- hat einen hellen, neutralen, ruhigen Hintergrund;
- zeigt das ganze Produkt (bei Sets die Hauptkomponente), nicht nur ein Detail;
- ist appetitlich/hochwertig und macht neugierig.

Punktabzug (schlechteres Titelbild):
- eingebrannter Text, Preise, Rabatt-/Prozent-Sticker, Werbe-Banner;
- Collagen/mehrere Kacheln in einem Bild, Wasserzeichen, Logos fremder Shops;
- reine Verpackungs-/Karton-Fotos, unscharfe oder dunkle Bilder, starke Randabschnitte.

Vergib je Bild einen score 0-10 (10 = perfektes Titelbild) mit kurzer Begruendung und waehle best_index = Index des besten Titelbildes. Nutze exakt die vorgegebenen Indizes. Antworte NUR ueber das Schema, auf Deutsch."""


class _ReviseSchema(BaseModel):
    """Strukturierte Ausgabe fuer revise_listing (KI-Bearbeitung nach Anweisung)."""

    title_seo: str = Field(description="ueberarbeiteter eBay-Titel, max 80 Zeichen, Title Case")
    description_clean: str = Field(description="ueberarbeitete vollstaendige Beschreibung mit Bloecken + unveraendertem § 19-Footer")
    item_specifics: list[_Aspect] = Field(default_factory=list,
                                          description="ueberarbeitete Artikelmerkmale (name/value)")
    category_hint: str = Field(default="", description="optionaler eBay-Kategorie-Vorschlag (nur Ziffern) oder leer")
    note: str = Field(default="", description="kurze Erklaerung, was geaendert wurde")
    warnings: list[str] = Field(default_factory=list)


_REVISE_SYSTEM = _LISTING_SYSTEM + """

BEARBEITUNGS-MODUS
Du bekommst ein BEREITS BESTEHENDES eBay-Listing (Titel, Beschreibung, Merkmale, Kategorie) und eine ANWEISUNG des Verkaeufers in Freitext. Setze NUR die Anweisung um und lass alles andere moeglichst unveraendert. Beispiele fuer Anweisungen: Titel/Beschreibung anders formulieren, Keywords ergaenzen, Merkmale hinzufuegen/aendern, eine andere Kategorie vorschlagen, einen Markennamen aus dem Titel entfernen (Markenrecht), einen Hinweis aufnehmen. Behalte den Emoji-Blockstil und den unveraenderten § 19-Footer. Wenn die Anweisung eine Kategorie nennt oder impliziert, gib sie als category_hint (nur Ziffern) zurueck, sonst leer. Schreibe in note kurz, was du geaendert hast. Halte dich an alle Regeln oben (kein Herkunftsland China, keine Garantie-Versprechen, VeRO/Markenrisiken kennzeichnen). Aenderst du den Titel, MUSS er weiterhin mit dem PRODUKTTYP (Substantiv) beginnen – beschreibende Woerter (edel, schwarz, japanisch, 925er, Damen) nie an den Anfang."""


_MARKET_SYSTEM = """Du bist ein eBay-Preis-/Marktanalyst fuer einen deutschen Dropshipping-Shop (Kleinunternehmer §19 UStG, keine USt). Du bekommst ein eigenes Produkt mit Varianten (inkl. Einkaufspreis EK und aktuellem Verkaufspreis) und eine Liste aktiver KONKURRENZPREISE auf eBay.de.

Aufgabe:
1) Empfehle je Variante einen wettbewerbsfaehigen Verkaufspreis in EUR. Orientiere dich am Marktniveau (Median mehrerer Konkurrenten, nicht Ausreisser). WICHTIG: Wenn der aktuelle Preis deutlich (>15%) UEBER dem Marktmedian liegt, empfiehl AKTIV eine SENKUNG Richtung Marktniveau – gerade schwach laufende Artikel sind oft schlicht zu teuer. Aber gehe NIE unter den je Variante mitgelieferten `price_floor_eur` (dort bleiben ~20% Marge, alles darunter waere Verlust/zu duenn). Nutze den EXAKTEN variant_key aus der Eingabe.
2) Gib konkrete, umsetzbare Push-Empfehlungen als kurze Stichpunkte (z.B. Preis leicht senken/anheben, Anzeigentarif/Promoted Listings erhoehen, Titel-Keywords, bessere Bilder). Nur sinnvolle, spezifische Hinweise.
3) market_note: 1-2 Saetze Markt-/Wettbewerbseinschaetzung.

Wichtig: Es sind nur VORSCHLAEGE – der Mensch bestaetigt jede Preis-/Tarifaenderung. Erfinde keine Varianten, nutze nur die gegebenen variant_keys. Alle Werte in EUR."""


class RealLLMClient(LLMClient):
    """Echter LLM-Client. Provider via settings.llm_provider ('claude' | 'openai').

    API-Fehler werden auf die Retry-Taxonomie aus app.retry abgebildet, damit
    retry_async() im Service-Layer korrekt zwischen transient/persistent/rate-limit
    unterscheidet.
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        self._anthropic = None  # lazy
        self._openai = None     # lazy

    # --- Client-Factories (lazy, damit das Modul ohne SDKs importierbar bleibt) ---
    def _anthropic_client(self):
        if self._anthropic is None:
            import anthropic

            # Drei Faehigkeiten haengen echt an Anthropic und lassen sich nicht
            # einfach umstellen: research_trends nutzt dessen eigene Web-Suche
            # (web_search_20250305), assess_images und read_purchase_receipt
            # dessen Bildformat. Steht ein anderer Anbieter eingestellt, laufen
            # sie in einen 401 - das ist hinnehmbar, denn jede faengt den Fehler
            # ab und faellt sauber zurueck. Nicht hinnehmbar war, dass im Log nur
            # "401" stand und niemand den Zusammenhang sah.
            anbieter = getattr(self.settings, "llm_provider", "claude")
            if anbieter != "claude":
                logger.warning(
                    "Anthropic-Client trotz LLM_PROVIDER=%s: diese Faehigkeit gibt "
                    "es nur ueber Anthropic und wird mit 401 fehlschlagen "
                    "(betrifft Trend-Recherche, Bildbewertung, Beleg-Lesen).",
                    anbieter)
            self._anthropic = anthropic.AsyncAnthropic(
                api_key=self.settings.llm_api_key or None
            )
        return self._anthropic

    def _openai_client(self):
        if self._openai is None:
            import openai

            self._openai = openai.AsyncOpenAI(api_key=self.settings.llm_api_key or None)
        return self._openai

    async def _lade_bilder(self, urls: list[str]) -> list[tuple[str, str]]:
        """Bilder herunterladen und als (Medientyp, base64) zurueckgeben.

        Ein nicht ladbares Bild wird uebersprungen, nicht zum Fehler gemacht -
        drei von vier Fotos reichen fuer einen Aufdruck allemal.
        """
        import base64

        import httpx

        raus: list[tuple[str, str]] = []
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as http:
            for u in urls:
                try:
                    antwort = await http.get(u, headers={
                        # Ohne User-Agent liefert alicdn teils gar nichts aus.
                        "User-Agent": "Mozilla/5.0 (compatible; PODShop/1.0)"})
                    antwort.raise_for_status()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Bild nicht ladbar", extra={"url": u[:80],
                                                               "error": str(exc)[:100]})
                    continue
                typ = (antwort.headers.get("content-type") or "image/jpeg").split(";")[0]
                if not typ.startswith("image/"):
                    typ = "image/jpeg"
                roh, typ = self._verkleinere(antwort.content, typ)
                raus.append((typ, base64.b64encode(roh).decode("ascii")))
        return raus

    @staticmethod
    def _verkleinere(roh: bytes, typ: str, kante: int = 900) -> tuple[bytes, str]:
        """Produktfotos vor dem Senden verkleinern.

        Zwei Gruende, beide handfest: Ein Produktfoto in voller Aufloesung
        sprengt als base64 die Anfrage - am 03.09.2026 antwortete OpenAI auf 16
        von 21 Aufrufen mit 429 "Rate limit reached". Und jedes Pixel kostet
        Geld, denn Bildgroesse geht in die Abrechnung ein.

        900 Pixel Kantenlaenge reichen zum ABLESEN eines Aufdrucks bequem - der
        steht formatfuellend auf der Brust, nicht im Kleingedruckten. Faellt die
        Verkleinerung aus, geht das Original raus statt gar nichts.
        """
        try:
            import io

            from PIL import Image

            with Image.open(io.BytesIO(roh)) as bild:
                if max(bild.size) <= kante:
                    return roh, typ
                bild = bild.convert("RGB")
                bild.thumbnail((kante, kante), Image.LANCZOS)
                puffer = io.BytesIO()
                bild.save(puffer, format="JPEG", quality=85, optimize=True)
                return puffer.getvalue(), "image/jpeg"
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bild nicht verkleinerbar", extra={"error": str(exc)[:100]})
            return roh, typ

    async def _parse_bilder(self, *, system: str, text: str, bild_urls: list[str],
                            schema, max_tokens: int = 900):
        """Ein Schema-Aufruf MIT BILDERN ueber den eingestellten Anbieter.

        Dasselbe Problem wie bei ``_parse_structured``, nur fuer Vision: Die
        Bildfaehigkeiten waren fest an Anthropic gebunden. Hier laeuft OpenAI,
        und der OpenAI-Schluessel an der Anthropic-Schnittstelle ergibt 401 -
        "API key is invalid" (nachgewiesen 03.09.2026 beim ersten Lauf der
        Aufdruck-Erkennung). ``gpt-4o-mini`` liest Bilder genauso.

        Die beiden Anbieter erwarten die Bilder unterschiedlich:
        Anthropic als ``{"type": "image", "source": {...}}``, OpenAI als
        ``{"type": "image_url", "image_url": {"url": ...}}``.
        """
        # Bilder SELBST holen statt den Anbieter danach schicken. AliExpress
        # liefert an fremde Abrufer nicht zuverlaessig aus - OpenAI antwortete
        # am 03.09.2026 auf 21 von 25 Bildern mit "Unable to download content
        # from the provided URL". Mit den Bytes im Aufruf faellt diese ganze
        # Fehlerquelle weg, und es funktioniert bei beiden Anbietern gleich.
        geladen = await self._lade_bilder(bild_urls)
        if not geladen:
            raise RuntimeError("Kein Bild ladbar")

        if getattr(self.settings, "llm_provider", "claude") == "openai":
            inhalt: list = [{"type": "text", "text": text}]
            for typ, roh in geladen:
                inhalt.append({"type": "image_url", "image_url": {
                    "url": f"data:{typ};base64,{roh}"}})
            client = self._openai_client()
            resp = await client.beta.chat.completions.parse(
                model=self.settings.llm_model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": inhalt}],
                response_format=schema,
            )
            return resp.choices[0].message.parsed

        inhalt = [{"type": "text", "text": text}]
        for typ, roh in geladen:
            inhalt.append({"type": "image", "source": {
                "type": "base64", "media_type": typ, "data": roh}})
        client = self._anthropic_client()
        resp = await client.messages.parse(
            model=self.settings.llm_model, max_tokens=max_tokens,
            system=system, messages=[{"role": "user", "content": inhalt}],
            output_format=schema,
        )
        return resp.parsed_output

    async def _parse_structured(self, *, system: str, user: str, schema,
                                max_tokens: int = 1500):
        """Ein Schema-Aufruf ueber den EINGESTELLTEN Anbieter statt fest ueber Anthropic.

        Von vierzehn Aufrufen fragten sieben nie nach ``llm_provider`` und bauten
        immer den Anthropic-Client. Im Original faellt das nicht auf, weil dort
        LLM_PROVIDER=claude steht - hier laeuft OpenAI, und der OpenAI-Schluessel
        an der Anthropic-Schnittstelle ergibt 401. Alle sieben Faehigkeiten fielen
        deshalb still in ihren Notbehelf zurueck.

        Aufgefallen beim Shop-Import am 27.08.2026: zehnmal "normalize_variants
        failed", waehrend die Entwuerfe sonst normal entstanden. Die Varianten
        blieben unuebersetzt ("black" statt "Schwarz").

        Gibt das geparste Schema-Objekt zurueck oder ``None``.
        """
        # getattr, weil Test-Attrappen der Settings das Feld teils nicht haben.
        # Voreinstellung claude = das Verhalten von vorher.
        if getattr(self.settings, "llm_provider", "claude") == "openai":
            client = self._openai_client()
            resp = await client.beta.chat.completions.parse(
                model=self.settings.llm_model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                response_format=schema,
            )
            return resp.choices[0].message.parsed
        client = self._anthropic_client()
        resp = await client.messages.parse(
            model=self.settings.llm_model, max_tokens=max_tokens,
            system=system, messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
        return resp.parsed_output

    async def _expand_title_if_short(self, title: str, *, keywords_line: str = "") -> str:
        """Faellt der Titel unter title_target_min_chars, laesst der CODE ihn EINMAL gezielt
        auffuellen (LLMs zaehlen Zeichen – v.a. mit Umlauten – unzuverlaessig und bleiben
        oft unter 80). Nur uebernehmen, wenn wirklich laenger. Best-effort (kein harter Fehler).
        """
        if not title or len(title) >= self.settings.title_target_min_chars:
            return title
        system = ("Du bist ein eBay-SEO-Tool. Gib AUSSCHLIESSLICH einen einzigen Titel aus – "
                  "eine Zeile, deutsch, keine Anfuehrungszeichen, keine Erklaerung, kein "
                  "Satzzeichen am Ende, keine Werbe-Floskeln.")
        prompt = (f"Dieser eBay-Titel hat nur {len(title)} von 80 moeglichen Zeichen – zu kurz. "
                  f"Erweitere ihn auf 78-80 Zeichen (moeglichst nah an 80) mit weiteren ECHTEN "
                  f"suchrelevanten Keywords/Synonymen/Eigenschaften (NICHT kuerzen, keine Floskeln, "
                  f"KEINE unbekannten No-Name-Marken). Das ERSTE Wort bleibt der Produkttyp – "
                  f"haenge Eigenschaften hinten an, setze nie ein Adjektiv davor. Nur der Titel.\n\n"
                  f"Titel: {title}{keywords_line}")
        try:
            if self.settings.llm_provider == "openai":
                client = self._openai_client()
                resp = await client.chat.completions.create(
                    model=self.settings.llm_model,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": prompt}])
                cand = resp.choices[0].message.content or ""
            else:
                client = self._anthropic_client()
                resp = await client.messages.create(
                    model=self.settings.llm_model, max_tokens=64, system=system,
                    messages=[{"role": "user", "content": prompt}])
                cand = "".join(b.text for b in resp.content if b.type == "text")
            cand = self._enforce_title(cand, [], warn_below=0)
            if cand and len(cand) > len(title):
                return cand
        except Exception:  # noqa: BLE001 – Auffuellen ist best-effort
            pass
        return title

    async def _fix_title_word_order(self, title: str) -> str:
        """Beginnt der Titel mit einem BESCHREIBENDEN Wort statt dem Produkt, laesst der
        CODE ihn EINMAL umsortieren (Prompt-Regeln wirken gut, aber nie zu 100 %).

        Uebernommen wird nur, wenn der neue Titel (a) mit dem Produkt beginnt, (b) KEIN
        Maß verfaelscht (Eiserne Regel 9) und (c) nicht wesentlich kuerzer wird – sonst
        bleibt das Original stehen. Best-effort, kein harter Fehler.
        """
        bad = leading_descriptor(title)
        if not bad:
            return title
        system = ("Du bist ein eBay-SEO-Tool. Gib AUSSCHLIESSLICH einen einzigen Titel aus – "
                  "eine Zeile, deutsch, keine Anfuehrungszeichen, keine Erklaerung, kein "
                  "Satzzeichen am Ende.")
        prompt = (f'Dieser eBay-Titel beginnt mit dem beschreibenden Wort "{bad}". Sortiere ihn '
                  f'so um, dass er mit dem PRODUKTTYP (Substantiv) beginnt – dem Wort, das '
                  f'Kaeufer in die eBay-Suche eintippen. Eigenschaften (Farbe, Material, Stil, '
                  f'Zielgruppe, Groesse) kommen DANACH. Beispiel: "Edle Schwarze Vase Deko" -> '
                  f'"Vase Deko Edel Schwarz". Behalte ALLE Keywords und ALLE Maße/Zahlen '
                  f'unveraendert – aendere nur die Reihenfolge (Adjektive dabei in die '
                  f'Grundform). Nur der Titel.\n\nTitel: {title}')
        try:
            if self.settings.llm_provider == "openai":
                client = self._openai_client()
                resp = await client.chat.completions.create(
                    model=self.settings.llm_model,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": prompt}])
                cand = resp.choices[0].message.content or ""
            else:
                client = self._anthropic_client()
                resp = await client.messages.create(
                    model=self.settings.llm_model, max_tokens=64, system=system,
                    messages=[{"role": "user", "content": prompt}])
                cand = "".join(b.text for b in resp.content if b.type == "text")
            cand = self._enforce_title(cand, [], warn_below=0)
            if (cand and leading_descriptor(cand) is None
                    and preserves_measurements(title, cand)
                    and len(cand) >= len(title) - 8):
                return cand
            logger.info("Titel-Umsortierung verworfen", extra={"alt": title, "neu": cand})
        except Exception:  # noqa: BLE001 – Umsortieren ist best-effort
            pass
        return title

    async def suggest_bewirtung_anlaesse(self, *, ort: str = "",
                                         art: str = "", hinweis: str = "",
                                         vermeiden: list[str] | None = None,
                                         zuletzt_benutzt: list[str] | None = None) -> dict:
        """Siehe LLMClient.suggest_bewirtung_anlaesse – hier mit echtem Modell.

        Drei Vorschlaege statt einer Vorgabe: der Nutzer waehlt bewusst den aus, der zum
        tatsaechlichen Treffen passt. Der Prompt verbietet Detail-Erfindungen (Beträge,
        Vertraege, Termine), sperrt `vermeiden` hart und bittet bei `zuletzt_benutzt` nur
        um Ausgewogenheit – Wiederholung ist erlaubt, Haeufung nicht.
        """
        def _liste(werte):
            return [" ".join(str(v).split()) for v in (werte or []) if str(v or "").strip()]

        alt = _liste(vermeiden)[:15]
        weich = [v for v in _liste(zuletzt_benutzt)[:15]
                 if _norm_anlass(v) not in {_norm_anlass(a) for a in alt}]
        system = (
            "Du lieferst VORSCHLAEGE fuer den Anlass eines deutschen Bewirtungsbelegs "
            "(§ 4 Abs. 5 EStG). Der Nutzer betreibt einen eBay-Shop (Dropshipping ueber "
            "AliExpress, Schwerpunkt Auto-Detailing-Sets und Edelstahlschmuck, "
            "Kleinunternehmer § 19 UStG). "
            "Gib GENAU DREI Vorschlaege aus, einen pro Zeile, deutsch, ohne Nummerierung, "
            "ohne Aufzaehlungszeichen, ohne Anfuehrungszeichen, ohne Erklaerung. "
            "Jeder Vorschlag nennt ein KONKRETES geschaeftliches THEMA. Pauschale Angaben "
            "wie 'Geschaeftsessen', 'Arbeitsessen', "
            "'Kundenpflege' oder blosses 'Besprechung' erkennt das Finanzamt NICHT an. "
            + (
                # Stichwort gesetzt -> es ist BINDEND. Ohne diese Regel lieferte das Modell
                # zu 'Einkaufspreise und Verhandlungen' Vorschlaege ueber Versand oder
                # Sortiment (Nutzer-Meldung 03.08.).
                "Der Nutzer hat ein STICHWORT vorgegeben. Alle drei Vorschlaege muessen "
                "GENAU DIESES Thema treffen – variiere nur den Blickwinkel und die "
                "Formulierung, NIEMALS das Thema. Ein Vorschlag, der das Stichwort "
                "inhaltlich nicht aufgreift, ist falsch. Die Woerter des Stichworts duerfen "
                "und sollen in den Vorschlaegen vorkommen. "
                if str(hinweis or "").strip() else
                "Die drei Vorschlaege muessen sich inhaltlich klar unterscheiden (drei "
                "verschiedene geschaeftliche Themen), nicht nur in der Wortwahl. "
            )
            + "WICHTIG: Es sind Formulierungshilfen, keine Behauptungen – erfinde deshalb "
            "KEINE pruefbaren Details (keine Betraege, Prozentsaetze, Vertragsnummern, "
            "Datumsangaben, erfundene Firmennamen). "
            # Der Nutzer schreibt den Anlass VON HAND in ein kleines Feld auf der
            # Restaurant-Quittung – lange Saetze passen dort nicht hinein. WER dabei war,
            # steht dort ohnehin schon in einem eigenen Feld.
            "NUR DAS THEMA, KEINE NAMEN: nenne keine Personen, keine Firmen, kein "
            "'Gespraechspartner', kein 'mit Herrn X' – wer bewirtet wurde, traegt der "
            "Nutzer an anderer Stelle ein. "
            "KURZ FASSEN: ein knappes Substantiv-Stichwort von hoechstens 45 Zeichen, "
            "OHNE Verb-Vorspann (also 'Einkaufskonditionen und Staffelpreise', NICHT "
            "'Besprechung der Einkaufskonditionen und Staffelpreise'). "
            "Kein Satzzeichen am Ende."
        )
        stichwort = " ".join(str(hinweis or "").split())
        prompt = ""
        if stichwort:
            prompt += f"STICHWORT (bindend, alle drei Vorschlaege drehen sich darum): {stichwort}\n"
        prompt += (f"Ort: {ort or '(nicht angegeben)'}\n"
                   f"Art der Bewirtung: {art or '(nicht angegeben)'}\n")
        if not stichwort:
            prompt += "Stichwort des Nutzers: (keines – frei vorschlagen)\n"
        if alt:
            # Mit Stichwort darf die Sperrliste das Thema NICHT verdraengen – sonst kaempfen
            # zwei Anweisungen gegeneinander und das Stichwort verliert.
            prompt += ("\n" + ("Diese Formulierungen wurden schon gezeigt – formuliere zum "
                               "SELBEN Thema anders (anderer Blickwinkel, andere Worte):\n- "
                               if stichwort else
                               "Diese Anlaesse wurden gerade eben gezeigt oder zuletzt direkt "
                               "hintereinander benutzt – schlage jetzt inhaltlich ANDERE vor:\n- ")
                       + "\n- ".join(alt) + "\n")
        # Die Ausgewogenheits-Liste entfaellt bei gesetztem Stichwort: sie zieht das Modell
        # thematisch weg, und der Nutzer hat das Thema gerade selbst vorgegeben.
        if weich and not stichwort:
            prompt += ("\nDiese Anlaesse kamen frueher schon vor. Sie DUERFEN wiederkommen "
                       "(dasselbe Thema kann sich wiederholen) – achte nur darauf, dass die "
                       "Auswahl ueber die Zeit ausgewogen bleibt und sich kein Thema haeuft:\n- "
                       + "\n- ".join(weich) + "\n")
        if stichwort:
            prompt += (f"\nDrei kurze Anlaesse zum Stichwort \"{stichwort}\" "
                       f"(je hoechstens 45 Zeichen, ohne Verb-Vorspann):")
        else:
            prompt += "\nDrei Vorschlaege:"
        try:
            if self.settings.llm_provider == "openai":
                client = self._openai_client()
                resp = await client.chat.completions.create(
                    model=self.settings.llm_model,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": prompt}])
                raw = resp.choices[0].message.content or ""
            else:
                client = self._anthropic_client()
                resp = await client.messages.create(
                    model=self.settings.llm_model, max_tokens=400, system=system,
                    messages=[{"role": "user", "content": prompt}])
                raw = "".join(b.text for b in resp.content if b.type == "text")
        except Exception as exc:  # noqa: BLE001 – Formulierhilfe darf nichts blockieren
            logger.warning("Bewirtungs-Vorschlaege: Modell-Aufruf fehlgeschlagen",
                           extra={"error": str(exc)[:200]})
            return {"vorschlaege": [],
                    "hinweis": "KI nicht erreichbar – bitte den Anlass selbst eintragen."}
        gesperrt = {_norm_anlass(v) for v in alt}
        out: list[str] = []
        for line in (raw or "").splitlines():
            # Modell-Zeilen von Nummerierung/Bullets/Anfuehrungszeichen befreien
            cand = " ".join(re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).split()).strip('"„“')
            # " · " trennt im gespeicherten Beleg die Pflichtangaben-Felder – im Anlass
            # selbst darf es deshalb nicht vorkommen (sonst zerreisst es den Datensatz).
            cand = cand.replace("·", "–").rstrip(".")
            key = _norm_anlass(cand)
            # Zu lang -> ganz verwerfen. Ein gekuerzter Vorschlag waere ein halber Satz,
            # und den kann der Nutzer weder lesen noch uebernehmen.
            if len(cand) < 15 or len(cand) > _ANLASS_MAX_ZEICHEN or key in gesperrt:
                continue
            gesperrt.add(key)
            out.append(cand)
        if not out:
            return {"vorschlaege": [],
                    "hinweis": "Kein brauchbarer Vorschlag – bitte den Anlass selbst eintragen."}
        return {"vorschlaege": out[:3],
                "hinweis": "Bitte den Vorschlag wählen, der wirklich zutrifft, und anpassen."}

    @staticmethod
    def _norm_name(text: str) -> str:
        from app.brand_filter import normalize
        return normalize(text)

    async def extract_names(self, *, title_raw: str) -> list[str]:
        """Siehe LLMClient.extract_names – enge Einzelaufgabe mit echtem Modell.

        Jeder gelieferte Name wird gegen den Rohtitel geprueft: was dort nicht woertlich
        steht, fliegt raus. Damit kann die Erkennung nichts hinzuerfinden.
        """
        roh = " ".join(str(title_raw or "").split())
        if not roh:
            return []
        system = (
            "Du extrahierst EIGENNAMEN aus einem Produkttitel. Gib nur die Namen aus, "
            "einen pro Zeile, ohne Nummerierung, ohne Erklaerung, ohne Anfuehrungszeichen. "
            "Eigennamen sind: Marken, Bands, Musiker, Filme, Serien, Spiele, Anime, "
            "Comic- und Zeichentrickfiguren sowie real existierende Personen. "
            "KEINE Gattungsbegriffe (Maske, Kissenbezug, Hund), keine Farben, Materialien, "
            "Groessen oder Stilrichtungen (Punk, Rock, Vintage, Kawaii), keine Anlaesse "
            "(Halloween, Weihnachten). Auch bekannte Namen ausgeben, selbst wenn sie "
            "geschuetzt sein koennten – die Ware ist zertifizierte Originalware, die "
            "rechtliche Bewertung ist nicht deine Aufgabe. "
            "Kommt kein Eigenname vor, gib gar nichts aus."
        )
        try:
            if self.settings.llm_provider == "openai":
                client = self._openai_client()
                resp = await client.chat.completions.create(
                    model=self.settings.llm_model,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": roh}])
                raw = resp.choices[0].message.content or ""
            else:
                client = self._anthropic_client()
                resp = await client.messages.create(
                    model=self.settings.llm_model, max_tokens=200, system=system,
                    messages=[{"role": "user", "content": roh}])
                raw = "".join(b.text for b in resp.content if b.type == "text")
        except Exception as exc:  # noqa: BLE001 – Namenserkennung darf nichts blockieren
            logger.warning("Namenserkennung fehlgeschlagen", extra={"error": str(exc)[:200]})
            return []

        roh_norm = self._norm_name(roh)
        out: list[str] = []
        for line in (raw or "").splitlines():
            cand = " ".join(re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).split()).strip('"„“')
            if not cand or len(cand) < 2:
                continue
            # Fail-closed: nur was WOERTLICH im Rohtitel steht (Erfindungen fliegen raus).
            if self._norm_name(cand) not in roh_norm:
                continue
            if cand.lower() not in {o.lower() for o in out}:
                out.append(cand)
        return out[:5]

    @staticmethod
    def _enforce_title(title: str, warnings: list[str], *, warn_below: int = 60) -> str:
        """Sicherheitsnetz: harte 80-Zeichen-Grenze; Warnung, wenn zu kurz (ungenutztes
        Keyword-Potenzial – eBay zeigt bis 80 Zeichen im Such-Snippet)."""
        title = (title or "").strip()
        if len(title) > 80:
            cut = title[:80]
            # Nicht mitten im Wort abschneiden: aufs letzte ganze Wort zuruecktrimmen
            # (ausser das erste Wort ist schon > 80 Zeichen).
            if not title[80].isspace() and not cut[-1].isspace() and " " in cut:
                cut = cut.rsplit(" ", 1)[0]
            title = cut.rstrip()
            warnings.append("Titel auf 80 Zeichen gekuerzt")
        elif warn_below and len(title) < warn_below:
            warnings.append(f"Titel nur {len(title)}/80 Zeichen – Keyword-Potenzial ungenutzt")
        return title

    # Werbe-Floskeln, die NICHT als "Keyword" aus Konkurrenztiteln uebernommen werden
    # sollen, plus haeufige deutsche Stoppwoerter.
    _TITLE_STOP = {
        "und", "mit", "fuer", "für", "der", "die", "das", "ein", "eine", "aus", "von",
        "den", "dem", "im", "in", "zu", "auf", "set", "stk", "stück", "stueck", "neu",
        "premium", "top", "qualität", "qualitaet", "blitzversand", "original", "hochwertig",
        "angebot", "sale", "xxl", "cm", "mm",
    }

    @classmethod
    def _mine_keywords(cls, titles, *, top: int = 6) -> list[str]:
        """Haeufigste echte Keywords aus Konkurrenztiteln (>=2x, keine Floskeln/Stoppwoerter)."""
        from collections import Counter
        counter: Counter = Counter()
        for t in titles or []:
            for w in re.findall(r"[A-Za-zÄÖÜäöüß0-9]{3,}", (t or "").lower()):
                if w in cls._TITLE_STOP:
                    continue
                counter[w] += 1
        return [w for w, n in counter.most_common(top) if n >= 2]

    # --- Fehler-Mapping ---
    def _translate(self, exc: Exception) -> Exception:
        """Mappt SDK-Fehler auf TransientError/PersistentError/RateLimitError."""
        try:
            import anthropic
        except Exception:  # pragma: no cover
            anthropic = None  # type: ignore

        if anthropic is not None:
            if isinstance(exc, anthropic.RateLimitError):
                retry_after = 5.0
                try:
                    retry_after = float(exc.response.headers.get("retry-after", 5))
                except Exception:  # pragma: no cover
                    pass
                return RateLimitError(str(exc), retry_after=retry_after)
            if isinstance(exc, (anthropic.APIConnectionError, anthropic.InternalServerError)):
                return TransientError(str(exc))
            if isinstance(exc, (anthropic.BadRequestError, anthropic.AuthenticationError,
                                anthropic.PermissionDeniedError, anthropic.NotFoundError)):
                return PersistentError(str(exc))
            if isinstance(exc, anthropic.APIStatusError):
                return (TransientError if exc.status_code >= 500 else PersistentError)(str(exc))
        return TransientError(str(exc))  # unbekannt -> vorsichtig wiederholen

    # --- API ---
    @staticmethod
    def _format_specs(specs: list | None) -> str:
        # size_info wird gesondert aufbereitet (siehe groessentabelle) und darf hier
        # NICHT noch einmal als JSON-Brocken auftauchen - sonst deutet die KI doch
        # wieder selbst daran herum.
        rows = [f"- {s.get('name')}: {s.get('value')}" for s in (specs or [])
                if s.get("name") and s.get("value")
                and str(s.get("name")).strip().lower() != "size_info"]
        tabelle = groessentabelle(specs)
        if tabelle:
            rows.append("- " + tabelle)
        return "\n".join(rows) if rows else "(keine strukturierten Specs)"

    @staticmethod
    def _format_axes(axes: dict | None) -> str:
        if not axes:
            return "(keine Varianten)"
        return "\n".join(f"- {name}: {', '.join(str(v) for v in vals)}"
                         for name, vals in axes.items())

    async def generate_listing(self, *, title_raw, description_raw, category_guess,
                               specs=None, variant_axes=None, pflicht_namen=None,
                               vergebene_anfaenge=None):
        namen = [str(n).strip() for n in (pflicht_namen or []) if str(n or "").strip()]
        user = (
            f"Kategorie-Hinweis: {category_guess or 'unbekannt'}\n\n"
            f"AliExpress-Titel:\n{title_raw}\n\n"
            f"AliExpress-Beschreibung (roh):\n{description_raw}\n\n"
            f"STRUKTURIERTE PRODUKT-SPECS (fuer 'Maße & Details' + item_specifics nutzen):\n"
            f"{self._format_specs(specs)}\n\n"
            f"VERFUEGBARE VARIANTEN (Achsen + Optionen – im Varianten-Block nennen):\n"
            f"{self._format_axes(variant_axes)}"
        )
        if namen:
            # Vorab erkannte Eigennamen hart einfordern: die grosse Regel allein reichte
            # nicht (Boehse Onkelz wurde trotzdem zu 'Punk Rock' verallgemeinert).
            user += ("\n\nPFLICHT-NAMEN – diese Eigennamen stehen im Original und MUESSEN "
                     "unveraendert im Titel vorkommen (und passend in der Beschreibung). "
                     "Nicht weglassen, nicht durch einen Gattungsbegriff ersetzen, nicht "
                     "umschreiben:\n- " + "\n- ".join(namen))
        # Ohne diese Liste kann die Variationsregel im System-Prompt gar nicht greifen:
        # jedes Produkt wird in einem EIGENEN Aufruf erzeugt, der die anderen nicht
        # kennt - jeder haelt sich fuer den ersten. Beim Shop-Import am 27.08.2026
        # fingen deshalb alle zehn Titel mit "T-Shirt" an, obwohl die Regel seit
        # jeher im Prompt steht.
        anfaenge = [str(a).strip() for a in (vergebene_anfaenge or []) if str(a or "").strip()]
        if anfaenge:
            user += ("\n\nBEREITS VERGEBENE TITELANFAENGE im Shop – waehle fuer DIESES "
                     "Produkt ein ANDERES Einstiegswort, aber weiterhin ein Substantiv, "
                     "nach dem Kaeufer suchen (Synonym, Zusammensetzung oder Zielgruppen-"
                     "Variante wie Herrenshirt, Damenshirt, Motiv-Shirt, Sprueche-Shirt). "
                     "NIE ein beschreibendes Wort voranstellen, nur um anders anzufangen:"
                     "\n- " + "\n- ".join(anfaenge))
        try:
            if self.settings.llm_provider == "openai":
                gen = await self._openai_generate(user)
            else:
                gen = await self._claude_generate(user)
        except Exception as exc:  # noqa: BLE001 – uebersetzen, dann erneut werfen
            raise self._translate(exc) from exc

        warnings = list(gen.warnings)
        title = korrigiere_schreibweise(gen.title_seo)     # "Overgroessen" -> "Oversize"
        title = self._enforce_title(title, warnings)
        title = await self._expand_title_if_short(title)   # 80-Zeichen ausschoepfen (Draft)
        title = await self._fix_title_word_order(title)    # Produkt nach vorn (SEO)
        if category_guess is None and "category" not in " ".join(warnings).lower():
            warnings.append("category might be wrong")
        specifics = {a.name.strip(): a.value.strip() for a in (gen.item_specifics or [])
                     if a.name and a.value and a.value.strip().lower() not in ("", "keine", "n/a")}
        # HARTER MASS-SCHUTZ: verliert ein item_specific ein Maß aus dem Roh-Spec (55x90cm -> 90cm),
        # den Original-Wert wiederherstellen – die KI darf Maße NIE kürzen/verfälschen.
        specifics = repair_spec_measurements(specifics, specs)
        description = strip_forbidden_blocks(gen.description_clean.strip(), warnings)
        # MASS-SCHUTZ auch fuer die Beschreibung: die Groessentabelle kommt aus der
        # Quelle, nicht aus der Deutung des Modells (siehe erzwinge_groessentabelle).
        description = erzwinge_groessentabelle(description, specs, warnings)
        # Hier stand bis 08.09.2026 der Edelmetall-Filter: er ersetzte falsche
        # 925-/Sterling-/Echtsilber-Behauptungen, weil die Handelsware Edelstahl war,
        # hoechstens silberfarben. Medienwerk bedruckt Textil mit eigenen Motiven -
        # es gibt keinen Lieferantentext mehr, der Silber behauptet, und damit auch
        # nichts zu entschaerfen. Der Filter ist mit dem Handelsteil ausgezogen.
        return GeneratedListing(
            title_seo=title, description_clean=description,
            warnings=warnings, strategic_note=(gen.strategic_note or "").strip(),
            item_specifics=specifics,
        )

    async def normalize_variants(self, axes: dict, product_title: str = "") -> dict:
        """Varianten-Achsen/-Werte in sinnvolle DEUTSCHE Käufer-Namen übersetzen.

        Rückgabe: {"axes": {alt: neu}, "values": {achse_alt: {wert_alt: wert_neu}}}.
        Bei Fehlern: {} (Original bleibt).
        """
        if not axes:
            return {}
        lines = [f"Achse: {a} | Werte: {', '.join(str(v) for v in vals)}"
                 for a, vals in axes.items()]
        user = (
            "Produkt: " + (product_title or "?")[:120] + "\n"
            "AliExpress-Varianten (oft kryptisch/englisch/falsch übersetzt):\n"
            + "\n".join(lines) + "\n\n"
            "Gib für JEDE Achse einen sinnvollen deutschen Achsennamen und für JEDEN Wert "
            "einen kurzen, kaufentscheidungs-tauglichen deutschen Namen. Regeln:\n"
            "- Farben -> deutsche Farbnamen (WEISS -> Weiß, Black -> Schwarz).\n"
            "- Mengen/Sets (1PCS, 5PCS, Package List) -> '1 Stück', '5 Stück', 'Set (…)'. "
            "Achse dann z.B. 'Menge' oder 'Set' nennen (NICHT 'Farbe', wenn es keine Farben sind!).\n"
            "- 'Style 1/2' o.ä.: nur umbenennen, wenn aus dem Produkt ableitbar; sonst Original behalten.\n"
            "- KI/AI-Zusätze beibehalten (z.B. 'Schwarz (KI-Version)').\n"
            "- MASSE/GRÖSSEN/MENGEN NIEMALS ändern! Zahlen und Einheiten (z.B. '55x90cm', "
            "'250 ml', '5 Stück') EXAKT wie im Original übernehmen – nur Wörter drumherum "
            "übersetzen ('Unframed'->'ohne Rahmen'). NIE eine Kante verdoppeln oder quadratisch "
            "machen (55x90 bleibt 55x90, wird NIE 90x90).\n"
            "- Werte je Achse muessen EINDEUTIG bleiben (keine zwei gleichen neuen Namen), max 50 Zeichen."
        )
        try:
            parsed = await self._parse_structured(
                system="Du benennst eBay-Variantennamen für deutsche Käufer. Antworte nur über das Schema.",
                user=user, schema=_VariantMapSchema, max_tokens=1500)
            if parsed is None:
                return {}
        except Exception as exc:  # noqa: BLE001 – Namens-Politur darf nie blockieren
            logger.warning("normalize_variants failed", extra={"error": str(exc)})
            return {}
        out: dict = {"axes": {}, "values": {}}
        for am in parsed.axes:
            if not am.axis:
                continue
            if am.axis_german and am.axis_german.strip():
                out["axes"][am.axis] = am.axis_german.strip()[:50]
            vmap = {}
            for p in am.values:
                if p.old and p.new and p.new.strip():
                    new = p.new.strip()[:50]
                    # HARTER SCHUTZ (deterministisch, nicht der KI vertrauen): aendert die
                    # Umbenennung ein Maß/eine Menge -> verwerfen, Original behalten.
                    if not preserves_measurements(str(p.old), new):
                        logger.warning("variant rename dropped (measurement changed)",
                                       extra={"old": str(p.old), "new": new})
                        continue
                    # HARTER SCHUTZ 2 (Vorfall Zirkonia-Kette 09.08.): ist der Originalname
                    # im Kern nur eine NUMMER ("3", "Style 5", "Nr. 12"), gibt es nichts zu
                    # uebersetzen — ein erfundener Klarname ("Gold") ist Halluzination und
                    # erzeugt falsche Kaeufer-Erwartungen (Ruecksendung, falsche Ware).
                    # Erlaubt bleibt nur eine Umbenennung, die die Nummer BEHAELT
                    # ("3" -> "Design 3"); sonst Original behalten.
                    m_num = re.fullmatch(r"(?:style|typ|type|no\.?|nr\.?|#)?\s*(\d+)\s*#?",
                                         str(p.old).strip(), re.I)
                    if m_num and not re.search(
                            rf"(?<!\d){re.escape(m_num.group(1))}(?!\d)", new):
                        logger.warning("variant rename dropped (opaque number label)",
                                       extra={"old": str(p.old), "new": new})
                        continue
                    vmap[str(p.old)] = new
            # Eindeutigkeit: kollidierende neue Namen -> Original behalten
            seen: dict = {}
            for old, new in list(vmap.items()):
                if new in seen:
                    vmap.pop(old)
                else:
                    seen[new] = old
            if vmap:
                out["values"][am.axis] = vmap
        return out

    async def research_trends(self, *, context="", max_terms=12):
        client = self._anthropic_client()
        # Stufe 1: LIVE-Web-Recherche (Anthropic web_search-Tool)
        research = ""
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
        prompt = (
            f"{context}\nDu bist Produkt-Scout für einen deutschen eBay-Dropshipping-Shop "
            "(Bezug: AliExpress). Recherchiere im WEB, welche Produkte AKTUELL stark "
            "nachgefragt sind / im Trend liegen. Nutze MEHRERE Quellen/Blickwinkel:\n"
            "- TEMU: aktuelle Bestseller & 'Temu viral/trending' Produkte (spiegeln fast 1:1 "
            "die güstige AliExpress-Ware -> starkes, direkt umsetzbares Signal).\n"
            "- Virale Produkte auf TikTok/Instagram/Pinterest ('TikTok made me buy it', Reels, "
            "Pins) und in Nischen-Communities (Reddit, Foren).\n"
            "- Saisonale/aktuelle Events (Sport-Events wie WM/EM → Fahnen/Schals/Fan-Artikel, "
            "Jahreszeit, anstehende Feiertage/Anlässe).\n"
            "- Bestseller-Kategorien (Amazon Movers & Shakers, Google Shopping, eBay).\n"
            "Nur Produkte, die als AliExpress-Dropshipping taugen (klein, gut versendbar, "
            "günstig einkaufbar, klare Nachfrage). WICHTIG: MEIDE elektrische/elektronische "
            "und batteriebetriebene Artikel (Powerbanks, Kopfhörer, Kameras, Smart-Gadgets, "
            "LED-Controller o.ä.) – die haben hohe Retouren und in DE Registrierungspflichten "
            "(ElektroG/Batteriegesetz). Bevorzuge langlebige Nicht-Elektronik: Haushalt/Küche, "
            "Auto-Detailing, Schmuck/Identität, Garten/Outdoor, Haustier, Beauty (nicht-elektrisch), "
            "Spielzeug/Sammler, Ordnung/Reise. Fasse die Ergebnisse konkret zusammen.")
        try:
            messages = [{"role": "user", "content": prompt}]
            resp = await client.messages.create(
                model=self.settings.llm_model, max_tokens=2000, tools=tools, messages=messages)
            # pause_turn: die Server-Websuche hat ihr Iterationslimit erreicht und ist
            # noch nicht fertig -> Antwort zurueckspielen und fortsetzen (sonst ist
            # 'research' leer und die BEZAHLTE Suche waere verschwendet).
            cont = 0
            while getattr(resp, "stop_reason", "") == "pause_turn" and cont < 3:
                messages = messages + [{"role": "assistant", "content": resp.content}]
                resp = await client.messages.create(
                    model=self.settings.llm_model, max_tokens=2000, tools=tools, messages=messages)
                cont += 1
            research = " ".join(getattr(b, "text", "") for b in resp.content
                                if getattr(b, "type", "") == "text")
        except Exception as exc:  # noqa: BLE001 – Web-Suche optional -> Fallback LLM-Wissen
            logger.warning("trend web_search nicht verfügbar, Fallback auf Wissen",
                           extra={"error": str(exc)[:150]})

        # Stufe 2: Recherche -> konkrete AliExpress-Suchbegriffe (structured)
        extract = (
            f"{context}\n"
            + (f"WEB-RECHERCHE-ERGEBNISSE:\n{research[:4000]}\n\n"
               if research else "Keine Web-Recherche verfügbar – nutze dein Wissen zu aktuellen Trends/Saison.\n\n")
            + f"Leite daraus {max_terms} konkrete, auf AliExpress SUCHBARE deutsche Keywords ab. "
            "Jedes Keyword muss konkret genug für eine Produktsuche sein (z.B. 'Länder Fan Schal', "
            "'Grill Thermometer Analog' – NICHT 'Elektronik', NICHT 'Gadget'). MEIDE elektrische/"
            "batteriebetriebene Produkte (keine Powerbanks/Kopfhörer/Kameras/Smart-Gadgets/LED-"
            "Controller). Streue über MEHRERE Kategorien (nicht 5x dieselbe). Je Keyword: kurze "
            "Begründung + Kategorie.")
        try:
            r2 = await client.messages.parse(
                model=self.settings.llm_model, max_tokens=1500,
                system="Du wandelst Trend-Recherche in konkrete, suchbare AliExpress-Keywords.",
                messages=[{"role": "user", "content": extract}],
                output_format=_TrendList)
            p = r2.parsed_output
            if p is None:
                return []
            out, seen = [], set()
            for t in p.terms:
                kw = (t.keyword or "").strip()
                if kw and kw.lower() not in seen:
                    seen.add(kw.lower())
                    out.append({"keyword": kw, "reason": (t.reason or "")[:200],
                                "category": (t.category or "")[:60]})
            return out[:max_terms]
        except Exception as exc:  # noqa: BLE001
            logger.warning("trend extract failed", extra={"error": str(exc)})
            return []

    async def rank_source_candidates(self, *, ebay_title, candidates):
        if not ebay_title or not candidates:
            return []
        lines = [f"- id={c.get('aliexpress_id')} | {(c.get('title') or '?')[:100]} | "
                 f"{c.get('price_eur') or '?'} EUR" for c in candidates]
        user = (
            f"eBay-Produkt (Titel): {ebay_title[:180]}\n\n"
            f"AliExpress-Kandidaten aus der Bildsuche:\n" + "\n".join(lines) + "\n\n"
            "Bewerte JEDEN Kandidaten: Ist es DASSELBE Produkt wie das eBay-Produkt "
            "(gleicher Artikel; anderer Titel/Sprache/Marke ist ok)? confidence 0-1.\n"
            "- 0.8-1.0: eindeutig dasselbe Produkt.\n"
            "- 0.4-0.7: aehnlich/plausibel, aber unsicher.\n"
            "- 0.0-0.3: anderes Produkt (nur optisch aehnlich).\n"
            "Wichtig: 'aehnlich sieht aus' ist NICHT 'dasselbe'. Lieber streng bewerten – "
            "eine falsche Quelle bestellt spaeter die falsche Ware. Gib zu JEDER id einen Wert."
        )
        try:
            p = await self._parse_structured(
                system="Du prüfst, ob AliExpress-Produkte mit einem eBay-Produkt identisch sind.",
                user=user, schema=_SourceRankList, max_tokens=1500)
            if p is None:
                return []
            valid = {str(c.get("aliexpress_id")) for c in candidates}
            out = []
            for r in p.ranked:
                if str(r.aliexpress_id) in valid:
                    out.append({"aliexpress_id": str(r.aliexpress_id),
                                "confidence": max(0.0, min(1.0, float(r.confidence or 0.0))),
                                "reason": (r.reason or "")[:160]})
            return out
        except Exception as exc:  # noqa: BLE001 – Ranking darf die Bildsuche nie sprengen
            logger.warning("rank_source_candidates failed", extra={"error": str(exc)})
            return []

    async def match_variant(self, *, ebay_selection, ali_variants, product_title=""):
        if not ebay_selection or not ali_variants:
            return {"attr": "", "confidence": 0.0, "reasoning": "keine Daten"}
        sel = ", ".join(f"{k}: {v}" for k, v in ebay_selection.items())
        lines = [f"- attr={v.get('attr')} | Name: {v.get('name') or '?'} | Preis: {v.get('price') or '?'}"
                 for v in ali_variants]
        user = (
            f"Produkt: {(product_title or '?')[:120]}\n"
            f"Der eBay-Kaeufer hat bestellt: {sel}\n\n"
            f"AliExpress-Varianten (nur aus DIESER Liste waehlen):\n" + "\n".join(lines) + "\n\n"
            "Welche AliExpress-Variante entspricht der eBay-Auswahl? Regeln:\n"
            "- Deutsch=Englisch: Schwarz=Black, Weiss=White, Rot=Red, Blau=Blue, Groesse=Size, Laenge=Length.\n"
            "- KI/AI ist ENTSCHEIDEND: 'Schwarz KI' -> 'Black AI' (NICHT nur 'Black'!). Umgekehrt genauso.\n"
            "- Ausfuehrungen/Sets: 'Nur Brille'='Only Goggles', 'mit Etui'='with Case'.\n"
            "- Zahlen in Klammern wie '(771)' sind interne IDs -> fuers Matching IGNORIEREN.\n"
            "- Heissen ALLE Varianten gleich (z.B. 'as picture') oder passen mehrere gleich gut, "
            "dann attr='' und niedrige Konfidenz -> ein Mensch entscheidet.\n"
            "PRAEZISION vor Vollstaendigkeit: Eine falsche Zuordnung bestellt die falsche Ware "
            "und kostet echtes Geld. Im Zweifel lieber attr='' als raten."
        )
        try:
            p = await self._parse_structured(
                system="Du ordnest eBay-Bestellvarianten exakt den AliExpress-Varianten zu.",
                user=user, schema=_VariantMatch, max_tokens=400)
            if p is None:
                return {"attr": "", "confidence": 0.0, "reasoning": "kein Schema"}
            valid = {str(v.get("attr")) for v in ali_variants}
            attr = p.attr if p.attr in valid else ""   # Halluzination abfangen
            conf = float(p.confidence or 0.0) if attr else 0.0
            return {"attr": attr, "confidence": max(0.0, min(1.0, conf)),
                    "reasoning": (p.reasoning or "")[:200]}
        except Exception as exc:  # noqa: BLE001 – Matching darf den Fulfill-Flow nicht sprengen
            logger.warning("match_variant failed", extra={"error": str(exc)})
            return {"attr": "", "confidence": 0.0, "reasoning": "LLM-Fehler"}

    async def revise_listing(self, *, instruction, current_title, current_description,
                             current_specifics=None, current_category=None):
        specs_txt = "\n".join(f"- {k}: {v}" for k, v in (current_specifics or {}).items()) \
            or "(keine)"
        user = (
            f"BESTEHENDES LISTING\nTitel: {current_title}\n"
            f"Kategorie: {current_category or 'unbekannt'}\n"
            f"Merkmale:\n{specs_txt}\n\n"
            f"Beschreibung:\n{current_description}\n\n"
            f"ANWEISUNG DES VERKAEUFERS:\n{instruction}"
        )
        try:
            if self.settings.llm_provider == "openai":
                client = self._openai_client()
                resp = await client.beta.chat.completions.parse(
                    model=self.settings.llm_model,
                    messages=[{"role": "system", "content": _REVISE_SYSTEM},
                              {"role": "user", "content": user}],
                    response_format=_ReviseSchema)
                p = resp.choices[0].message.parsed
            else:
                client = self._anthropic_client()
                resp = await client.messages.parse(
                    model=self.settings.llm_model, max_tokens=4096,
                    system=_REVISE_SYSTEM,
                    messages=[{"role": "user", "content": user}],
                    output_format=_ReviseSchema)
                p = resp.parsed_output
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        if p is None:
            raise PersistentError("LLM lieferte kein valides Schema zurueck")
        warnings = list(p.warnings or [])
        title = self._enforce_title(p.title_seo or current_title, warnings)
        title = await self._expand_title_if_short(title)   # 80-Zeichen ausschoepfen (Überarbeiten)
        title = await self._fix_title_word_order(title)    # Produkt nach vorn (SEO)
        specifics = {a.name.strip(): a.value.strip() for a in (p.item_specifics or [])
                     if a.name and a.value and a.value.strip().lower() not in ("", "keine", "n/a")}
        desc = strip_forbidden_blocks((p.description_clean or current_description).strip(), warnings)
        specifics = specifics or dict(current_specifics or {})
        # Der Edelmetall-Filter ist mit dem Handelsteil ausgezogen (08.09.2026) -
        # siehe die Begruendung weiter oben in dieser Datei.
        cat = (p.category_hint or "").strip()
        return {"title_seo": title, "description": desc,
                "item_specifics": specifics,
                "category_hint": cat if cat.isdigit() else None,
                "note": (p.note or "").strip(), "warnings": warnings}

    @staticmethod
    def _format_competitor_prices(offers: list[dict] | None) -> str:
        rows = []
        for o in (offers or []):
            if o.get("price_eur") is not None:
                rows.append(f"- {o['price_eur']:.2f} EUR: {(o.get('title') or '')[:70]}")
        return "\n".join(rows) if rows else "(keine Konkurrenzpreise gefunden)"

    async def analyze_market(self, *, product_title, variants, competitor_prices,
                             current_price_eur=None):
        valid_keys = {str(v.get("key")) for v in (variants or [])}
        vtxt = "\n".join(
            f"- key={v.get('key')} | {v.get('name')} | EK {v.get('ek_eur')} EUR | "
            f"aktuell {v.get('current_price_eur')} EUR" for v in (variants or [])) or "(keine)"
        user = (
            f"EIGENES PRODUKT: {product_title}\n"
            f"Aktueller Preis (repraesentativ): {current_price_eur} EUR\n\n"
            f"EIGENE VARIANTEN:\n{vtxt}\n\n"
            f"KONKURRENZPREISE auf eBay (aktive Angebote):\n"
            f"{self._format_competitor_prices(competitor_prices)}")
        try:
            p = await self._parse_structured(
                system=_MARKET_SYSTEM, user=user, schema=_MarketAnalysis,
                max_tokens=1500)
        except Exception as exc:  # noqa: BLE001 – Analyse darf den Flow nie sprengen
            logger.warning("analyze_market failed", extra={"error": str(exc)[:150]})
            return {"price_recommendations": [], "push_recommendations": [], "market_note": ""}
        if p is None:
            return {"price_recommendations": [], "push_recommendations": [], "market_note": ""}
        recs = [{"variant_key": r.variant_key,
                 "recommended_price_eur": round(float(r.recommended_price_eur), 2),
                 "reasoning": (r.reasoning or "")[:160]}
                for r in (p.price_recs or [])
                if str(r.variant_key) in valid_keys and r.recommended_price_eur and
                float(r.recommended_price_eur) > 0]   # Halluzinierte Keys/Preise abfangen
        return {"price_recommendations": recs,
                "push_recommendations": [s for s in (p.push_recommendations or []) if s][:6],
                "market_note": (p.market_note or "")[:300]}

    async def lies_aufdruck(self, *, product_title, image_urls):
        # Zwei Bilder reichen: der Aufdruck steht formatfuellend auf dem
        # Titelbild und meist noch auf einem Detailfoto. Vier waren es zuerst -
        # das kostete doppelt so viel und lief bei OpenAI in die Mengenbremse
        # (429, 03.09.2026), ohne einen einzigen Aufdruck mehr zu finden.
        urls = [u for u in (image_urls or []) if u][:2]
        leer = {"text": "", "sicher": False, "sprache": "", "zielgruppe": "", "note": ""}
        if not urls:
            return leer
        text = (f"Produkt: {(product_title or '?')[:160]}\n"
                f"Es folgen {len(urls)} Fotos desselben Kleidungsstuecks. Lies den "
                "Aufdruck ab, falls einer vorhanden ist.")
        try:
            p = await self._parse_bilder(
                system=_AUFDRUCK_SYSTEM, text=text, bild_urls=urls,
                schema=_AufdruckSchema, max_tokens=600)
        except Exception as exc:  # noqa: BLE001 - eine Bildanalyse darf nie etwas sprengen
            # ``fehler`` ist kein Schoenheitsfeld: ohne ihn sieht ein gescheiterter
            # Aufruf wie "kein Aufdruck gefunden" aus. Der Aufrufer wuerde das als
            # Ergebnis festhalten und den Artikel nie wieder ansehen - das System
            # behauptete dann dauerhaft, ein Motiv-Shirt trage keinen Aufdruck.
            logger.warning("lies_aufdruck failed", extra={"error": str(exc)[:150]})
            return {**leer, "fehler": str(exc)[:200]}
        if p is None:
            return {**leer, "fehler": "Modell lieferte keine auswertbare Antwort"}
        return {"text": (p.text or "").strip()[:200], "sicher": bool(p.sicher),
                "sprache": (p.sprache or "").strip()[:5],
                "zielgruppe": (p.zielgruppe or "").strip()[:120],
                "note": (p.note or "").strip()[:200]}

    async def beschreibe_motiv(self, *, product_title, image_urls):
        # EIN Bild genuegt und ist hier sogar besser: das Titelbild zeigt den
        # Druck formatfuellend. Ein zweites Foto ist meist eine Groessentabelle
        # oder eine andere Farbe - es kostet, verwirrt die Beschreibung und
        # brachte bei der Aufdruck-Erkennung am 03.09.2026 die Mengenbremse.
        urls = [u for u in (image_urls or []) if u][:1]
        leer = {"motiv": "", "text_woertlich": "", "text_anordnung": "",
                "schrift": "", "stil": "", "farben": [], "komposition": "",
                "effekte": [], "ware_farbe": "", "zielgruppe": "", "thema": "",
                "sicher": False}
        if not urls:
            return {**leer, "fehler": "kein Bild vorhanden"}
        text = (f"Produkt: {(product_title or '?')[:200]}\n"
                "Es folgt das Produktfoto. Beschreibe NUR den Aufdruck - nicht "
                "das Kleidungsstueck, nicht den Hintergrund.")
        try:
            p = await self._parse_bilder(
                system=_MOTIV_SYSTEM, text=text, bild_urls=urls,
                schema=_MotivBeschreibungSchema, max_tokens=1200)
        except Exception as exc:  # noqa: BLE001 - eine Bildanalyse sprengt nie etwas
            # Wie bei lies_aufdruck: ohne ``fehler`` saehe ein gescheiterter
            # Aufruf aus wie "auf dem Shirt ist nichts zu sehen" - und genau so
            # wuerde er gespeichert und nie wieder angesehen.
            logger.warning("beschreibe_motiv failed", extra={"error": str(exc)[:150]})
            return {**leer, "fehler": str(exc)[:200]}
        if p is None:
            return {**leer, "fehler": "Modell lieferte keine auswertbare Antwort"}
        return {
            "motiv": (p.motiv or "").strip()[:1500],
            "text_woertlich": (p.text_woertlich or "").strip()[:300],
            "text_anordnung": (p.text_anordnung or "").strip()[:400],
            "schrift": (p.schrift or "").strip()[:300],
            "stil": (p.stil or "").strip()[:400],
            "farben": [str(f).strip()[:80] for f in (p.farben or [])][:12],
            "komposition": (p.komposition or "").strip()[:600],
            "effekte": [str(e).strip()[:80] for e in (p.effekte or [])][:10],
            "ware_farbe": (p.ware_farbe or "").strip()[:60],
            "zielgruppe": (p.zielgruppe or "").strip()[:150],
            "thema": (p.thema or "").strip()[:80],
            "sicher": bool(p.sicher),
        }

    async def assess_images(self, *, product_title, image_urls):
        urls = [u for u in (image_urls or []) if u][:8]   # Deckel: max 8 Bilder pro Analyse
        if not urls:
            return {"assessments": [], "best_index": None, "note": ""}
        # Vision: je Bild ein 'Bild N:'-Label + das Bild selbst (URL-Quelle; eBay-Bilder sind
        # oeffentlich abrufbar). Claude bewertet die Titelbild-Eignung ueber das Schema.
        content: list = [{"type": "text", "text": (
            f"Produkt: {(product_title or '?')[:160]}\n"
            f"Es folgen {len(urls)} Produktbilder desselben Artikels, jeweils mit 'Bild N:' "
            "nummeriert (0-basiert). Bewerte jedes als moegliches eBay-Titelbild und waehle "
            "das beste (best_index).")}]
        for i, u in enumerate(urls):
            content.append({"type": "text", "text": f"Bild {i}:"})
            content.append({"type": "image", "source": {"type": "url", "url": u}})
        try:
            client = self._anthropic_client()
            resp = await client.messages.parse(
                model=self.settings.llm_model, max_tokens=1200,
                system=_IMAGE_ASSESS_SYSTEM,
                messages=[{"role": "user", "content": content}],
                output_format=_ImgAssessmentList)
            p = resp.parsed_output
        except Exception as exc:  # noqa: BLE001 – Bildbewertung darf das Panel nie sprengen
            logger.warning("assess_images failed", extra={"error": str(exc)[:150]})
            return {"assessments": [], "best_index": None, "note": ""}
        if p is None:
            return {"assessments": [], "best_index": None, "note": ""}
        out = []
        for a in (p.assessments or []):
            if 0 <= int(a.index) < len(urls):
                out.append({"index": int(a.index),
                            "score": max(0.0, min(10.0, float(a.score or 0.0))),
                            "reason": (a.reason or "")[:200]})
        # best_index nur akzeptieren, wenn es ueberhaupt Bewertungen gibt – sonst wuerde die
        # Schema-Vorgabe best_index=0 ein 🏆 auf ein UNBEWERTETES Bild setzen (leeres assessments).
        best = int(p.best_index) if out and 0 <= int(p.best_index) < len(urls) else None
        if best is None and out:                # Fallback: hoechster Score gewinnt
            best = max(out, key=lambda x: x["score"])["index"]
        return {"assessments": out, "best_index": best, "note": (p.note or "")[:300]}

    async def read_purchase_receipt(self, *, image_bytes, media_type="image/png"):
        """Beleg-Bild an Claude (Vision) + Schema. Fehler -> {} (Aufrufer meldet 'pruefen')."""
        import base64

        if not image_bytes:
            return {}
        content = [
            {"type": "text", "text": "Lies diesen AliExpress-Beleg aus."},
            {"type": "image", "source": {
                "type": "base64", "media_type": media_type,
                "data": base64.b64encode(image_bytes).decode("ascii")}},
        ]
        # BEWUSST messages.create + JSON, NICHT messages.parse: der parse-Weg lief auf
        # dem VPS in wiederholte APITimeoutError (live 11.08.: 273 s ohne Ergebnis),
        # waehrend create dort in ~2 s antwortet. Das Schema wird hier selbst validiert.
        try:
            client = self._anthropic_client()
            resp = await client.messages.create(
                model=self.settings.llm_model, max_tokens=2000,
                system=_RECEIPT_SYSTEM,
                messages=[{"role": "user", "content": content}], timeout=90.0)
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
        except Exception as exc:  # noqa: BLE001 – Beleg-Lesen darf den Lauf nie sprengen
            # Grund MITGEBEN: ohne ihn ist im Massenlauf nicht zu sehen, ob es am Modell,
            # am Schluessel oder am Bild lag (Vorfall 11.08.: 3 min Warten, keine Ursache).
            logger.warning("read_purchase_receipt failed", extra={"error": str(exc)[:300]})
            return {"_fehler": f"{type(exc).__name__}: {exc}"[:300]}
        return parse_receipt_json(text)

    async def _claude_generate(self, user: str) -> _ListingSchema:
        client = self._anthropic_client()
        resp = await client.messages.parse(
            model=self.settings.llm_model,
            max_tokens=4096,  # Beschreibung mit Bloecken + Footer wird laenger
            system=_LISTING_SYSTEM,
            messages=[{"role": "user", "content": user}],
            output_format=_ListingSchema,
        )
        if resp.parsed_output is None:
            raise PersistentError("LLM lieferte kein valides Schema zurueck")
        return resp.parsed_output

    async def _openai_generate(self, user: str) -> _ListingSchema:
        client = self._openai_client()
        resp = await client.beta.chat.completions.parse(
            model=self.settings.llm_model,
            messages=[
                {"role": "system", "content": _LISTING_SYSTEM},
                {"role": "user", "content": user},
            ],
            response_format=_ListingSchema,
        )
        parsed = resp.choices[0].message.parsed
        if parsed is None:
            raise PersistentError("LLM lieferte kein valides Schema zurueck")
        return parsed

    async def suggest_title(self, *, current_title, competitor_titles, internal_titles=None):
        # System-Prompt erzwingt reine Titel-Ausgabe: ohne dies antwortet das Modell
        # bei "leeren" Konkurrenzdaten im Chat-Ton ("Ich bin bereit, ...") und dieser
        # Text landete frueher als eBay-Titel (Vorfall 07/2026).
        system = (
            "Du bist ein eBay-SEO-Tool. Du gibst AUSSCHLIESSLICH einen einzigen "
            "optimierten Produkttitel aus – eine Zeile, deutsch, keine "
            "Anfuehrungszeichen, keine Erklaerung, keine Einleitung, kein Satzzeichen am "
            "Ende. WICHTIGSTE REGEL: Der Titel MUSS mit dem PRODUKTTYP (Substantiv) beginnen – "
            "dem Wort, das Kaeufer eintippen. Beschreibende Woerter (Farbe, Material, Stil, "
            "Herkunft, Zielgruppe, Karat/Menge: edel, schwarz, japanisch, 925er, Damen) "
            "NIE am Anfang, sondern dahinter in der Grundform. FALSCH 'Edle Schwarze Vase "
            "Deko' -> RICHTIG 'Vase Deko Edel Schwarz'. "
            "Schoepfe die eBay-Kapazitaet VOLL aus: Ziel 78-80 Zeichen, moeglichst nah an "
            "80, NIE unter 72 – lieber ein echtes Merkmal mehr als kuerzen. Fuelle mit ECHTEN "
            "suchrelevanten Keywords (Modell, Material, Farbe, Groesse, Set-Menge, Anwendung, "
            "Synonyme). NIEMALS Werbe-Floskeln ('Premium', 'Top', 'Blitzversand', 'NEU', "
            "'Qualitaet', 'hochwertig'). MEIDE zudem UNBEKANNTE No-Name-Marken ohne Suchvolumen "
            "(z.B. POEDAGAR, PHISHGER, anniyo, SEAMETAL, WIFRU) – solche Markennamen KOMPLETT "
            "weglassen und NICHT erwaehnen; nur etablierte, wirklich gesuchte Marken nennen. "
            "WICHTIG: Deine eigene Zeichenzaehlung ist unzuverlaessig (v.a. bei Umlauten aeoeuess) "
            "– zaehle NICHT selbst, schreibe lieber einen deutlich VOLLEREN Titel bis an die "
            "80-Zeichen-Grenze; ein System kuerzt bei Bedarf sicher. Wenn Infos fehlen, optimierst "
            "du den vorhandenen Titel trotzdem. Orientiere dich an dem, was NACHWEISLICH verkauft "
            "(Top-Angebote der Konkurrenz + meine eigenen Bestseller) – uebernimm deren bewaehrte "
            "Keyword-Struktur, aber aendere den Titel NICHT willkuerlich und bleibe beim "
            "TATSAECHLICHEN Produkt (keine erfundenen Marken/Modelle/Eigenschaften). Wenn aehnliche "
            "Artikel sonst mit demselben Wort beginnen wuerden, VARIIERE das Einstiegs-Keyword "
            "(nicht alle Titel identisch anfangen lassen)."
        )
        hints = ("\n\nErfolgreiche Konkurrenz-Titel (eBay Best-Match – bewaehrte Formulierungen):\n"
                 + "\n".join(f"- {t}" for t in competitor_titles)) if competitor_titles else ""
        intern = ("\n\nMEINE eigenen, GUT VERKAUFTEN aehnlichen Artikel (bewaehrt – Stil & Keyword-"
                  "Reihenfolge uebernehmen, wo es zum Produkt passt):\n"
                  + "\n".join(f"- {t}" for t in internal_titles)) if internal_titles else ""
        # Keywords aus BEIDEN Quellen schuerfen (Konkurrenz + eigene Bestseller).
        mined = self._mine_keywords(list(competitor_titles or []) + list(internal_titles or []))
        kw_line = ("\n\nHaeufige Keywords in erfolgreichen Angeboten (einbauen, wenn passend): "
                   + ", ".join(mined)) if mined else ""
        prompt = (
            "Optimiere diesen eBay-Titel fuer die Suche: starke Keywords voran, Modell/"
            "Eigenschaften, keine Fuellwoerter, KEINE unbekannten No-Name-Marken. Ziel 78-80 "
            "Zeichen – fuelle die Kapazitaet bis nah an 80 aus, mach den Titel NICHT kuerzer als "
            "noetig. Orientiere dich an den bewaehrten Titeln unten (ohne den Produktbezug zu "
            "verlieren) und variiere das Einstiegs-Keyword. Antworte nur mit dem Titel.\n\n"
            f"Aktueller Titel: {current_title}{hints}{intern}{kw_line}"
        )
        async def _one(pr: str) -> str:
            if self.settings.llm_provider == "openai":
                client = self._openai_client()
                resp = await client.chat.completions.create(
                    model=self.settings.llm_model,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": pr}])
                return resp.choices[0].message.content or ""
            client = self._anthropic_client()
            resp = await client.messages.create(
                model=self.settings.llm_model, max_tokens=64, system=system,
                messages=[{"role": "user", "content": pr}])
            return "".join(b.text for b in resp.content if b.type == "text")

        try:
            text = await _one(prompt)
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        title = self._enforce_title(text, [], warn_below=0)
        # Code misst exakt und laesst bei zu kurzem Titel gezielt auffuellen (LLM-Zaehlschwaeche).
        title = await self._expand_title_if_short(title, keywords_line=kw_line)
        title = await self._fix_title_word_order(title)    # Produkt nach vorn (SEO)
        warnings: list[str] = []
        return self._enforce_title(title, warnings,
                                   warn_below=self.settings.title_warn_below_chars)
