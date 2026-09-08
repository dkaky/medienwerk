# Bauplan — Studio-Trakt, Etappe 1

**Zielrepo: dieses Projekt.** Alle untersuchten Berührungspunkte liegen hier; `app\studio\` entsteht hier.

**Leitidee:** Etappe 1 baut kein Studio, sondern den *Rahmen mit Riegel*. Der Beweis „am Handel läuft nichts anders" gelingt nur, wenn (a) das `listings`-Schema byte-identisch bleibt, (b) keine neue SQL-Bedingung entsteht, solange der Schalter aus ist, und (c) kein neuer Scheduler-Job existiert. Vorbild in jedem Punkt: die Growth Engine (`growth_engine_enabled`, `app\routers\growth.py:18`, `app\scheduler.py:635`).

---

## 1. Tabellen (genau zwei)

Beide über `Base` aus `app\database.py`, damit `init_db()` sie mit `create_all` anlegt — **immer**, auch bei ausgeschaltetem Schalter (Growth-Präzedenz, `tests\unit\test_growth_engine.py:97`). Leere Tabellen ändern am Handel nichts, ersparen aber ein Schema-Ereignis beim ersten Einschalten auf dem laufenden VPS.

### `studio_designs`
| Spalte | Typ | Wozu |
|---|---|---|
| `id` | int PK | |
| `title` | String(255), not null | Arbeitstitel des Motivs |
| `status` | String(20), default `"draft"` | `draft` / `ready` / `archived` — Etappe 1 kennt nur `draft` |
| `source` | String(30), nullable | Herkunft der Grafik (`gpt-image-1`, `flux`, `upload`) — reine Notiz |
| `image_url` | String(1024), nullable | Vorschau |
| `meta` | JSONType, nullable | Prompt, Seed, Maße — schemafrei, damit Etappe 2 nichts migrieren muss |
| `created_at`/`updated_at` | via `TimestampMixin` | |

### `studio_listing_links` — die tragende Tabelle
| Spalte | Typ | Wozu |
|---|---|---|
| `listing_id` | int PK, FK `listings.id` | **Definiert `is_studio()`.** PK = ein Listing gehört höchstens einem Design |
| `design_id` | int, FK `studio_designs.id`, nullable | Zuordnung |
| `linked_at` | DateTime(tz) | |
| `note` | Text, nullable | Warum von Hand verknüpft |

**Warum nicht eine Tabelle:** ein Design existiert, bevor ein Listing existiert. **Warum keine dritte:** Jobs, Preis-Regeln, Publish-Warteschlange kommen in Etappe 2.

**Warum keine Spalte `listings.studio_id`:** Eine zusätzliche Spalte wäre in der Abfrage billiger, macht aber `ALTER TABLE` auf der produktiven SQLite/WAL nötig und verändert das Schema der Handelstabelle — genau das, was hier unbeweisbar würde. Die Verknüpfungstabelle wird stattdessen **einmal pro Vorgang** als ID-Menge geladen, nicht pro Zeile abgefragt.

---

## 2. Neue Dateien

| Pfad | Zweck |
|---|---|
| `app\studio\__init__.py` | Paket; re-exportiert `is_studio`, `studio_enabled`, `exclude_studio` |
| `app\studio\models.py` | `StudioDesign`, `StudioListingLink` (nutzen `Base` aus `app.database`) |
| `app\studio\guard.py` | **Kernstück** — `studio_enabled()`, `is_studio(listing)`, `studio_listing_ids(db=None)`, `exclude_studio(stmt, db=None)`, `invalidate()`, `require_studio_enabled()` (FastAPI-Dependency, 404) |
| `app\studio\service.py` | `list_designs`, `create_design`, `link_listing`, `unlink_listing` — jede Schreibfunktion ruft `guard.invalidate()`. Propose-only, kein eBay-Kontakt |
| `app\studio\router.py` | `APIRouter(prefix="/api/v1/studio", dependencies=[Depends(require_studio_enabled)])` — bei ausgeschaltetem Schalter liefert jeder Pfad 404 |
| `app\studio\schemas.py` | Pydantic Ein-/Ausgabe |
| `tests\unit\test_studio_killswitch.py` | Schalter-Beweise (Abschnitt 5, Nr. 1–5) |
| `tests\unit\test_studio_guard.py` | `is_studio`-Verhalten an/aus |
| `tests\unit\test_handel_unveraendert.py` | Fingerabdruck-Vergleich gegen Grundlinie |
| `tests\data\handel_baseline.json` | **vor** der Verdrahtung erzeugte Grundlinie, mit eingecheckt |

### `guard.py` — das entscheidende Verhalten

```python
def is_studio(listing) -> bool:
    if listing is None or not get_settings().studio_enabled:
        return False                      # kein Import der Studio-Modelle, kein DB-Zugriff
    return getattr(listing, "id", None) in studio_listing_ids()

def exclude_studio(stmt, db=None):
    ids = studio_listing_ids(db)
    return stmt if not ids else stmt.where(Listing.id.notin_(ids))
```

`studio_listing_ids()` cacht prozessweit (ein Prozess: FastAPI + APScheduler + Publish-Worker), TTL 60 s, plus ausdrückliches `invalidate()` bei jedem Schreibvorgang. Bei ausgeschaltetem Schalter: sofortiges `frozenset()`, ohne Abfrage.

Der Doppelriegel — Schalter aus ⇒ `is_studio` immer `False` **und** `exclude_studio` gibt das Statement unverändert zurück — ist die Grundlage der Beweise in Abschnitt 5.

---

## 3. Bestehende Dateien (sechs, alle rein additiv)

| Datei | Änderung | Warum unvermeidbar |
|---|---|---|
| `app\config.py` (bei Zeile 26, neben `growth_engine_enabled`) | `studio_enabled: bool = False` | Der Schalter |
| `app\database.py`, in `init_db()` neben `from app import models` | `from app.studio import models as _studio_models  # noqa: F401` | Ohne Registrierung in `Base.metadata` legt `create_all` die Tabellen nicht an |
| `app\main.py` (Import-Block Z. 22–34, `include_router`-Block Z. 113–127) | `from app.routers import ...` bleibt; `from app.studio import router as studio_router` + `app.include_router(studio_router.router)` | Der Router riegelt sich selbst ab (404), darum ist Einbinden gefahrlos |
| `app\schemas.py:247` `HealthResponse` | `studio_enabled: bool = False` | Das Dashboard muss den Zustand erfahren |
| `app\routers\system.py:131` | `studio_enabled=s.studio_enabled,` | dito |
| `app\static\index.html` | drei Stellen, exakt nach Growth-Vorbild: Z. 703 ein `<button data-view="studio" hidden>`; Z. 1213 `if (name === "studio" && !studioEnabled) name = "overview";`; Z. 1608–1613 `studioEnabled = h.studio_enabled === true;` + `studioTab.hidden = !studioEnabled;` | Reiter versteckt |

**`app\scheduler.py` wird nicht angefasst.** Etappe 1 bringt keinen Job mit. Das ist selbst ein Beweisstück (Test Nr. 5).

Dazu kommen die `is_studio`-Einhängungen aus Abschnitt 6 — additiv, und bei ausgeschaltetem Schalter nachweislich wirkungslos.

---

## 4. Der Schalter

**`STUDIO_ENABLED`** in der `.env` → `settings.studio_enabled`, Vorgabe `False`.

Ein Schalter, drei Wirkungen:
1. `app\studio\router.py` → jeder Studio-Endpunkt liefert 404 (nicht 403 — die Existenz wird nicht verraten)
2. `guard.is_studio()` → immer `False`, `guard.exclude_studio()` → Statement unverändert
3. `/health` → `studio_enabled: false` → Dashboard versteckt den Reiter und wirft eine offene Studio-Ansicht auf „Übersicht" zurück

In `tests\conftest.py` neben `os.environ["GROWTH_ENGINE_ENABLED"] = "false"` (Z. 24) ergänzen: `os.environ["STUDIO_ENABLED"] = "false"`. Damit läuft die gesamte Suite gegen den Aus-Zustand — die Regressionswirkung der 400+ bestehenden Tests ist der breiteste Beweis überhaupt.

---

## 5. Beweise, dass der Handel unverändert läuft

**Reihenfolge zwingend:** Erst Grundlinie erzeugen, dann verdrahten.

| # | Test | Beweist |
|---|---|---|
| 1 | `test_studio_flag_defaults_off` — `Settings.model_fields["studio_enabled"].default is False` | Deployment allein schaltet nichts frei (Vorbild `test_growth_engine.py:498`) |
| 2 | `test_studio_api_is_404_when_off` — jeder Studio-Pfad → 404, danach `db` prüfen: 0 Zeilen in beiden Tabellen | Kein Laufzeitzustand entsteht (Vorbild `:506`) |
| 3 | `test_studio_tables_created_but_empty` | Schema da, Inhalt leer |
| 4 | `test_is_studio_false_for_all_when_off` — Verknüpfungszeile von Hand einfügen, Schalter aus, `is_studio()` bleibt `False` für alle | Der Riegel hängt am Schalter, nicht an den Daten |
| 5 | **`test_scheduler_job_ids_unchanged`** — `{j.id for j in start_scheduler().get_jobs()}` gegen eine eingefrorene Menge der heutigen 20 IDs, geprüft mit Schalter **aus und an** | Kein neuer Nachtjob, keine geänderte Auslösung |
| 6 | **`test_exclude_studio_sql_identical_when_off`** — für jedes der in Abschnitt 6 angefassten Statements `str(stmt.compile())` vor/nach `exclude_studio` vergleichen | Die Abfragen des Handels sind *wörtlich* dieselben |
| 7 | **`test_handel_unveraendert.py`** — feste Dropship-Fixture (aktiv/Entwurf/beendet, mit und ohne Quelle, mit/ohne Klicks), dann `list_optimization_buckets`, `cleanup_candidate_ids`, `monitor_status`, `list_candidates`, `list_performance`, `sold_out_center`, `reprice_report`, `count_affected` aufrufen und als JSON gegen `tests\data\handel_baseline.json` vergleichen | Gleiche Eingabe, gleiche Ausgabe wie vor der Verdrahtung |
| 8 | `test_listings_schema_unchanged` — Spaltennamen von `listings` gegen eingefrorene Liste | Kein `ALTER TABLE` auf der Handelstabelle |
| 9 | `test_cleanup_excludes_studio_when_on` — Schalter **an**, verknüpftes Listing erfüllt jedes Aufräum-Kriterium → darf **nicht** in `cleanup_candidate_ids` erscheinen | Der Riegel greift, wenn er soll (sonst wäre Nr. 4–7 wertlos) |
| 10 | `test_publish_queue_requeue_skips_studio` — Schalter an, `publish_queued=True` + Verknüpfung → nicht eingereiht | dito für den Publish-Pfad |
| 11 | `python -m pytest -q` vollständig grün | Breitenschutz |
| 12 | `git diff --stat` zeigt in `app\services\*` ausschließlich hinzugefügte Zeilen, keine geänderten Bedingungen | Prüfbar per Auge |

---

## 6. `is_studio()` — Einhängepunkte nach Dringlichkeit

Sortiert nach: *läuft von allein* vor *Knopfdruck*, darin *schreibt bei eBay* vor *schreibt lokal*.

### Stufe 1 — läuft von allein UND fasst das eBay-Angebot an (Blocker für Etappe 1)

| # | Datei : Zeile | Funktion | Auslöser | Einhängen |
|---|---|---|---|---|
| 1 | `app\services\publish_queue.py:167` | `start_worker` (Wiedereinreihen) | **jeder Serverstart**, `app\main.py:74` | `exclude_studio(...)` auf das `stale`-Statement. Ohne Riegel wird ein Studio-Angebot nach jedem Neustart über den Dropshipping-Publish-Pfad veröffentlicht |
| 2 | `app\services\golive_service.py:3262` | `retry_failed_publishes` | alle ~20 Min (Minute 5/25/55) | Direkt neben `if not listing.product_id: continue` ein `if is_studio(listing): continue`. **Heute bereits gedeckt** (Studio-Listings haben `product_id = NULL`) — der Riegel hält, sobald ein Studio-Angebot je eine Quelle bekommt |
| 3 | `app\services\optimization_service.py:1142` | `auto_remove_unprofitable_multibuy` | täglich 03:40 | `exclude_studio` auf das `actives`-Statement. Entfernt sonst die Mengenrabatt-Staffel eines Studio-Angebots live und ohne Rückfrage |
| 4 | `app\services\monitoring_service.py:529` | `run_monitoring` | alle 6 h, Minute 15 — **nicht abschaltbar** | `stmt = exclude_studio(stmt)`. **Heute bereits gedeckt** durch `product_id.isnot(None)`; der Riegel verhindert, dass eine hinterlegte Quelle die Menge eines Studio-Angebots auf 0 zieht |

### Stufe 2 — läuft von allein, schreibt nur lokal (verfälscht Zahlen, kein Live-Eingriff)

| # | Datei : Zeile | Funktion | Auslöser |
|---|---|---|---|
| 5 | `app\services\ebay_import_service.py:38` | `sync_ebay_live_prices` | täglich 02:50 — schreibt `ebay_live_prices`, erzeugt Preis-Drift-Warnung gegen einen Zielpreis, den es nicht gibt |
| 6 | `app\services\ad_rate_service.py:49` | `sync_ad_rates` | täglich 02:30 — schreibt `ad_rate_pct` ins Studio-Angebot |
| 7 | `app\services\listing_match_service.py:557` **und `:749`** | `rebuild_reprice_report` | 02:50 + alle 6 h + bei jedem `GET /reprice-report` (`:790` baut selbst neu) — schreibt `cost_eur` und `db.commit()`. `:749` sammelt gezielt `product_id IS NULL` ⇒ trifft Studio-Angebote direkt |
| 8 | `app\services\ebay_import_service.py:357` | `sync_listing_stats` | täglich 03:00 (`daily_performance`) |
| 9 | `app\services\optimization_service.py:1287` | `refresh_click_data` | täglich 03:00 + montags — setzt Klicks/Impressionen auf 0 |
| 10 | `app\services\ebay_import_service.py:258` | `backfill_categories` | täglich 03:35 **und 3 Min nach jedem Serverstart** |
| 11 | `app\services\growth_engine_service.py:223`, `:338`, `:559` | Scorecard / Metriken / `detect_opportunities` | 06:00–06:30, nur bei `growth_engine_enabled` — zählt das Studio-Angebot sonst als schwaches AliExpress-Listing |

### Stufe 3 — nur auf Knopfdruck, aber greift live bei eBay ein

| # | Datei : Zeile | Funktion | Knopf |
|---|---|---|---|
| 12 | **`app\services\optimization_service.py:459` (Abfrage) und `:576` (`cleanup.append`)** | `list_optimization_buckets` | `POST /optimization/cleanup/end-all` → `app\routers\listings.py:193` → `cleanup_candidate_ids:1357` → `end_cleanup_candidates:1389`. **Größter Schaden im ganzen Register:** der Herkunfts-Filter `_unverified()` greift erst in Zeile 585, also *nach* dem Append; die zweite Sicherung `_cleanup_still_valid:1364` prüft nur Verkäufe/Klicks/Impressionen — ein frisches Studio-Angebot besteht sie und wird echt beendet. **Riegel gehört in Zeile 576, vor das `append`**, zusätzlich in `_cleanup_still_valid` als fail-closed-Nachprüfung |
| 13 | `app\services\precious_metal_service.py:177` (dazu `:53` Scan, `:154` Zählung) | `regenerate_affected` | `app\routers\products.py:192/195` — formuliert Titel und Beschreibung per KI neu und schiebt sie live; ein Studio-Motiv mit „Silber" im Text verliert seine eigene Markenbeschreibung |
| 14 | `app\services\ebay_import_service.py:203` | `import_listings` (Reconcile) | „Von eBay importieren" — setzt jedes aktive Listing außerhalb der Live-Liste auf `ended` |
| 15 | `app\services\listing_match_service.py:63` | `match_all` | **kein Aufrufer im Repo** — aber die Abfrage sucht exakt `product_id IS NULL`, also Studio-Angebote, und klebt ihnen per Bildsuche eine fremde AliExpress-Quelle an. Hier kein Filter, sondern ein **Riegel am Funktionsanfang**: bei eingeschaltetem Studio-Schalter `raise RuntimeError`, bevor irgendetwas läuft |
| 16 | `app\services\ebay_import_service.py:334` | `scan_shipping_policies` | `app\routers\products.py:744` |

### Stufe 4 — nur Anzeige (Riegel zur Sauberkeit der Kennzahlen)

| # | Datei : Zeile | Funktion |
|---|---|---|
| 17 | `app\services\monitoring_service.py:582` | `monitor_status` — Studio-Angebot erscheint mit `cost_eur = None` und verfälscht die Gesamt-Marge des Dropshipping-Cockpits |
| 18 | `app\services\optimization_service.py:79`, `:104`, `:342`, `:373`, `:1026` | `list_performance`, `list_suggestions`, `list_candidates`, `list_volume_pricing_candidates` |
| 19 | `app\services\listing_match_service.py:235` | `sold_out_center` — hebt ein quellenloses Studio-Angebot fälschlich als „komplett ausverkauft" nach oben |
| 20 | `app\services\analytics_service.py:55` | OOS-Kachel |

**Ausdrückliche Ausnahme:** `app\services\analytics_service.py:349`, `:505`, `:667`, `:824`, `app\services\finance_service.py:234`, `app\services\invoice_service.py:618` bleiben **ohne Riegel**. Das sind Umsatz-, Gebühren- und Rechnungspfade; ein Studio-Verkauf ist echter Umsatz desselben Gewerbes und darf dort nicht verschwinden. Sonst entsteht eine Lücke zwischen eBay-Abrechnung und eigener Buchhaltung.

---

## Reihenfolge der Umsetzung

1. `tests\data\handel_baseline.json` erzeugen und einchecken (Zustand **vor** jeder Änderung)
2. `app\studio\` anlegen, sechs bestehende Dateien anfassen, Tests 1–8 grün
3. Einhängen Stufe 1 → Test 6 + 7 müssen weiterhin identisch sein, Test 10 grün
4. Stufen 2–4 in derselben Schleife, nach jeder Stufe Test 7
5. `python -m pytest -q` vollständig grün, dann commit. `STUDIO_ENABLED` bleibt aus.