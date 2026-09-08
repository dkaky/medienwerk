# FINALE ABNAHME — GROWTH ENGINE V1 (Review #1)

Stand: 20.08.2026 · Reviewer: Claude · Gegenstand: Growth Engine V1 auf `codex-growth`
(uncommitteter Stand: growth_engine_service.py, routers/growth.py, growth_research.py,
models.py-/scheduler.py-/main.py-/index.html-Diffs, test_growth_engine.py, PRODUCT.md)

Grundlage: vollständige Erstlektüre aller neun Implementierungs-Artefakte, fünf
Faktenchecks gegen den Bestand, lokaler Testlauf (10/10 Growth-Tests, volle Suite
1224/1224 grün auf dem Branch) und eine adversarische 4-Linsen-Prüfung mit 24
Agenten, deren Funde einzeln verifiziert wurden (19 bestätigt, 1 widerlegt).
Nichts wurde committet, gepusht, gemergt oder deployt.

## 1. FINAL VERDICT: **CHANGES REQUIRED**

Nicht BLOCKED: Die Architektur ist tragfähig, es existiert **kein einziger Pfad von
APPROVE zu einer echten Geld-/Marktplatz-Aktion** (adversarial bestätigt), und die
Kern-Ökonomie hat die historischen Lektionen verinnerlicht. Aber zwei HIGH-Fehler
würden die Shadow-Daten sofort verfälschen bzw. Owner-Zustände nächtlich zerstören,
der Kill-Switch fehlt, und die UI widerspricht dem Besitzer-Entscheid.

## 2. BLOCKING FINDINGS (vor dem Shadow-Run zwingend)

**B1 — HIGH: Nächtliche Detection überschreibt Owner- und Research-Zustände.**
`growth_engine_service.py`, `_upsert_opportunity` (Z. 322–335) vs. Regel-Blöcke
(Z. 441/495/559). Geschützt sind nur terminale Status (REJECTED/SCALED/CLOSED) —
**READY_FOR_REVIEW, APPROVED und EXPERIMENT_RUNNING werden jede Nacht auf
VERIFYING/ANALYZING zurückgesetzt**, verifizierte Research-Evidenz
(`evidence.research`, `CONFIRMED_QUOTE`-Provenienz, leere `missing_evidence`) wird
gelöscht; Transitionen laufen an `ALLOWED_TRANSITIONS` vorbei (per Runtime-Repro
bestätigt). *Szenario:* Besitzer genehmigt abends eine Karte; um 06:10 ist die
Freigabe-Grundlage weg und die Chance wieder „in Prüfung".
*Kleinster Fix:* In `_upsert_opportunity` zusätzlich alle Status ab
READY_FOR_REVIEW schützen (nur `last_evaluated_at` + Frisch-Fakten unter separatem
Evidenz-Schlüssel aktualisieren; `status/decision_state/expected_*/
economic_provenance/missing_evidence/rejection_reason` unangetastet lassen) +
Regressionstest „Detection ändert APPROVED/READY nie".

**B2 — HIGH: Stornierte/erstattete Verkäufe NACH Einkauf zählen als bestätigter
Deckungsbeitrag.** Alle vier Ökonomie-Pfade (Scorecard Z. 164, Listing-Ökonomie
Z. 346, Early-Signal Z. 938, Experimente Z. 1093) filtern nur `Sale.status` —
**`ebay_cancel_state` wird nirgends geprüft**. Unser Bestand lässt bei Storno nach
Einkauf den Status bewusst auf delivered/tracking (models.py-Kommentar Z. 261).
*Szenario:* Ein rückabgewickelter 25-€-Verkauf steht mit +10 € „CONFIRMED"-Beitrag
in Scorecard, Winner-Auswahl und Experiment-WON.
*Kleinster Fix:* In allen vier Pfaden `ebay_cancel_state == 'CANCELED'`
ausschließen und die Restlücke (Voll-Erstattung nach Einkauf ohne Markierung) in
`provenance` als bekannte Grenze dokumentieren + Test.

**B3 — HIGH: Kein Feature-Flag.** Kein `growth_engine_enabled` in config — Jobs,
Router und UI-Tab aktivieren sich mit dem Deploy; Checkliste §9 verlangt
vollständige Abschaltbarkeit, die UX-Spec die „nicht aktiv"-Karte.
*Kleinster Fix:* Config-Flag (Default **aus**), Scheduler-Registrierung +
Router-Verhalten + Tab-Sichtbarkeit dahinter; Shadow-Run = Flag gezielt an.

**B4 — HIGH: UI verstößt gegen den Besitzer-Entscheid.** Neuer eigener Tab
„Growth Center" statt Integration oben im Optimierung-Tab; nur `index.html`,
`index_classic.html` (das Design des Besitzers!) fehlt; **PRODUCT.md Z. 36
kodifiziert sogar wörtlich das Gegenteil der Beide-Designs-Regel**; Owner-Texte
teils englisch (`recommended_action`, `missing_evidence`, API-Meldungen).
*Kleinster Fix für Shadow:* Tab hinter B3-Flag verbergen und die PRODUCT.md-Zeile
korrigieren; vollständige Integration (Optimierung-Tab, beide Designs, durchgängig
Deutsch/ELI5) ist Pflicht vor dem Owner-Release.

**B5 — MEDIUM: Margen-Einheiten-Falle am Research-Gate.**
`apply_verified_research` prüft `expected_margin_pct` nur gegen `0.20`, validiert
aber den Wertebereich nicht — ein Adapter, der **30 statt 0.30** liefert, hebelt
das 20-%-Pflicht-Gate aus (Feldname endet auf `_pct`, `growth_research.py`
dokumentiert die Einheit nicht). *Kleinster Fix:* Range-Guard (0 < margin ≤ 1,
sonst ValueError) + Einheiten-Docstring + Test.

## 3. NON-BLOCKING FINDINGS

- **MEDIUM (vor dem ersten ECHTEN Experiment, nicht vor Shadow):**
  `evaluate_experiments` wertet `failure_threshold`/`rollback_rule` nie aus (kein
  Stopp-Pfad), ignoriert CONTROL-Kohorten (WON rein aus absoluten
  Treatment-Schwellen, Default `min_positive_listings=1` → **ein** Verkauf kann
  WON auslösen), und der Verkaufs-Filter hat kein Fenster-**Ende** (Verkäufe nach
  Ablauf zählen bis zum Bewertungslauf mit).
- **MEDIUM (vor Merge):** Governance-Lücken — kein Datenlage-Hash beim Approve
  (durch B1-Fix entschärft, laut Policy trotzdem gefordert),
  `GrowthApproval.expires_at` ungenutzt, **kein task_log-Audit** für
  Growth-Läufe/Freigaben (Policy-Leitprinzip 4), kein Pruning für
  `growth_scorecards` (1 Zeile/Tag) und `growth_listing_metrics`
  (aktive Listings × Tage).
- **MEDIUM (mit Auflage tolerierbar):** `early_winner_signal` subtrahiert
  **rollierende** Fensterwerte (impressions_week/views_30d) wie kumulative
  Zähler — die Deltas und Score-Schwellen sind bis zum Aufbau echter
  Tages-Historie semantisch falsch; zudem Quellen-Mix clicks_week↔views_30d.
  Auflage: Signal bis ≥14 echte Tages-Snapshots als `data_status="NOT_KNOWN"`
  ausweisen (die eigene DATA_QUALITY-Karte benennt das Problem bereits — die
  Ausgabe tut es noch nicht).
- **MEDIUM:** `record_opportunity_decision` erlaubt REJECT aus **jedem** Status
  (auch EXPERIMENT_RUNNING, ohne das Experiment zu berühren) — auf
  nicht-laufende, nicht-terminale Zustände begrenzen.
- **LOW:** Winner-Konzentration = Top-20-Beitragsanteil statt Spec-Definition
  (Top 5, Umsatz) — Definition ist sauber in `provenance` dokumentiert, aber
  UI-Text muss dieselbe Sprache sprechen. `reevaluate_opportunities` schreibt
  jede Zeile jeden Lauf (updated_at-Rauschen). Branch-Basis liegt hinter `main`
  — Merge braucht Rebase + erneute volle Suite (models.py/scheduler.py werden
  von beiden Strängen berührt).

## 4. CONFIRMED SAFEGUARDS (aktiv gegengeprüft)

- **Kein Ausführungspfad:** Keine eBay-/AliExpress-/LLM-/golive-/order_service-
  Schreibimporte im Growth-Code; jeder Approve-/Decision-Endpoint ist record-only
  mit `execution_authorized: False`; der einzige Bypass-Verdacht (freie
  `action_type`-Wahl) wurde als folgenlos **widerlegt** — es existiert schlicht
  kein Executor. Experiment-Start braucht explizite Freigabe und markiert nur den
  Messbeginn.
- **Ökonomie-Fundament korrekt:** Zeilensummen-Semantik ohne ×quantity (Test mit
  quantity=3), `cost_cny`-als-EUR, strikte Trennung estimated/confirmed,
  unknown ≠ zero, 20 %/5 €-Gates mit Auto-Reject, READY nur bei
  CONFIRMED-Provenienz + leerer Evidenz-Lücke, Null-READY als gültiger Zustand
  (getestet).
- **Betrieb:** Scheduler-Jobs im Bestandsmuster (eigene Session, try/finally,
  keine externen Calls, freies 06:00-Fenster); Schema additiv mit sauberen
  Indizes/Uniques (create_all-Test); Auth-Middleware deckt die neuen Endpoints;
  keine PII/Secrets in Tabellen, Logs oder UI; Tests decken die Kernrisiken
  echt ab.

## 5. EXACT NEXT STEP

**Findings-Paket an Codex zur Nachbesserung** — Reihenfolge: B1 → B2 → B3 → B5 →
B4-Minimal (Flag versteckt Tab + PRODUCT.md-Korrektur), jeweils mit
Regressionstest nach Vorfalls-Muster; danach Re-Review nur auf diese Fixes (wie
beim Snapshot-Exporter: dort hat genau dieser Zyklus in einer Runde zu APPROVED
geführt). Erst nach Re-Review: Rebase auf `main`, volle Suite, Merge-Go des
Besitzers, Deploy mit **Flag aus**, dann kontrollierter Shadow-Run mit Flag an.
Die Experiment-/Governance-Punkte (Abschnitt 3) sind Auflagen vor dem ersten
echten Experiment bzw. vor dem Owner-Release, nicht vor dem Shadow-Run.
