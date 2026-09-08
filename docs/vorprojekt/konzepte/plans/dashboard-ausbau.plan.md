# Plan: Dashboard-Ausbau — Feature-Übernahme aus ebay-automation

**Quelle**: Feature-Inventur (3-Agenten-Workflow, 12.07.2026): 37 UI-Features, 30 Backend-Features,
10 Automatisierungen, 39 UI-/Architektur-Muster aus `C:\Users\HP\Projekte\eBay-Automation`.
**Complexity**: Large (4 Ausbaustufen, je einzeln shippbar)

## Summary
Das aktuelle Dashboard (Phasen 1–5) ist funktional, aber karg — das alte eBay-Dashboard hat
jahrelang gereifte Features und UX-Muster, die wir gezielt übernehmen. Vier Ausbaustufen,
sortiert nach Abhängigkeit: **P6** braucht nichts (sofort), **P7** braucht die AliExpress-API-
Keys, **P8** nutzt den vorhandenen OpenAI-Key, **P9** wartet auf das eBay-Token-Setup.

## Patterns to Mirror (aus ebay-automation)
| Kategorie | Muster |
|---|---|
| Governance | **Propose-only überall**: KI/System schlägt vor (current vs. suggested), Mensch klickt frei; Geld-/Außen-Aktionen NIE automatisch; nie auto-löschen (nur „Aktion erforderlich"-Flag) |
| Jobs | Sofort-202 + Hintergrund-Worker + Run-Lock; Fehler AM OBJEKT (max 500 Z.) statt nur im Log; transiente Fehler auto-retry (Backoff) + Cron-Sicherheitsnetz; Zombie-Cleanup beim Start |
| UI | KPI-Karten klickbar als Tabellen-Filter; Karten-Header als Toolbar (Suche+Filter+Aktionen); Inline-Aktions-Buttons mit title-Tooltips; 40px-Thumbnails; EIN wiederverwendetes Modal; Erklärtext unter jeder Karte; dependency-freie SVG-Charts; Toasts; Kopieren-per-Klick; Downloads als `<a>` auf GET |
| Eingaben | Komma-Zahlen akzeptieren, Marge als % oder Bruch, dry_run=true als Default bei Bulk |
| Cron | KI nie im Cron (nur Daten-Refresh), teure Reports nachts vorcachen, Nachtjob-Blöcke in eigenen try/except |

## Ausbaustufe P6 — Fundament & Optik (KEINE externen Voraussetzungen)
Behebt „extrem mau" + übernimmt die Infrastruktur-Kronjuwelen.

### Task 6.1: TaskLog / Aktivitäts-Feed + Zombie-Cleanup
- task_logs-Tabelle + Contextmanager (Port aus services/common.py); JEDE Aktion (Generierung, Bulk, Publish, Sync) schreibt hinein
- Feed auf der Übersicht („Was ist passiert"); Zombie-Tasks >2h beim App-Start auf failed
- **Validate**: Feed zeigt Design-Generierung + Printify-Bulk chronologisch

### Task 6.2: Persistente Publish-/Job-Selbstheilung
- Jobs-Tabelle bekommt Retry-Zähler; hängende queued/running-Jobs beim Start neu einreihen oder failed markieren; Fehler am Listing (publish_error-Muster) statt nur im Job
- **Validate**: Server-Kill mitten im Job → Neustart → Job als failed sichtbar, Retry-Knopf

### Task 6.3: Übersicht aufwerten (Charts + Status-Breakdown)
- Dependency-freier SVG-Chart: Umsatz+Gewinn-Zeitreihe mit 7/30/90-Tage-Wahl (Daten: orders daily aggregation); Bestellungen-nach-Status-Breakdown; KPI-Tooltips
- **Validate**: Chart rendert mit Fake-Orders, Zeitraumwechsel funktioniert

### Task 6.4: Detail-Modals + Mockup-Anzeige
- Zentrales Modal (ein DOM-Knoten, Inhalt getauscht); Design-Detail (Großbild, Metadaten, Historie); Produkt-Detail mit **echten Printify-Mockup-Bildern** (GET product → images) — das macht das Dashboard sofort „lebendig"
- **Validate**: Klick auf Produktzeile zeigt Printify-Mockups

### Task 6.5: Tabellen-UX-Paket
- Live-Suche + klickbare KPI-Filter in Designs/Produkte/Listings/Poster; Status-Badges einheitlich; Erklärtexte unter Karten; Kopieren-per-Klick auf IDs; Hell/Dunkel-Theme
- **Validate**: Suche filtert live; KPI-Klick filtert Tabelle

### Task 6.6: Betriebsausgaben + §19-Verkaufsrechnungen (POD)
- expenses-Tabelle (Datum/Betrag/Kategorie/Beleg-Upload) — Printify-Abo, OpenAI, Hosting sofort erfassbar; §19-Rechnung je Order idempotent (Port invoice_service-Kern, COGS=Printify); Steuerberater-ZIP (Belege+Index+Summen)
- Fließt in Finanzen-Report (belegte Zahlen, wie gehabt)
- **Validate**: Ausgabe erfassen → taucht in Monatsreport+ZIP auf

## Ausbaustufe P7 — Poster-Power (VORAUSSETZUNG: AliExpress-API-Keys)
**⚠️ ENTSCHEIDUNG NÖTIG**: AliExpress-App-Keys aus ebay-automation/.env wiederverwenden
(gleicher Developer-Account, technisch sauber, getrennte .env) — Ja/Nein?

### Task 7.1: AliExpress-Integration portieren
- aliexpress_api.py-Kern (Produkt-Daten, **Freight Query** mit selectedSkuId/locale-Quirks, fee_cent=EUR) hinter SupplierBackend-ABC; Mock für Tests
### Task 7.2: URL-Import statt Handeingabe
- AliExpress-URL einfügen → Titel/Bilder/Preis/Versand automatisch extrahiert → PosterSource (Muster „Produktupload aus Lieferanten-URL")
### Task 7.3: Poster-Research mit Margen-Filter
- Kriterien-Suche (Nischen, EK-Spanne, Min-Marge 25%, Rating≥4, Lieferzeit≤10T) als Hintergrund-Job mit Run-Lock; Triage-Workflow (offen/gemerkt/verworfen) + Sortierung
- Erfüllt nebenbei den offenen Auftrag „100 Produktvorschläge zum Review" für Poster
### Task 7.4: Lieferanten-Sync + Preis-Check-Ampel
- EK/Bestand-Sync je Quelle (Button + 6h-Cron); Ampel-Report (Aktion erforderlich/Anhebung nötig/passt/ausverkauft); OOS → Listing-Flag (nie auto-beenden)
- **Validate je Task**: offline via Mock-Supplier; live-Smoke mit einem echten Poster

## Ausbaustufe P8 — KI-Assist (OpenAI-Key vorhanden) — ERLEDIGT 07.08.2026
### Task 8.1: Trend-Recherche (Design-Trends) ✔
- Recherche per Knopfdruck **und** als opt-in-Nachtjob (`POD_TREND_RESEARCH_ENABLED`, default AUS), Kostendeckel `POD_TREND_MAX_IDEAS` (default 10, hart max 25) gilt für den ganzen Lauf
- Ideen landen in `design_ideas` zur Sichtung (neu/uebernommen/verworfen); IP-Filter greift VOR der Ablage, Gesperrtes wird gezählt statt gespeichert
- LLM: OpenAI-Websuche-Modell (`POD_OPENAI_SEARCH_MODEL`)
- Statt Trend-Box auf der Übersicht: eigene Ansicht „KI-Assist" — die Ideen brauchen Platz für die Entscheidung
### Task 8.2: KI-Freitext-Edit (propose-only) ✔
- `POST /api/v1/ai/suggest-text` liefert current vs. suggested für Titel/Spruch/Motiv und schreibt nichts; der Vorschlag läuft selbst noch durch die Sperrliste
### Task 8.3: Scheduler-Grundgerüst ✔
- **Abweichung:** eigener kleiner `DailyScheduler` (app/scheduler.py) statt APScheduler — gebraucht wird nur „einmal am Tag ab Uhrzeit X", eine zusätzliche Abhängigkeit im Startpfad wäre Risiko ohne Gegenwert
- Jeder Nachtjob in eigenem try/except; der Lauf wird vor der Ausführung als „heute erledigt" vermerkt (kein Minutentakt nach Absturz); jeder Lauf ist als Job im Aktivitäts-Feed sichtbar; Uhrzeiten in lokaler Zeit

## Ausbaustufe P9 — eBay-Betrieb (VORAUSSETZUNG: Token-Setup)
### Task 9.1: ebay.py-Port (Listing-Teil) + echtes Publish über die Queue
### Task 9.2: Order-Polling (5 Min) + Storno-Erkennung mit Prüf-Workflow
  - ⚠️ bekannten Duplikat-Bug des alten sync NICHT mitportieren (Root-Cause dort ungefixt) — Upsert-Muster aus unserem Phase-4-Sync verwenden
### Task 9.3: Echte eBay-Gebühren (Finances API) → echte Marge im Finanzreport
### Task 9.4: Traffic-Report → Sales-Hub mit 3 Funnel-Buckets (Preis/Titel/Reaktivieren) + Optimierungs-Nachverfolgung (vorher/nachher)
### Task 9.5: Varianten-Preis-Editor (Vorschau → Push) + Preis-Übernahme per Klick

## Bewusst NICHT übernommen
- AutoDS-Adapter (tot), Bulk-Varianten-Reparatur (Altlast), Bildsuche-Quellenfinder (nice-to-have später), Selenium-Browser-Extraktion (fragil; Poster-Belege kommen aus AliExpress-API/manuell), Auto-Fulfillment (POD: macht Printify; Poster: bewusst manuell mit Freigabe)

## Risks
| Risk | Likelihood | Mitigation |
|---|---|---|
| Scope-Explosion (77 Features!) | Hoch | Stufen einzeln shippen; jede Stufe endet mit Tests+Review+Commit |
| AliExpress-Key-Reuse kollidiert mit Live-System (Rate-Limits) | Mittel | Eigene App-Keys wären sauberer — Entscheidung; sonst Rate-Limit-Puffer + keine Cron-Syncs tagsüber |
| Trend-Recherche-Kosten | Niedrig | opt-in, Kosten-Deckel, default aus |
| Duplikat-Bug aus altem Order-Sync einschleppen | Mittel | Nicht portieren; eigenes Upsert-Muster (Phase 4) verwenden |
| UI-Umbau destabilisiert bestehende Views | Mittel | Komponenten-weise; 106 Tests + Review je Stufe |

## Validation (je Stufe)
```bash
./.venv/Scripts/python.exe -m ruff check app tests src
./.venv/Scripts/python.exe -m pytest          # Ziel: alles gruen
cd web && npm run build                        # Frontend baut
# + Live-Smoke auf :8010 + code-reviewer vor Commit
```

## Acceptance
- [ ] P6: Feed, Charts, Modals mit Printify-Mockups, Ausgaben+§19-Rechnungen, UX-Paket
- [ ] P7: URL-Import, Research, Freight Query, Preis-Check-Ampel (nach Key-Entscheidung)
- [x] P8: Trend-Recherche, KI-Edit propose-only, Nachtjob-Scheduler
- [ ] P9: nach Token-Setup — Publish live, Orders, echte Gebühren, Sales-Hub
- [ ] Alle Muster propose-only / dry-run / nie-auto-löschen eingehalten
