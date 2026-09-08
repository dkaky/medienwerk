# Listing-Stil-Leitfaden (Druckhelden)

Abgeleitet aus bewährten Bestands-Listings. Dient als Referenz für den
System-Prompt in [`app/integrations/llm.py`](../app/integrations/llm.py) und als
Few-Shot-Material. Geltung: eBay.de, Kleinunternehmer nach § 19 UStG, deutscher Markt.

## Titel
- **Wichtigste Regel — Titelanfang:** Der Titel **beginnt mit dem Produkttyp** (Substantiv),
  also mit dem Wort, das Käufer eintippen. eBay gewichtet die ersten Wörter am stärksten.
  Beschreibende Wörter (Farbe, Material, Stil, Herkunft, Zielgruppe, Karat/Menge) stehen
  **nie am Anfang**, sondern dahinter — dort in der Grundform.
  - ❌ `Edle Schwarze Vase Deko Japanisch` → ✅ `Vase Deko Edel Schwarz Japanisch`
  - ❌ `925er Silber Halskette Damen` → ✅ `Halskette Damen Silber 925er`
  - Durchgesetzt per Prompt **und** deterministisch: `leading_descriptor()`
    (`app/integrations/llm.py`) fängt Adjektiv-/Zahl-Anfänge ab und lässt einmal umsortieren.
- **72–80 Zeichen** (oberes Ende anpeilen), **Title Case**.
- Trenner: Kommas und `&`; im Titel ist `–` (Gedankenstrich) als Trenner erlaubt.
- Inhalt: Produkttyp **zuerst** + Hauptmerkmale + Größen/Mengen/Farben + Anwendungsbereich.
- Keyword-Reihenfolge geht vor Lesefluss — der Titel ist eine Suchzeile, kein Werbesatz.
- Vorteils-Phrasen statt reiner Specs ("Weich & Schnell Trocknend") — aber erst **nach** dem Produkttyp.
- **Marken:** etablierte mit Suchvolumen rein (SCVCN, KAPVOE, ROCKBROS …);
  No-Name raus (SEAMETAL, cazador, anniyo, WIFRU …); Konkurrenzmarken entfernen.
- **VeRO sofort flaggen** (Thomas Sabo, Pandora, Disney, **Fifty Shades**, NBA/NFL …).
- **Identität/Diaspora:** Begriff zweisprachig (Deutsch + Landessprache):
  Syrien+Syria, Polen+Polska+Pole, Türkei+Türkiye, Iran+Shir-o Khorshid, Palästina+Filistin/Falastin.
- Bei ähnlichen Produkten Einstiegs-Keyword variieren (SEO-A/B) — aber immer unter
  Produkt-Synonymen (Vase/Blumenvase/Deko-Vase), nie über ein beschreibendes Wort.

## Beschreibung
- Block-Aufbau: **Emoji + knappe Überschrift (eigene Zeile) + 1–3 Sätze**. **Keine Markdown-Sternchen** (eBay = HTML).
- Reihenfolge: Hook (Emoji + 3 kurze Statements, dann USP-Satz) → Hauptmerkmal → Material/Qualität → Varianten → Oberflächen-Hinweis (Schmuck) → Maße → Anwendungsbereiche → Zielgruppe/Anlass → Lieferumfang → Footer.
- **Mehrteilige Sets:** jede Komponente eigene Zeile mit Maß + Zweck.
- **"Vielseitig einsetzbar":** kommagetrennte Aufzählung.
- Nur tatsächlich bestellte Varianten nennen; Mehrlängen als Vorteil.

## Artikelmerkmale (item_specifics)
- **Richtwert 12 Merkmale** (10–14) — je mehr, desto besser für die eBay-Suche,
  aber nur inhaltlich korrekte, aus den Produkt-Specs ableitbare Merkmale (nichts erfinden).
- Immer dabei: **Marke** (No-Name → „Markenlos"); dazu Farbe, Material, Größe/Maße,
  Gewicht, Stil, Anlass, Themen/Motive, Einsatzbereich, Abteilung … soweit ableitbar.
- **Alle Werte auf Deutsch.** Trifft ein Standard-Merkmal nicht zu → **„Nicht zutreffend"**
  (nie englisch „Does not apply"; gilt auch für EAN/MPN, gesetzt in `ebay.py`).

## Pflicht-Bausteine
**Schmuck-Disclaimer (Gold/Silber):**
> ℹ️ Hinweis zur Oberfläche
> Goldfarbig und silberfarbig beschichteter Edelstahl. Modeschmuck, kein Echtgold oder Echtsilber.

**Standard-Footer (unverändert, immer am Ende):**
> 📦 Sobald Ihr Paket unterwegs ist, erhalten Sie selbstverständlich eine Sendungsverfolgung, sodass Sie jederzeit wissen, wann Ihr Produkt bei Ihnen ankommt.
> 📩 Fragen? Unser Kundenservice hilft jederzeit gerne.
> Als Kleinunternehmer im Sinne von § 19 Abs. 1 UStG wird keine Umsatzsteuer berechnet

## Sprach-Regeln
- Bindestriche/Em-Dashes nur in Komposita (im Titel auch als Trenner); nicht als Satzzeichen im Fließtext.
- Generisches Maskulinum bzw. klare Zielgruppen (Damen, Herren, Unisex, Kinder); keine Doppelformen.
- Keine "nicht … sondern"-Konstruktionen, keine schließenden Floskeln, keine Qualifier ("laut Bildern").
- Positiv formulieren, Verneinungen vermeiden.

## Strategische Prüfungen (vor jedem Listing)
1. **Markenrisiko / VeRO** erkennen und melden.
2. **Widersprüche** zwischen Specs und Text flaggen; Übersetzungsmüll ignorieren
   (z. B. "Hochbetriebenes Chemikalienunternehmen", "Keine", "CN (Herkunft)").
3. **Falsche Vermarktung** vermeiden (Schlüssel- vs. Zahlenschloss, BT vs. GPS …).
4. **Zielgruppen-Optimierung** über Diaspora-Begriffe.

---

## Kanonische Beispiele (aus dem eBay-Projekt)

### A) Mehrteiliges Set (No-Name entfernt)
**Titel:** `Mikrofaser Auto-Waschset 9-teilig mit Handschuh, Bürste & Schwamm in Grau/Orange` (80)
*Strategie:* SEAMETAL = No-Name ohne Suchvolumen → entfernt, Platz für Keywords. Komponenten einzeln mit Maßen, zwei Farbvarianten, Anwendungsbereiche als Liste.

### B) Identitätsprodukt (Diaspora-SEO, Schmuck-Disclaimer)
**Titel:** `Syrien Syria Adler Saladin Halskette Anhänger Edelstahl Gold Silber 45cm Unisex` (79)
*Strategie:* Marke "Cazador" raus. Syrische Diaspora (>800k in DE) gezielt angesprochen. Kontextblock zum Saladin-Adler. Nur 2 von 4 Farben (Gold/Silber) geordert → nur diese genannt. 45cm + 5cm Verlängerung als Vorteil. Pflicht-Disclaimer für beschichteten Edelstahl.

### C) VeRO-Risiko korrekt behandelt
**Titel:** `BDSM Triskele Halskette Ring Anhänger Edelstahl Gold Silber Schwarz Punk 50 60cm` (80)
*Strategie:* "Fifty Shades of Grey" ist eingetragene Marke → **niemals** in Titel/Text (VeRO). Triskele (1995 Public Domain) ist der korrekte, SEO-starke Begriff. 7 Varianten transparent aufgelistet.
