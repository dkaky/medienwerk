# Growth Engine — Historischer Constraint-Audit (fuer Codex)

Autor: Claude. Stand 19.08.2026. Zweck: Codex baut Growth Engine V1 ohne die
volle Projekt-Historie — dieses Dokument liefert die Schutzmechanismen, Fallen,
Wiederverwendungs-Kandidaten und Lehren, die das Design beeinflussen MUESSEN.
Pflichtlektuere dazu: CLAUDE.md, docs/CLAUDE-NOTES.md, docs/HANDOFF-CODEX.md,
docs/GROWTH_APPROVAL_POLICY.md.

## A. Schutzmechanismen, die Codex ERHALTEN muss (nie umbauen/umgehen)

1. **Geld-Guard-Kaskade in `fulfill_sale`** (order_service): Doppelbestell-
   Claims (`_claim_order_slot`, `verify_needed` nach unklarem Ausgang),
   Verlust-Sperre, Storno-Live-Check + TOCTOU-Re-Read, Blindkauf-Sperren,
   Bestands-Preflight, Liefer-Preflight (destinationsbewusst, fail-open).
   Growth-Code ruft diesen Pfad hoechstens AUF (nach Freigabe), aendert ihn nie.
2. **Ausverkauft-Schutz + Beweis-Doktrin** (monitoring/parse_product):
   fehlende Daten sind NIE „Bestand 0"; Mengen ERHOEHEN nur mit positivem
   `stock_reported`-Beweis; Failover nur mit Beweis-Gate. Vorfall 15.08.:
   zwei lebende Listings wurden genullt, weil eine degradierte API-Antwort wie
   „0" behandelt wurde — diese Doktrin gilt sinngemaess fuer JEDE externe
   Datenquelle der Growth Engine (Ergebnisse aus degradierten Antworten sind
   „unbekannt", nie ein Fakt).
3. **Massschutz `preserves_measurements`** (llm.py): KI darf NIE Groessen/Masse
   veraendern (Pokemon-Poster-Vorfall). Jede Growth-Titel-/Text-Generierung
   MUSS durch diesen Guard.
4. **Storno-Unantastbarkeit** (Vorfall Sale 1179): kein Automatismus schreibt
   je ueber `cancelled`/`refunded` — jeder neue Auto-Pfad, der `sale.status`
   anfasst, braucht diese Skip-Liste.
5. **Policy-Unantastbarkeit**: bestehende eBay-Versand-Policies werden nie
   editiert (wirken auf ALLE Listings) — nur neue Varianten je Ausschluss-
   Muster (`listing_country_service` als Vorbild inkl. Varianten-Cache).
6. **Tracking-Semantik**: DE nur finale DHL-/Hermes-Nummern; Ausland beste
   verfuegbare Nummer + Endzusteller-Upgrade; Nummern nie „verschlechtern".
7. **§ 19-/Finanz-Ehrlichkeit**: Umsatz ohne Storno/Erstattung; echte
   Gebuehren (`fee_eur_actual`) vor Schaetzungen; EK-Herkunft (`cost_source`)
   ausweisen; NIE geschaetzte Zahlen als Fakten praesentieren.
8. **Oeffentliche Pflicht-Endpoints**: `/health` und
   `/ebay/marketplace-account-deletion` bleiben ohne Auth (sonst deaktiviert
   eBay das Production-Keyset).

## B. Architektur-Fallen (hier sind wir schon einmal hineingefallen)

1. **`Sale.price_eur` ist die ZEILENSUMME inkl. Versandanteil** (eBay
   `li.total`); `quantity` steht separat und wird NICHT multipliziert. Codex'
   Growth-Snapshot hatte genau diesen Fehler (Review-H1, 18.08.). Jede Umsatz-/
   Margen-Rechnung der Growth Engine uebernimmt die App-Semantik —
   `analytics_service` ist die Referenz.
2. **`cost_cny` enthaelt EUR** (historischer Feldname; Kurs-Faktor 1.0).
3. **Status-Vokabular vollstaendig verwenden**: `self_shipped` (Eigenbestand!)
   und `verify_needed` (unklarer Einkauf) existieren wirklich — Allowlists,
   die sie vergessen, verwaschen genau die interessanten Kategorien
   (Snapshot-Review M1).
4. **SQLite-Disziplin**: WAL-Modus, trotzdem gilt die Eiserne Regel — nie eine
   offene SCHREIB-Transaktion ueber Netz-/LLM-Calls halten (erst rechnen, DANN
   kurz schreiben). Locks sind PROZESS-lokal: Zweitprozesse (Diagnose-Skripte)
   koordinieren nicht mit dem Server — lange Growth-Batches gehoeren in den
   Server-Scheduler, nicht in Zweitprozesse.
5. **Migrationen sind ADDITIV via ORM**: Spalten entstehen aus `app/models.py`
   + `_sqlite_add_missing_columns` beim Start. Kein Alembic. Keine Renames,
   keine Drops, keine berechneten Defaults. Die Live-DB kann verwaiste
   Alt-Spalten tragen — neuer Code muss damit leben.
6. **Scheduler-Konventionen**: eigene `SessionLocal()` je Job, try/finally +
   close, Einzel-Fehler brechen den Lauf nicht, `MISFIRE_GRACE_SECONDS`
   (Windows-Schlaf-Vorfall), Lauf-Locks gegen Doppel-Laeufe
   (`try_acquire_run`), Rate-Limit-Sleeps gegen AliExpress (429-Kaskade).
7. **Parallel-Entwicklung auf `main` + Auto-Deploy**: jeder main-Push geht
   nach dem Test-Gate LIVE; non-fast-forward ist Alltag (drei Akteure).
   Growth-Arbeit bleibt auf dem Branch bis zum kontrollierten Merge; die
   Suite muss vor jedem Push gruen sein.
8. **Zwei UI-Monolithe**: index.html + index_classic.html doppelzupflegen; der
   Besitzer nutzt CLASSIC. Encoding-Falle ist real (0x15-Steuerzeichen-Vorfall).
9. **AliExpress-API-Formvarianz**: Feldnamen/Formen wechseln (drei Freight-
   Antwortformen, fehlende Bestandsfelder). Parser defensiv; Kalibrier-Modi im
   Diagnose-Kanal vorsehen (`shipto:`-Muster).
10. **Suchseiten-Harvest ist server-blockiert** (dauerhaft, live verifiziert):
    Marktplatz-Recherche via API + STORE-Seiten (mit Browser-Tarnung), nicht
    via /w/wholesale-Seiten.

## C. Vorhandene Faehigkeiten — WIEDERVERWENDEN statt neu bauen

| Bedarf der Growth Engine | Existiert bereits |
|---|---|
| Preis-/Margen-Kalkulation | `pricing.py` — EINE Formel (20 % Marge / min 4 €), ueberall gleich |
| Echte Gebuehren je Verkauf | `finance_service.sync_ebay_fees` + `fee_eur_actual` (ganzes Jahr nachgezogen) |
| Umsatz-/Gewinn-Semantik | `analytics_service` (Referenz-Implementierung) |
| Wettbewerbs-Preise | `search_competitor_offers` (ebay.py) |
| Produkt-/Nischen-Sourcing | `product_research_service` (discover, Trends, 3-Store-Entdeckung, ID-Import, Filterwerk inkl. beider ID-Formen) |
| Lieferanten-Alternativen | `supplier_service` (Slots, `variant_alt_availability`, Failover mit Gates) |
| Lieferbarkeit je Land | `delivery_check_service` + `delivery_availability` |
| Laender-Ausschluss je Listing | `listing_country_service` (Policy-Varianten + Cache + apply je Freigabe-Liste) |
| Titel-Vorschlaege mit Freigabe | `optimization_service` + `optimization_suggestion` (Propose-only!) + Massschutz |
| Klick-/Impression-Daten | `refresh_click_data`, `sync_listing_stats` (Quota-schonend) |
| Publish/Entwuerfe | `begin_import`, Publish-Queue (Neustart-fest) |
| Audit-Trail | `task_log`-Kontextmanager (ueberall im Einsatz) |
| Sanitisierte Datenexporte | `scripts/export_growth_snapshot.py` (Allowlist-Doktrin, approved 18.08.) |
| Betriebs-Diagnose | Diagnose-Kanal (diagnose.yml + Modi) — fuer Growth eigene READ-ONLY-Modi ergaenzen statt neuer Kanaele |
| Ladenhueter-/Aufraeum-Logik | cleanup-Buckets + `cleanup_dismissed_at`/`opt_dismissed_at` (Besitzer-Entscheid: nachvollziehbares Verwerfen, nie Auto-Beenden) |

## D. Lehren, die das Growth-Design praegen sollten

1. **Propose-only hat sich bewaehrt** — der Besitzer hat Auto-Vorschlaege
   (Kosten) und Auto-Repricing (Kontrolle) explizit ABGESCHAFFT. Die Growth
   Engine gewinnt Vertrauen durch Qualitaet weniger Empfehlungen, nicht durch
   Volumen (UX: max. 5 Karten).
2. **Jeder Vorfall wird ein Test** — etablierte Praxis. Growth-Fehler ebenso.
3. **Fail-open vs. fail-closed je Richtung bewusst waehlen** (Bestell-Preflight
   fail-open; Listing-Laender fail-closed) — fuer jede Growth-Entscheidung
   dokumentieren, welche Richtung die sichere ist.
4. **Klartext gewinnt**: der Besitzer handelt selbststaendig, wenn Meldungen
   ELI5 sind und den Selbsthilfe-Weg nennen. Empfehlungs-Karten muessen diesem
   Standard genuegen, sonst bleiben sie liegen.
5. **Kosten sichtbar machen**: der Besitzer fragt aktiv nach Betriebskosten;
   Tages-Budget mit stiller Drosselung passt zu seinen Erwartungen
   (Referenz: ~0,10–0,15 €/Nacht Trend-Recherche).
6. **Elektronik/VeRO/Listing-Stil sind Produkt-Politik, keine Optionen**:
   Elektronik-Filter waechst mit echten Durchrutschern (Klebepistole/LED-Lampe/
   Drehmaschine); HTC/BTS sind als zertifiziert geklaert, andere Marken nicht;
   Titel 72–80 Zeichen, Pflicht-Footer, keine Garantie-Versprechen.

## E. Operative/finanzielle Risiken des Growth-Konzepts (offen benannt)

1. **Empfehlungs-Ausfuehrung ist DER Risikopunkt.** Ein Approve, das einen
   neuen Schreibweg nutzt statt der Bestands-Pfade, umgeht die gesamte
   Guard-Historie. (Policy-Anforderung 3: mechanisch per Grep pruefbar.)
2. **Metrik-Drift**: eigene Growth-Umsatzrechnungen erzeugen zwei Wahrheiten
   (Dashboard vs. Growth) und zerstoeren Vertrauen. Kennzahlen aus denselben
   Service-Funktionen beziehen ODER per Test gegen sie abgleichen.
3. **Experiment-Messbarkeit bei kleinen Zahlen**: bei wenigen Verkaeufen/Tag
   sind A/B-Signale wochenlang verrauscht. V1 konservativ: Vorher/Nachher mit
   Mindestlaufzeit und ehrlichem „unklar"-Ausgang — keine vorgetaeuschte
   Signifikanz (Finanz-Ehrlichkeits-Regel).
4. **Scheduler-Last**: das Nachtfenster 03:00–05:30 UTC ist voll belegt
   (Bank, Kategorien, Trends, Stores, Prune). Growth-Jobs in freie Fenster;
   API-Rate-Limits respektieren.
5. **DB-Wachstum**: Opportunity-/Experiment-/Metrik-Tabellen brauchen von
   Anfang an eine Aufbewahrungs-/Pruning-Strategie (Vorbild `prune_old_ideas`
   mit Schutz-Ausnahmen).
6. **PII-Disziplin**: Recherche-Artefakte (Wettbewerber-Listings ok) nie mit
   Kaeuferdaten mischen; fuer jeden Export gilt die Snapshot-Allowlist-Doktrin.
