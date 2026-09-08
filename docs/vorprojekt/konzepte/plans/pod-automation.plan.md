# Plan: KI-Design-Generator (mit IP-Filter)

**Source PRD**: `.claude/prds/pod-automation.prd.md`
**Selected Milestone**: 1 — KI-Design-Generator (mit IP-Filter)
**Complexity**: Large (Greenfield, mehrere Integrationen, aber offline testbar)

## Summary
Ein Python-CLI-Tool, das aus Themen (KI-erfunden **und** eigene Konzepte des Betreibers) POD-taugliche Design-Dateien erzeugt: Motiv per KI-Bildmodell, Text (Namen/Sprüche) verlässlich per Overlay, Transparenz + Upscale auf Druckspezifikation, davor ein IP-/Content-Filter. Alles läuft **ohne eBay-/Printify-Accounts** und ist dank Mock-Provider **komplett offline entwickel- und testbar**; echte Bild-APIs (OpenAI gpt-image-1 / fal.ai Flux) sind hinter einer Schnittstelle gekapselt und werden per Config gewählt.

## Patterns to Mirror
| Category | Source | Pattern |
|---|---|---|
| Naming | — | Kein bestehender Code im Repo (Greenfield). Neue Konvention: `snake_case` Module, `PascalCase` Klassen, kleine kohäsive Dateien (200–400 Zeilen) je Verantwortung. |
| Errors | — | Keine Vorlage. Neu: eigene Exceptions in `pod/errors.py`, an Systemgrenzen (API, Datei, Filter) explizit fangen & mit Kontext loggen; nie still schlucken. |
| Tests | — | Keine Vorlage. Neu: `pytest` unter `tests/`, AAA-Struktur, Mock-Provider statt echter API-Calls, deterministische Assertions. |

> Hinweis: Der (separate) bestehende Shop des Nutzers ist Python/FastAPI — Konventionen können wir später angleichen. Dieses Repo bleibt eigenständig.

## Stack-Vorschlag (Greenfield)
- **Python 3.11+**, `src/`-Layout, `pyproject.toml`
- **Typer** (CLI), **pydantic-settings** (Config/.env), **Pillow** (Text-Overlay/Bildops), **rembg** (Transparenz, falls Modell keine liefert), **SQLModel/SQLite** (Design-Tracking), **httpx** (API-Calls)
- **openai** (gpt-image-1) und **fal-client** (Flux) als optionale Provider hinter einem gemeinsamen Interface
- **pytest** + **ruff** (Lint/Format)
- Secrets nur via `.env` (nie im Code) — entspricht deiner Security-Regel

## Druck-Spezifikation (Zielformat der Design-Dateien)
- PNG, **RGBA mit transparentem Hintergrund**
- Zielauflösung ~**4500 × 5400 px** (DTG-Standard, ~300 DPI auf ~15×18″); pro Produkttyp konfigurierbar
- Farbraum sRGB; Metadaten je Design in SQLite

## Files to Change
| File | Action | Why |
|---|---|---|
| `pyproject.toml`, `.env.example`, `.gitignore`, `README.md` | CREATE | Projektgerüst, Secrets-Vorlage, Ignore für `designs/`+`data/`+`.env` |
| `src/pod/config.py` | CREATE | Zentrale Einstellungen (Provider-Wahl, Druck-Specs, Pfade) via .env |
| `src/pod/errors.py` | CREATE | Eigene Exceptions (Provider-, Filter-, IO-Fehler) |
| `src/pod/cli.py` | CREATE | Typer-CLI: `generate`, `eval`, `review`, `approve`, `list` |
| `src/pod/generation/base.py` | CREATE | `ImageProvider`-Protocol (Schnittstelle für Anbieterwechsel) |
| `src/pod/generation/mock_provider.py` | CREATE | Deterministischer Offline-Provider für Entwicklung/Tests |
| `src/pod/generation/openai_provider.py` | CREATE | gpt-image-1 (starke Textwiedergabe, native Transparenz) |
| `src/pod/generation/fal_provider.py` | CREATE | fal.ai Flux (günstiger; gut in Kombi mit Text-Overlay) |
| `src/pod/themes/generator.py` | CREATE | KI-Themengenerator (LLM erzeugt Nischen/Sprüche) |
| `src/pod/themes/concepts.py` | CREATE | Eigene Konzepte des Betreibers aus YAML/CSV einlesen |
| `src/pod/prompts/builder.py` | CREATE | Thema → Bildprompt (Stil, Motiv, optional Text) |
| `src/pod/postprocess/text_overlay.py` | CREATE | Sauberer Text (Namen/Sprüche) per Pillow — Default statt KI-Text |
| `src/pod/postprocess/background.py` | CREATE | Transparenz sicherstellen (native oder rembg) |
| `src/pod/postprocess/upscale.py` | CREATE | Hochskalieren auf Druck-Spec |
| `src/pod/safety/ip_filter.py` | CREATE | Blocklist-Filter (Marken/Promis/Franchises/verbotene Inhalte) |
| `src/pod/safety/blocklist.yaml` | CREATE | Kuratierte Sperrliste (erweiterbar) |
| `src/pod/storage/models.py` | CREATE | `Design`-Datensatz (Thema, Prompt, Modell, Filter, Pfad, Status) |
| `src/pod/storage/repository.py` | CREATE | Persistenz/Abfragen (Repository-Pattern) |
| `src/pod/pipeline.py` | CREATE | Orchestriert Thema→Prompt→Filter→Bild→Postprocess→Speichern |
| `tests/…` | CREATE | Unit- + End-to-End-Tests (mit Mock-Provider), Ziel 80 %+ |

## Tasks

### Task 1: Projektgerüst & Config
- **Action**: `pyproject.toml` (Deps, ruff, pytest), `src/pod/`-Layout, `config.py` (pydantic-settings, lädt `.env`), `errors.py`, leeres `cli.py`-Skelett, `.env.example`, `.gitignore`, `README`.
- **Mirror**: neue Konvention (kleine Dateien, .env-Secrets).
- **Validate**: `pip install -e .` klappt; `pod --help` zeigt Kommandos; `pytest` läuft (0 Tests grün).

### Task 2: Provider-Interface + Mock + reale Provider
- **Action**: `ImageProvider`-Protocol (`generate(prompt, size, transparent) -> Bild`). `mock_provider` (deterministisch, malt Platzhalter). `openai_provider` (gpt-image-1) + `fal_provider` (Flux) real angebunden, per Config wählbar. Fehler → `ProviderError`.
- **Mirror**: Repository-/Strategy-Pattern; Secrets aus Config.
- **Validate**: Unit-Test: Mock liefert reproduzierbares Bild; Provider-Auswahl per Config getestet; reale Provider ohne Key sauber mit klarer Fehlermeldung.

### Task 3: Modell-Evaluations-Spike (löst offene Kernfrage)
- **Action**: CLI `pod eval --prompts <datei>` erzeugt Vergleichsraster: gpt-image-1 vs. Flux, jeweils (a) KI-Text vs. (b) Motiv+Overlay. Protokolliert **Kosten/Bild** und **Textschärfe** (visuell) + kommerzielle Lizenzlage.
- **Mirror**: nutzt Provider-Interface aus Task 2.
- **Validate**: Erzeugt Vergleichsbilder + kurze Markdown-Notiz. *(Realer Lauf erst, wenn du einen API-Key hast — bis dahin gegen Mock lauffähig.)*
- **Ergebnis-Gate**: Entscheidung Modell + Text-Strategie wird hier final getroffen und im README/Config festgehalten.

### Task 4: Themen-Quellen + Prompt-Builder
- **Action**: `themes/generator.py` (LLM erzeugt N Themen/Sprüche zu Nische, mockbar), `themes/concepts.py` (eigene Konzepte aus YAML/CSV), `prompts/builder.py` (Thema → strukturierter Bildprompt; markiert Text, der später als Overlay kommt).
- **Mirror**: Input-Validierung an der Grenze (deine Regel: externe Daten nie vertrauen).
- **Validate**: Unit-Tests: deterministische Prompts aus Fixtures; Konzept-Loader validiert & meldet fehlerhafte Zeilen; Themengenerator mockbar.

### Task 5: Post-Processing (Text-Overlay, Transparenz, Upscale)
- **Action**: `text_overlay.py` (Pillow: Sprüche/Namen scharf platzieren, Schriftwahl, Zeilenumbruch), `background.py` (Transparenz sichern), `upscale.py` (auf Druck-Spec ~4500×5400 RGBA).
- **Mirror**: reine Funktionen, unveränderliche Eingaben.
- **Validate**: Test prüft Ausgabe-PNG auf Größe, `mode == "RGBA"`, echte Transparenz; Overlay-Text an erwarteter Position.

### Task 6: IP-/Content-Filter
- **Action**: `blocklist.yaml` (Marken, Promis, Franchises, verbotene Inhalte) + `ip_filter.py`: prüft **vor** der Generierung Thema/Prompt/Overlay-Text; blockierte → Status `rejected` mit Grund. Fuzzy-Matching gegen offensichtliche Umgehungen.
- **Mirror**: fail-fast mit klarer Meldung.
- **Validate**: Unit-Tests: bekannte geschützte Begriffe werden geblockt, saubere Begriffe passieren; Umgehungs-Varianten (Leerzeichen/Ziffern) erkannt.

### Task 7: Storage + Review/Freigabe
- **Action**: `models.py` (`Design`: id, thema, prompt, modell, filter_ergebnis, pfad, status=pending/approved/rejected, timestamps), `repository.py` (create/list/get/update). CLI `list`, `review`, `approve <id>` → der eine „Freigabe-Klick" aus der PRD.
- **Mirror**: Repository-Pattern; SQLite-Schreibsperre kurz halten.
- **Validate**: Roundtrip-Test create→query→approve; Statuswechsel korrekt.

### Task 8: Pipeline + Batch-Generierung
- **Action**: `pipeline.py` verkettet alle Schritte; CLI `generate --count N --source ai|concepts --provider mock|openai|fal`. Robust: ein fehlerhaftes Design stoppt nicht den Batch; Ergebnis je Design als Datensatz + Datei.
- **Mirror**: explizites Error-Handling pro Item; Logging je Schritt.
- **Validate**: End-to-End-Test mit Mock-Provider erzeugt N Datensätze + Dateien; Filter greift; `--count 100` läuft offline durch.

### Task 9: README + Betriebsdoku
- **Action**: README: Setup, `.env`-Keys, Kommandos, Druck-Specs, wie man von Mock auf echten Provider wechselt, Blocklist pflegen.
- **Validate**: Anleitung Schritt für Schritt nachvollziehbar; Befehle stimmen.

## Validation
```bash
# im Projektordner C:\Users\HP\Projekte\POD-Shop
pip install -e ".[dev]"
ruff check src tests
pytest -q                     # Ziel: alles grün, 80%+ Coverage
pod generate --count 3 --source concepts --provider mock   # offline End-to-End
pod list                      # 3 Designs sichtbar
# Sobald API-Key vorhanden:
pod eval --prompts examples/eval_prompts.yaml              # Modellvergleich
```

## Risks
| Risk | Likelihood | Mitigation |
|---|---|---|
| KI rendert Text verzerrt (Namen/Sprüche) | Mittel | Text-Overlay (Pillow) als Default → Text ist immer scharf; KI-Text nur optional |
| Upscale-Qualität reicht nicht für Druck | Mittel | Modell-native Hochauflösung nutzen; bei Bedarf Real-ESRGAN/hosted Upscaler; Druckprobe vor Masse |
| IP-Filter übersieht geschützte Motive | Hoch | Konservative, gepflegte Blocklist + Fuzzy-Match + manuelle Freigabe-Stichprobe (Task 7) |
| Kommerzielle Lizenz des KI-Modells unklar | Mittel | Nur Modelle mit klarer kommerzieller Lizenz; im Eval-Spike (Task 3) prüfen & dokumentieren |
| Noch kein API-Key vorhanden → Blocker | Sicher (temporär) | Komplette Entwicklung + Tests gegen Mock-Provider; echte APIs erst für Eval/Erst-100 |
| `rembg`/Torch-Abhängigkeit schwer/gross | Niedrig | Erst native Transparenz des Modells nutzen; rembg nur als Fallback, optionales Extra |

## Acceptance
- [ ] Alle Tasks abgeschlossen
- [ ] `pytest` grün, 80%+ Coverage; `ruff` sauber
- [ ] Offline-Batch (`--provider mock`) erzeugt Design-Dateien + Datensätze mit Freigabe-Status
- [ ] IP-Filter blockt geschützte Begriffe nachweislich
- [ ] Ausgabe-PNGs erfüllen Druck-Spec (RGBA, Zielauflösung, transparent)
- [ ] Modellentscheidung aus Eval-Spike dokumentiert (oder klar als offen markiert, bis Key da ist)
- [ ] Konventionen neu etabliert, nicht erfunden-inkonsistent

---
*Status: DRAFT — wartet auf Bestätigung, bevor Code geschrieben wird.*
