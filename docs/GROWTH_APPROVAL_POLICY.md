# Growth Engine — Freigabe-Richtlinie (V1)

Autor: Claude (historischer Sicherheits-Reviewer). Stand 19.08.2026.
Verbindliche Grundlage: CLAUDE.md (Eiserne Regeln) + dokumentierte Nutzer-
Entscheidungen aus docs/CLAUDE-NOTES.md. Diese Richtlinie SCHWAECHT keine
bestehende Regel ab — sie ordnet die Growth-Engine-Aktionen den Regeln zu.

## Leitprinzipien

1. **Durchsetzung im SERVER, nie in der UI.** Jeder Freigabe-Endpoint prueft
   selbst, ob die Aktion erlaubt, aktuell und konsistent ist. Ein manipulierter
   oder veralteter Frontend-Klick darf nichts Verbotenes ausloesen.
2. **Ausfuehrung NUR ueber Bestands-Pfade.** Freigegebene Aktionen rufen die
   existierenden, abgesicherten Services auf (Preis-Check-Uebernahme,
   `begin_import`/Publish-Queue, `fulfill_sale`, Optimierungs-Propose-Flow,
   Policy-VARIANTEN-Mechanik). Die Growth Engine baut KEINE parallelen
   Schreibwege zu eBay/AliExpress.
3. **Eine Freigabe = genau EINE beschriebene Aktion.** Keine Sammel-Freigaben
   mit verstecktem Umfang; „Alle uebernehmen"-Knoepfe nur, wenn jede Einzel-
   Aktion identisch klassifiziert und einzeln aufgelistet ist (Vorbild:
   „Alle sicheren Anhebungen" im Preis-Check).
4. **Audit-Pflicht.** Jede autonome UND jede freigegebene Aktion erzeugt einen
   `task_log`-Eintrag (bestehendes Muster) mit Empfehlungs-ID, Vorher/Nachher
   und Ausloeser (system/owner).
5. **Kosten-Deckel fuer autonome Recherche.** Referenzpunkt ist der bestehende
   Rahmen (~0,10–0,15 €/Nacht LLM fuer die Trend-Recherche); die Growth Engine
   bekommt ein konfigurierbares Tages-Budget und stoppt still, statt es zu
   ueberschreiten.

## Die Matrix

### ✅ AUTONOM ERLAUBT (ohne Besitzer, mit Audit)

| Aktion | Begruendung / Praezedenz |
|---|---|
| Daten-Analyse (eigene DB, read-only) | reine Leseoperationen; Praezedenz: Analytics, Growth-Snapshot (dessen Allowlist-Doktrin gilt fuer jeden Datenexport) |
| Marktplatz-Recherche (eBay-Suchen, oeffentliche Daten) | Praezedenz: `search_competitor_offers`, Trend-Recherche; Kosten-Deckel gilt |
| Lieferanten-Recherche (AliExpress-API/Store-Ernte, read-only) | Praezedenz: taegliche Trend- und 3-Store-Entdeckung; Browser-Tarnung nutzen; Suchseiten sind fuer Server dauerhaft blockiert (verifiziert 16.08.) |
| Chancen VERWERFEN (unterhalb definierter Schwellen) | Praezedenz: `prune_old_ideas` (5 Tage; Ausnahmen 🏬-Store-Ideen und Gemerktes); Verworfenes bleibt einsehbar (Pipeline-Filter „verworfen") |
| Chancen PRIORISIEREN / Pipeline-Reihenfolge | interne Ordnung ohne Aussenwirkung |
| Experiment VORSCHLAGEN | ein Vorschlag ist eine Entscheidungs-Karte — noch keine Wirkung |
| Metriken/Experimente MESSEN | read-only auf eigene Daten |
| Produkt-IDEEN anlegen (`ProductIdea`-Zeilen) | Praezedenz: naechtliche Scanner; Elektronik-, Duplikat- (beide ID-Formen!), Rating- und Lieferzeit-Filter gelten unveraendert |

### 🟡 BESITZER-FREIGABE ERFORDERLICH (Entscheidungs-Karte, je Einzelfall)

| Aktion | Ausfuehrender Bestands-Pfad | Historische Regel |
|---|---|---|
| Listing NEU erstellen (Entwurf ODER live) | `begin_import` / Publish-Queue | Import ist heute Besitzer-Klick (📝/🚀); Entwuerfe lokal ohne eBay-Call (Regel 12); listing-stil.md, Massschutz-Guard (`preserves_measurements` — NIE umgehen), `spec_filter` (kein Herkunftsland China), VeRO gelten zwingend |
| Titel/Beschreibung aendern | Optimierungs-Propose-Flow (`optimization_suggestion` → manuelle Freigabe) | automatische Vorschlags-Generierung wurde BEWUSST abgeschafft (Kosten/Kontrolle); Propose-only ist die Norm; Masse/Groessen NIE per KI aendern |
| Bilder aendern | `revise_item_pictures` nach Freigabe | sichtbare Listing-Aenderung wie Titel |
| PREIS aendern (jede Richtung) | Preis-Check-Mechanik („✓ uebernehmen") | Eiserne Regel: KEIN Auto-Repricing — eBay-Preise aendert ausschliesslich der Besitzer; Verlust-Sperre bleibt aktiv |
| Anzeigen/Werbung (Rate aendern, Kampagnen) | bestehender Ad-Rate-Pfad (heute nur Read-Sync) | direkte Geld-Wirkung |
| Versand-Policy eines Listings wechseln (z. B. Laender-Ausschluss) | `listing_country_service`-Mechanik | bestehende Policies NIE editieren — nur neu angelegte VARIANTEN, nur je freigegebener Listing-Liste |
| Lieferanten-/Quellen-Wechsel (Haupt-/Alt-Slot) | Preis-Check „🔗 Quellen" / `set_variant_alt_source` | Bestehende Ausnahme bleibt UNVERAENDERT: Auto-Failover nur bei TOTER Hauptquelle, nicht geteilten Produkten, mit Beweis-Gate — die Growth Engine erweitert diese Ausnahme nicht |
| Experiment STARTEN, das etwas an eBay veraendert | die jeweilige Aktion dieser Tabelle | Experiment = freigegebene Aenderung + automatische Messung |
| Bestands-/Mengen-Eingriffe ausserhalb der bestehenden Automatik | Monitoring-Pfade | Automatik bleibt exakt: Ausverkauft-Schutz (Menge→0) + beweispflichtige Wiederherstellung (`stock_reported`/`restock_ok`); nichts Neues autonom |

### ⛔ IN V1 NIEMALS AUTOMATISCH (auch nicht per pauschaler Vorab-Freigabe)

| Aktion | Historische Regel |
|---|---|
| EINKAUF bei AliExpress (`ds.order.create`) | Eiserne Regel 1: `auto_fulfill` bleibt `false` — Freigabe pro Bestellung, zwei bewusste Schritte; Regel: nach unklarem Ausgang NIE automatisch wiederholen (`verify_needed`) |
| Fulfillment veraendern (jenseits des bestehenden Tracking-Syncs) | Tracking-Automatik ist abschliessend definiert (45-min-Sync, destinationsbewusst, Endzusteller-Upgrade, Storno-Guard 1179) — Growth Engine fasst sie nicht an |
| Listing BEENDEN / loeschen | Eiserne Regel 7: NIE automatisch — nur ausdrueckliche Besitzer-Anweisung je konkreter Liste; selbst bei toter Quelle nur „Aktion erforderlich" markieren |
| Kaeufer-Kommunikation (Nachrichten, Storno-/Bewertungs-Antworten) | existiert bewusst nicht im System; aussenwirksam + rechtlich bindend (Regel: keine Garantie-/Servicezusagen) — in V1 komplett ausserhalb des Systems |
| Bestehende eBay-Versand-Policies EDITIEREN | Vorfall-Regel: wirken auf ALLE zugeordneten Listings — nur neue Varianten, nur nach Freigabe |
| Batch-Aenderungen ohne einzeln gelistete Objekte | Kontroll-Prinzip des Besitzers (Praezedenz endlist:/landapply: — immer explizite ID-Listen) |
| Steuer-/Rechts-Entscheidungen (Drittland CH/GB, § 19) | liegen beim Besitzer + Steuerberater (Checkliste Punkt 6, CLAUDE-NOTES) |
| Konfigurations-Aenderungen an Geld-Guards (Verlust-Sperre, Claims, Liefer-Preflight, Bestands-Beweis-Gates) | Architektur-Verbot: die Growth Engine konfiguriert keine Guards um — sie lebt UNTER ihnen |

## Durchsetzungs-Anforderungen an Codex (verbindlich)

1. Jede Empfehlung traegt SERVERseitig eine `action_class`
   (`autonomous` / `owner_approval` / `forbidden_v1`). `forbidden_v1` darf gar
   nicht erst als ausfuehrbare Empfehlung entstehen — hoechstens als
   Informations-Notiz ohne Ausfuehrungs-Endpoint.
2. Der Approve-Endpoint validiert VOR der Ausfuehrung: Empfehlung existiert,
   Status `bereit`, Datenlage unveraendert (Versions-/Hash-Check gegen die beim
   Erzeugen gesehenen Werte), `action_class == owner_approval` — und ruft dann
   ausschliesslich den registrierten Bestands-Pfad auf.
3. Kein Growth-Modul importiert eBay-/AliExpress-Clients fuer SCHREIB-
   Operationen — nur die genannten Service-Funktionen (die Review-Checkliste
   prueft das mechanisch per Grep).
4. Autonome Laeufe respektieren: Lauf-Locks (`try_acquire_run`-Muster gegen
   parallele ProductIdea-Schreiber), SQLite-Regel (NIE eine offene Schreib-
   Transaktion ueber Netz-/LLM-Calls halten), Kosten-Deckel, Scheduler-
   Konventionen (eigene Session je Job, try/finally + close, MISFIRE_GRACE,
   Einzel-Fehler stoppen den Lauf nicht).
5. Ablehnungen/Verwuerfe sind LERN-Signale, keine Loeschungen: verworfene
   Chancen bleiben abfragbar (analog `cleanup_dismissed_at`/`opt_dismissed_at`
   — der Besitzer entschied bewusst, Verworfenes nachvollziehbar zu halten).
