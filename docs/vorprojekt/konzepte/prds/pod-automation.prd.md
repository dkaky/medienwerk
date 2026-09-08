# POD-Automatisierung — KI-Designs → Printify → eBay

## Problem
Ein Solo-Betreiber (neu angemeldetes Gewerbe, §19 Kleinunternehmer, DE/AT/CH) will einen Print-on-Demand-Shop auf **Masse** bringen: viele Designs über viele Nischen (Junggesellenabschiede, lustige Sprüche, breite Motivpalette). Der Flaschenhals ist **manuelle Arbeit** — jedes Design gestalten, als Produkt anlegen, mit Varianten und optimiertem Titel listen. Von Hand skaliert das nicht auf hunderte Produkte. Ohne Automatisierung bleibt der Shop klein und kann nicht genug Motive/Nischen testen, um Verkaufsschlager zu finden.

## Evidence
- **Annahme — noch zu validieren.** Es gibt bisher **keine** eigenen Verkaufsdaten: eBay-Konto, Printify-Konto und KI-Zugang existieren noch nicht; das Gewerbe ist neu.
- Marktannahme des Betreibers: Nachfrage nach JGA-/Spruch-/Nischen-Merch ist real und breit. → Zu validieren über: erste Live-Listings + reale eBay-Verkäufe (kleiner Testschwarm an Designs).
- Bekanntes Muster: POD + Marktplatz-Listing ist ein etabliertes Geschäftsmodell; der Hebel liegt in Menge × Trefferquote der Motive.

## Users
- **Primär**: Der Betreiber selbst — steuert das System, bringt eigene Motiv-Konzepte ein und lässt die KI zusätzlich Themen erfinden. Braucht: aus einem Design mit möglichst wenig Klicks fertige, gelistete Produkte.
- **Nicht für**: Endkunden (die kaufen nur auf eBay), Teams/Mehrbenutzer, andere Marktplätze als eBay (vorerst).

## Hypothesis
Wir glauben, dass eine **Pipeline „KI-Design → Printify-Produkt → automatisiertes eBay-Listing"** das manuelle Skalierungsproblem löst und dem Betreiber erlaubt, in kurzer Zeit **hunderte Motive** live zu testen.
Wir wissen, dass wir richtig liegen, wenn **aus 100 generierten Designs automatisiert Produkte entstehen und gestaffelt bis zu 400 Varianten-Listings auf eBay live gehen — und die ersten Verkäufe eingehen.**

## Success Metrics
| Metrik | Ziel | Wie gemessen |
|---|---|---|
| Automatisiert generierte Designs (Start) | 100 | Anzahl freigegebener Design-Dateien im System |
| Live-Produkte/Listings auf eBay | 400 (Zielzustand, gestaffelt) | eBay-Listings gezählt; Staffelung an eBay-Verkaufslimit gebunden |
| Manuelle Schritte pro Design→Listing | ≤ 1 (Freigabe-Klick) | Beobachteter Workflow |
| Erste Verkäufe | ≥ 1 (dann steigend) | eBay-Bestellungen |
| Abgemahnte/beanstandete Listings (IP) | 0 | eBay-Meldungen / manuelle Stichprobe |

## Scope
**MVP** — Nur diese drei Fähigkeiten, plus Sicherheits-/Rechte-Filter:
1. **KI-Design-Generator** — erzeugt Motive aus (a) KI-erfundenen Themen und (b) eigenen Konzept-Vorgaben des Betreibers; inkl. **IP-/Markenrechts-Filter** (blockt geschützte Marken, Promis, Pop-Kultur-Bezüge) und Ausschluss verbotener Inhalte.
2. **Printify-Produkterstellung** — Design per Printify-API auf Produkte (Shirt/Bekleidung/Tasse/…) legen inkl. Mockups; Printify-Anbindung hinter einer Schnittstelle gekapselt (späterer Anbieterwechsel möglich).
3. **eBay-Bulk-Listing** — Produkte automatisiert auf eBay listen mit optimierten Titeln/Keywords; Freigabe-gesteuert; Staffelung entlang des eBay-Verkaufslimits.

**Out of scope**
- Bestell-Import, Fulfillment, Versand, Tracking — *macht Printifys nativer eBay-Channel automatisch.*
- Eigenes Order-Routing/Zahlungsabwicklung — nicht bauen.
- Weitere Marktplätze (Amazon, Etsy, eigener Webshop) — später.
- Kundenkommunikation, Retouren, Buchhaltung — später.
- Mehrbenutzer/Team-Funktionen.

## Delivery Milestones
<!-- Business-Ergebnisse, keine Engineering-Tasks. /plan macht aus jedem einen Plan. -->

| # | Milestone | Outcome | Status | Plan |
|---|---|---|---|---|
| 0 | Zugänge & Rechtsbasis | eBay-Verkäuferkonto (gewerblich) + Printify-Konto + KI-API eingerichtet; eBay-Developer-Keys vorhanden; Verkaufslimit geklärt | pending | — |
| 1 | KI-Design-Generator (mit IP-Filter) | Betreiber erhält aus KI-Themen + eigenen Konzepten fertige, rechtlich gefilterte Design-Dateien; erste 100 verfügbar | in-progress | `.claude/plans/pod-automation.plan.md` |
| 2 | Printify-Produkterstellung | Aus einem freigegebenen Design entstehen automatisch Produkte + Mockups bei Printify | pending | — |
| 3 | eBay-Bulk-Listing | Produkte gehen mit optimierten Titeln/Keywords automatisiert auf eBay live (gestaffelt bis 400) | pending | — |
| 4 | Erster Live-Betrieb & Validierung | Testschwarm live, erste Verkäufe, IP-Beanstandungen = 0 | pending | — |

## Open Questions
- [ ] **KI-Bildmodell:** OpenAI `gpt-image-1` (starke Textwiedergabe für JGA-Namen/Sprüche) vs. fal.ai/Flux vs. andere — Entscheidung via kleinem Kosten-/Qualitäts-Test (Textschärfe, Kosten/Bild, kommerzielle Nutzungsrechte). Budget laut Betreiber vorerst nachrangig.
- [ ] **eBay-Verkaufslimit:** Wie hoch startet das gewerbliche Neukonto, wie schnell lässt es sich anheben? Bestimmt die Staffelung Richtung 400.
- [ ] **Überlappung Printify↔eBay:** Printifys nativer eBay-Channel kann selbst publishen. Klären, welcher Teil des Listings nativ läuft und wo Eigenbau (Bulk, Titel/Keyword-Optimierung) echten Mehrwert bringt — Doppelarbeit vermeiden.
- [ ] **IP-Filter-Tiefe:** Regelbasiert (Sperrliste) reicht für MVP, oder braucht es Prüfung gegen Markenregister? MVP-Annahme: konservative Sperrliste + manuelle Stichprobe.
- [ ] **Design→Varianten-Mapping:** Welche Produkttypen/Varianten pro Design standardmäßig (nur Shirt, oder Shirt+Tasse+…)?
- [ ] **Titel/Keyword-Optimierung:** regelbasiert oder KI-gestützt? Quelle für Keywords?

## Risks
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| eBay-Verkaufslimit für Neukonto bremst „400 live" massiv aus | Hoch | Hoch | 400 als gestaffelten Zielzustand behandeln; Konto verifizieren; Limit-Anhebung aktiv beantragen; früh reale Verkäufe für Historie |
| Marken-/Urheberrechts-Abmahnungen bei „lustigen Motiven"/Pop-Kultur | Hoch | Hoch | IP-Filter als MVP-Pflichtteil; konservative Sperrliste; manuelle Stichprobe vor Live |
| KI-Textwiedergabe zu schwach (verzerrte Sprüche/Namen) | Mittel | Hoch | Modellwahl per Test; gpt-image-1 als Favorit; Fallback: Text als sauberes Overlay statt vom Modell gerendert |
| Kommerzielle Nutzungsrechte der KI-Bilder unklar | Mittel | Hoch | Nur Modelle mit klarer kommerzieller Lizenz nutzen; im Modell-Test prüfen |
| Doppelarbeit gegen Printifys nativen eBay-Channel | Mittel | Mittel | Vor Bau von Listing-Logik native Fähigkeiten testen; nur den Delta-Mehrwert bauen |
| Alle Accounts/APIs fehlen → Blocker vor Zeile 1 Code | Sicher | Mittel | Milestone 0 zuerst; Design-Generator (M1) lässt sich aber schon vor eBay-Accounts bauen/testen |

---
*Status: DRAFT — nur Requirements. Implementierungsplanung folgt via /plan.*
