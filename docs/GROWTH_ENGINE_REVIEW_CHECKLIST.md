# Growth Engine V1 — Review-Checkliste (Claude-Abnahme vor Merge)

Autor: Claude. Stand 19.08.2026. Anwendung: Sobald Codex „V1 fertig" meldet,
wird JEDER Punkt geprueft (Code-Lektuere + lokale Laeufe in der Mock-Umgebung +
adversarische Mehr-Linsen-Review wie bei Snapshot/Liefer-Check). Merge auf
`main` (= Live-Deploy!) erst bei bestandener Liste und gruener Gesamt-Suite.

## 1. Datenbank-Migrationen

- [ ] NUR additive Spalten/Tabellen via `app/models.py` (kein Alembic, keine
      Renames/Drops, nur konstante Defaults) — kompatibel mit
      `_sqlite_add_missing_columns`.
- [ ] Neue Tabellen haben TimestampMixin + sinnvolle Indizes; JSON via
      `JSONType`.
- [ ] Start gegen eine ALTE DB-Kopie getestet (Spalten fehlen → Auto-ALTER
      ergaenzt; App startet ohne Handarbeit).
- [ ] Aufbewahrung/Pruning fuer wachsende Tabellen definiert und getestet
      (inkl. Schutz-Ausnahmen analog prune_old_ideas).

## 2. Opportunity-Lifecycle

- [ ] Zustandsmaschine explizit (entdeckt → analysiert → verifiziert → bereit →
      freigegeben/abgelehnt/verworfen/veraltet) — keine undokumentierten
      Seitenwege; jede Transition mit task_log.
- [ ] „bereit" hat eine Confidence-Schwelle; darunter erscheint nichts als
      Entscheidung (UX-Vertrag: max. 5 Karten).
- [ ] Veralterung: Datenlage-Hash/Version an der Empfehlung; Aenderung ⇒
      Status `veraltet`, Ausfuehrung unmoeglich.
- [ ] Verworfen/abgelehnt bleibt abfragbar (kein Hard-Delete); Ablehnungsgrund
      wird gespeichert.
- [ ] Kein Lifecycle-Schritt haelt eine offene Schreib-Transaktion ueber
      Netz-/LLM-Calls (Code-Lektuere je Schritt).

## 3. Oekonomie-Korrektheit

- [ ] Umsatz = Summe `Sale.price_eur` (Zeilensumme! KEIN ×quantity), ohne
      cancelled/refunded — per Test gegen `analytics_service` abgeglichen.
- [ ] `cost_cny`-als-EUR korrekt; EK nur mit `cost_source`-Ausweis; Gebuehren
      aus `fee_eur_actual` (Abdeckung ausgewiesen).
- [ ] Erwarteter Wert je Empfehlung: Rechenweg gespeichert + im Drawer
      reproduzierbar; Spannen statt Scheinpraezision; „geschaetzt" markiert.
- [ ] Preis-Empfehlungen respektieren `pricing.py` (Marge/Boeden) und die
      Verlust-Sperre; keine zweite Preisformel.
- [ ] Statuslisten vollstaendig (self_shipped, verify_needed, …) — Abgleich
      gegen models.py-Kommentare und order_service.

## 4. Metrik-Provenienz

- [ ] Jede Business-Health-Kennzahl dokumentiert Quelle (Tabellen/Felder,
      Ausschluesse) maschinenlesbar; die UI-sub-Zeile speist sich daraus.
- [ ] Kennzahlen kommen aus bestehenden Service-Funktionen ODER ein Test
      vergleicht Growth-Werte gegen die Referenz (analytics/finance) auf
      Identitaet.
- [ ] Unvollstaendige Datenbasis wird als solche ausgegeben (nie stillschweigend
      hochgerechnet).

## 5. Freigabe-Durchsetzung (Policy-Enforcement)

- [ ] `action_class` serverseitig an jeder Empfehlung; `forbidden_v1` ohne
      Ausfuehrungs-Endpoint.
- [ ] Approve-Endpoint validiert Existenz + Status + Aktualitaets-Hash +
      action_class VOR Ausfuehrung; Tests fuer alle Verweigerungs-Faelle.
- [ ] Grep-Beweis: Growth-Module importieren KEINE eBay-/AliExpress-Clients
      fuer Schreiboperationen; Ausfuehrung nur ueber die registrierten
      Bestands-Services (Liste in GROWTH_APPROVAL_POLICY.md).
- [ ] Verbotene Aktionen (Einkauf, Listing-Beenden, Policy-Edit,
      Kaeufer-Kommunikation, Batch ohne Einzelliste) sind NIRGENDS ausfuehrbar
      — auch nicht ueber Umwege (z. B. „Experiment-Stopp", Rollbacks).
- [ ] Rollback/Stopp eines Experiments nutzt denselben Bestands-Pfad wie die
      Aenderung und ist selbst approval-frei NUR, wenn er exakt den vorherigen
      Zustand wiederherstellt.

## 6. Scheduler-Sicherheit

- [ ] Eigene Session je Job, try/finally + close; Einzel-Fehler stoppen den
      Lauf nicht; Ergebnis als task_log.
- [ ] Lauf-Lock gegen Doppel-Laeufe; Vertraeglichkeit mit try_acquire_run
      (keine parallelen ProductIdea-Schreiber).
- [ ] Zeitfenster kollidieren nicht mit 03:00–05:30-Belegung; Rate-Limits
      (Sleep) gegen AliExpress/eBay.
- [ ] Kosten-Deckel implementiert: Budget-Feld in config, stiller Stopp,
      Zaehler im task_log.
- [ ] Kein Growth-Job schreibt sale.status/Listings/Policies direkt.

## 7. Experiment-Lifecycle

- [ ] Start NUR aus freigegebener Empfehlung (bei eBay-Wirkung);
      Beobachtungs-Experimente ohne Wirkung klar getrennt.
- [ ] Vorher-Snapshot (Muster opt_snapshot) + definierte Mindestlaufzeit +
      Endkriterium; Ausgang gewonnen/verloren/unklar — „unklar" ist ein
      ehrlicher Endzustand.
- [ ] Stopp-Funktion getestet (Ruecksetzung ueber Bestands-Pfad); kein
      Experiment kann „haengen" (Neustart-Recovery wie Publish-Queue).
- [ ] Lehrsaetze („Gelernt") entstehen nur aus abgeschlossenen Experimenten,
      mit Quelle-Link.

## 8. UI-Klarheit

- [ ] BEIDE Designs (index.html + index_classic.html), classic zuerst geprueft;
      Tab-/Badge-Benennung vom Besitzer freigegeben.
- [ ] Karten beantworten alle Pflichtfragen (Was/Warum/Wert/Sicherheit/Risiko/
      Unbekannt/Was-passiert); Details nur im Drawer; max. 5 Karten;
      Null-Zustand positiv.
- [ ] Alle Texte deutsch/ELI5 mit Selbsthilfe-Weg; `esc()` ueberall; Toasts
      6,2 s + persistente Kartenfehler; UTF-8-Literale (0x15-Falle).
- [ ] Zwei-Schritte-Bestaetigung bei Geld-/Live-Wirkung (Karte → Drawer →
      Ausfuehren) real implementiert.
- [ ] Kein Innen-Scroll in Tabellen (stick-scroll-Entscheid); mobile Ansicht
      funktioniert (Wajjahats Mobile-Anpassung nicht brechen).

## 9. Produktions-Schutz

- [ ] Feature-Flag in config (Growth komplett abschaltbar; UI zeigt dann die
      eine „nicht aktiv"-Karte).
- [ ] Dev-Kopie kann nie live wirken: Schreib-Aktionen hinter
      monitor_push_real-artigen Gates wie im Bestand.
- [ ] /health + Deletion-Endpoint unangetastet; Auth-Middleware deckt neue
      Endpoints ab (ausser den zwei Pflicht-oeffentlichen).
- [ ] Diagnose-Modi fuer Growth (read-only Zustand/letzter Lauf) vorhanden —
      Betrieb ohne VPS-SSH bleibt moeglich.

## 10. Regressions-Risiko

- [ ] Diff-Durchsicht: keine Aenderungen an fulfill_sale-Guards, Monitoring-
      Doktrin, Tracking-Semantik, Policy-Mechanik, pricing.py — ausser explizit
      begruendet und einzeln reviewt.
- [ ] Gesamt-Suite gruen (alle bestehenden ~1250+ Tests) auf dem Merge-Stand;
      keine Test-Abschwaechungen ohne Begruendung.
- [ ] Rebase-/Merge-Konflikte mit Wajjahats parallelem Buchhaltungs-Strang
      geprueft (CLAUDE-NOTES: beide Bloecke behalten).

## 11. PII / Secrets

- [ ] Growth-Tabellen speichern keine Kaeuferdaten (Namen/Adressen/Mails/
      Telefon) — Feld-Audit wie beim Snapshot (KNOWN/SENSITIVE-Denke).
- [ ] Exporte/Reports folgen der Snapshot-Allowlist-Doktrin; Fehlermeldungen/
      Logs wertfrei.
- [ ] Keine Secrets im Code/Repo; neue config-Felder in .env.example
      dokumentiert; LLM-Prompts enthalten keine Kaeuferdaten.

## 12. Kompatibilitaet mit bestehenden Ablaeufen

- [ ] Naechtliche Scanner, Preis-Check, Publish-Queue, Beleg-Kette, Bank-Sync
      laufen unveraendert (Smoke ueber die Suite + gezielte Tests).
- [ ] Growth nutzt dieselben Ideen-/Listing-Objekte statt Parallelwelten
      (eine ProductIdea bleibt EINE ProductIdea).
- [ ] Diagnose-Workflow, Deploy-Workflow, Growth-Snapshot-Export unveraendert
      funktionsfaehig.

## 13. Test-Qualitaet

- [ ] Hermetisch (conftest-Mock-Zwang, PID-isolierte DB); keine Netz-Calls.
- [ ] Jede Policy-Verweigerung, jeder Lifecycle-Uebergang, Veralterung,
      Kosten-Deckel, Feature-Flag: eigener Test.
- [ ] Oekonomie-Abgleichstests gegen analytics/pricing (Punkt 3/4).
- [ ] Negativ-Tests nach Vorfalls-Muster (degradierte API-Antworten sind
      „unbekannt", nicht „0"/„nein").
- [ ] Kein Test generiert sein Schema aus dem Pruefling selbst
      (Selbstreferenz-Falle aus dem Snapshot-Review M4) — Schema-Vertraege
      gegen app.models bauen.

## Abnahme-Protokoll

Ergebnisformat wie etabliert: VERDICT (APPROVED / APPROVED WITH MINOR CHANGES /
CHANGES REQUIRED / UNSAFE) + Findings nach Schwere + Minimal-Aenderungen vor
Merge + explizite Antworten: mergen? deployen? Feature-Flag-Zustand beim ersten
Deploy? Erst-Aktivierung mit welchem Budget?
