# Rechts- & Pflichten-Checkliste (Druckhelden) — Stand 14.08.2026

> Diese Punkte kann nur der Betreiber selbst erledigen. Keine Rechtsberatung —
> bei Unsicherheit Steuerberater/Anwalt fragen. Reihenfolge = Prioritaet.

> **Aenderung gegenueber 11.07.:** Der Schwerpunkt liegt jetzt auf Handelsware von
> AliExpress (Drucke, Wandbilder, bedruckte Shirts) statt auf Printify.

## 0. Pflicht-Merkmal "Hersteller" beim Einstellen (technisch)
- eBay verlangt je nach Kategorie ein Merkmal "Hersteller". Im Handelsteil wird
  es beim Einstellen automatisch geheilt: gefuellt mit der Marke, sonst mit
  "Sonstige".
- [ ] Beim Portieren des Listing-Teils dieselbe Heilung uebernehmen, sonst
      scheitert das Veroeffentlichen an fehlenden Pflicht-Merkmalen.

## 0b. Textilkennzeichnung (PFLICHT bei Shirts)
- [ ] Faserzusammensetzung je Artikel angeben ("100 % Baumwolle")
- Bei AliExpress-Ware steht sie oft ungenau oder gar nicht dabei -> beim
      Lieferanten erfragen und korrekt uebernehmen. Klassischer Abmahngrund.

## 1. Verpackungsregister LUCID (PFLICHT vor dem ersten Verkauf)
- [ ] Registrieren: https://lucid.verpackungsregister.org (kostenlos, ~15 Min)
- [ ] LUCID-Nummer erhalten und notieren
- [ ] Systembeteiligung bei einem dualen System abschliessen (z. B. Lizenzero,
      Zmarta/Recycling-Dienste; fuer POD/Dropshipping-Kleinstmengen guenstig)
- [ ] LUCID-Nummer in den eBay-Verkaeufereinstellungen hinterlegen
- Hintergrund: gilt AUCH bei Dropshipping/POD, wenn du als Haendler in DE
  verpackte Ware an Endkunden in Verkehr bringst. eBay sperrt Angebote ohne
  LUCID-Nummer zunehmend automatisch.
- WICHTIG (Printify klaeren): Wer ist "Erstinverkehrbringer" der Verpackung,
  wenn Printify aus DE (Textildruck Europa) versendet? Konservative Praxis:
  selbst registrieren. -> Frage fuer den Steuerberater/Support.

## 2. eBay-Shop-Rechtstexte (PFLICHT)
- [ ] Impressum vollstaendig (Name, Anschrift, E-Mail — Gewerbedaten)
- [ ] Widerrufsbelehrung + Muster-Widerrufsformular hinterlegen
- [ ] AGB (optional, aber empfohlen) — Quellen: IT-Recht Kanzlei, Haendlerbund
      (kostenpflichtige Schutzpakete inkl. Abmahnschutz, ~10-15 EUR/Monat)
- [ ] Datenschutzerklaerung
- [ ] Hinweis: KEINE Garantieversprechen/Servicezeiten in Beschreibungen
      (rechtlich bindend — Team-Regel)

## 3. Steuerberater-Fragen (§ 19 UStG Kleinunternehmer)
- [ ] Printify-Rechnungen: faellt auf Basis+Versand 19 % USt an, oder greift
      Reverse-Charge (Printify = irischer Anbieter)? Als § 19 KEIN Vorsteuerabzug
      -> beeinflusst die ECHTE Marge (ggf. ~19 % hoeherer Einkauf)!
- [ ] Umsatzgrenze § 19 im Blick: 25.000 EUR Vorjahr / 100.000 EUR laufend
      (Stand 2025-Reform) — beide Gewerbe getrennt oder zusammen zaehlen?
      (Gleiche Person -> i. d. R. ZUSAMMEN. Klaeren!)
- [ ] AliExpress-Poster-Stream: Einfuhr/Zoll-Handling wie im bestehenden
      Gewerbe (Pauschalzoll je Sendung seit 07/2026 beachten)
- [ ] Getrennte Buchfuehrung der beiden Gewerbe (Konten/Belege)

## 4. eBay-Konto Druckhelden
- [x] Verkaeuferregistrierung abgeschlossen, Verkaufslimit 656.570 EUR / 75.000 Stk
      (per Schnittstelle geprueft am 14.08.2026 — kein Neukonto-Limit)
- [x] Entwicklerzugang + Verkaufsschnittstelle angebunden
- [x] Verkaufsbedingungen angelegt: kostenloser Versand DE mit 7 Tagen
      Bearbeitung, 30 Tage Ruecknahme (Rueckversand Kaeufer), Standard-Zahlung
- [ ] Zahlungsabwicklung (eBay Payments) aktiv
- [ ] Steuernummer nachtragen, sobald sie da ist (siehe Abschnitt 4b)

## 4b. Steuernummer fehlt noch — was trotzdem geht
- Antraege sind gestellt, die Nummer steht aus. Bewertung:
  - Kleinbetragsrechnungen bis 250 EUR: **zulaessig ohne Steuernummer** (§ 33
    UStDV verlangt sie dort nicht) — genau die stellt das System aus
  - Rechnungen ueber 250 EUR: Steuernummer ist Pflichtangabe
  - eBay-Verkaeuferkonto: fragt sie ab und kann das Konto einschraenken
- [ ] Beim Finanzamt telefonisch nach dem Bearbeitungsstand fragen — viele
      Aemter nennen die Nummer am Telefon, waehrend der Brief noch laeuft

## 4c. Wo der § 19 vermerkt sein muss (drei Stellen)
- [ ] **eBay-Verkaeufereinstellungen:** so einstellen, dass KEINE Umsatzsteuer
      ausgewiesen wird. Wichtigster Punkt: Wer als Kleinunternehmer USt ausweist,
      schuldet sie auch, ohne sie eingenommen zu haben.
- [x] **Artikelbeschreibung:** fester Abschluss mit dem § 19-Satz, seit 14.08.
      automatisch unter jeder Beschreibung (app/services/listing_gen.py, FOOTER)
- [x] **Rechnungen:** "Kleinunternehmer nach § 19 UStG, kein Vorsteuerabzug"
      (app/services/receipt_service.py)
- Im Impressum nicht vorgeschrieben, aber ueblich.

## 4d. Rechtstexte — kostenlos beschaffen
- Rechtstexte muessen fuer dieses Unternehmen eigens beschafft werden.
- **Nicht aus einem fremden Konto kopieren:** Stammen die Texte aus einem
  Schutzpaket, gilt die Lizenz nur fuer den Betrieb, der sie gekauft hat.

Kostenlose Quellen, nach Verlaesslichkeit sortiert:

- [ ] **Widerrufsbelehrung + Widerrufsformular: amtliches Muster** aus dem EGBGB
      (Anlage 1 und Anlage 2 zu Art. 246a). Kostenlos, im Gesetzestext auf
      gesetze-im-internet.de nachlesbar. Wer das Muster UNVERAENDERT uebernimmt
      und nur die eigenen Daten einsetzt, erfuellt die Anforderungen kraft
      Gesetzes — das ist die sicherste kostenlose Variante ueberhaupt.
- [ ] **Impressum: Generator von e-recht24.de** (Basisversion kostenlos).
      Pflichtangaben nach § 5 DDG: Name, Anschrift (kein Postfach), E-Mail,
      Telefon, Gewerbe/Rechtsform, ggf. USt-IdNr. — als Kleinunternehmer ohne
      USt-IdNr. entfaellt diese Zeile.
- [ ] **Datenschutzerklaerung: Generator von e-recht24.de** (Basisversion
      kostenlos) — fuer eBay reicht die Standardvariante, da keine eigene
      Website mit Tracking betrieben wird.
- [ ] **IHK fragen.** Als Gewerbetreibender bist du ohnehin Pflichtmitglied und
      zahlst Beitrag. Die meisten IHKs bieten kostenlose Merkblaetter UND eine
      kostenlose Erstberatung zu genau diesen Texten. Guenstiger geht Beratung
      durch einen Juristen nicht.
- **AGB sind KEINE Pflicht.** Ohne AGB gilt schlicht das Gesetz, was fuer einen
  Kleinhaendler meist voellig ausreicht. Fuer den Start weglassen.

Was du beim kostenlosen Weg NICHT bekommst: den Abmahnschutz (Kostenuebernahme
im Streitfall) der Bezahlpakete und die automatische Aktualisierung bei
Gesetzesaenderungen. Bei Aenderungen musst du die Texte selbst nachziehen.

## 5. Marken-/Urheberrecht (laufende Pflicht)
- Automatischer IP-Filter blockt bekannte Marken/Franchises (Blocklist in
  src/pod/safety/blocklist.yaml — regelmaessig ERWEITERN)
- [ ] Vor jedem Live-Gang manuelle Stichprobe der Motive/Titel
- Poster-Stream: NUR generische Motive (Natur, abstrakt, Sprueche ohne Marke) —
  auch der physische Weiterverkauf von Marken-Postern verletzt Markenrecht

## 6. Wenn die ersten Verkaeufe laufen
- [ ] Ersten echten Printify-Order-Sync gemeinsam pruefen (Feld-Mapping
      Verkaufspreis/Kosten gegen echte Daten verifizieren)
- [ ] Belege (Printify-Rechnungen) monatlich ablegen -> CSV-Export aus dem
      Dashboard (Finanzen) fuer den Steuerberater
