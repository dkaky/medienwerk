# CLAUDE.md — POD Shop

## Was das ist

Print-on-Demand-System für **Medienwerk** (eingetragenes Einzelunternehmen).
Store-Marke bei eBay ist **Druckhelden**, das Projekt heißt **POD Shop**.

**Kein Dropshipping.** Verkauft werden eigene Motive, gedruckt auf Bestellung.
Es wird nichts eingekauft, nichts weiterverkauft und bei keinem Lieferanten
bestellt. Wer im Code noch einen Lieferanten-, Einkaufs- oder Bestellweg findet,
hat einen Rest gefunden — kein gültiges Muster.

Der Weg: Motiv entwerfen (`app/studio/`) → drucken lassen (Printify) →
verkaufen (eBay als „Druckhelden", Spreadshirt im Aufbau). Später geplant:
weitere Verkaufskanäle (Etsy und andere).

## Eigenständig

Dieses Projekt gehört **Medienwerk** und steht für sich. Es gibt kein
Mutterprojekt, keinen geteilten Server und keine fremden Konten.

Es gibt eine Vorgeschichte: Teile des Handelsteils und der Studio-Trakt stammen
aus früheren eigenen Arbeiten und sind hier erprobt weitergeführt worden. Was
davon übrig ist, steht in diesem Verzeichnis — sonst nirgends.

Daraus folgt eine Regel, die nicht verhandelbar ist: **keine fremden
Zugangsdaten, keine fremden Deploy-Ziele.** Wer in einer Datei einen Schlüssel,
einen Server oder einen Pfad findet, der nicht zu diesem Projekt gehört, hat
einen Rest gefunden — kein gültiges Ziel. Solche Funde gehören entfernt, nicht
benutzt.

## Start und Tests

- Umgebung: `.venv\Scripts\python.exe` (Python 3.11)
- **Port 8030.** 8000 und 8010 werden gemieden — dort lagen früher andere
  Server, ein Zahlendreher soll nicht im falschen Dienst landen.
- Start: `START-DASHBOARD.bat`
- Tests: `python -m pytest -q` — über 1400 Tests, müssen vor jedem Commit grün
  sein. Ein voller Lauf dauert rund 15 Minuten.

## Aufbau

- `app/` — FastAPI, SQLAlchemy, SQLite. Der Verkaufsteil: eBay-Anbindung,
  Preise, Belege, Bank. Erprobt, aber aus der Dropshipping-Zeit — hier stehen
  noch Reste, die nicht mehr gebraucht werden.
- `app/studio/` — der Kreativteil: eigene Motive, Bilderzeugung, Printify.
  Standardmäßig **ausgeschaltet** (`STUDIO_ENABLED`).
- `app/integrations/spreadshirt.py` — zweiter Verkaufskanal, nur lesend.
- `app/static/index.html` — die alte Oberfläche (5.800 Zeilen, ein Stück).
- `app/static/studio.html` — der erste Bereich im neuen Design. Weitere Bereiche
  werden nach und nach nachgezogen.
- `docs/BAUPLAN-STUDIO-ETAPPE1.md` — die 20 Stellen, an denen der Riegel
  zwischen Studio und Handel sitzen muss.

## Eiserne Regeln

1. **Propose-only.** Nichts geht ohne Klick eines Menschen live. Kein Angebot,
   keine Preisänderung, kein Druckauftrag.
2. **Niemals automatisch löschen** — weder Listings noch Produkte. Nur als
   „Aktion erforderlich" kennzeichnen.
3. **Keine Schätzungen.** Fehlt ein Wert, bleibt er leer. Lieber eine Lücke als
   eine erfundene Zahl.
4. **Der Riegel `is_studio()`** trennt den Studio-Weg vom alten Verkaufsteil.
   Solange dort noch Dropshipping-Reste sitzen, muss jede Automatik, die
   Angebote anfasst, ihn einhängen.
5. **Kostenbremse.** Bilderzeugung nur mit gesetztem Tagesbudget. Ohne Budget
   wird nichts erzeugt.
6. **Marken- und Rechteprüfung** vor der Erzeugung, nicht danach. Ein
   verworfenes Bild kostet trotzdem Geld.
7. **Keine fremden Keys.** Siehe „Eigenständig“ oben.
8. §19-Kleinbetragsrechnungen, Nummern im Format `MW-JJJJ-NNNN`.

## Offene Punkte

- **Hochskalierung fehlt.** Von den 48 Motiven sind 19 druckfähig (4500×5400),
  28 zu klein — 1024×1536 ergibt nur 51 DPI bei 15 Zoll Druckbreite. Der
  Umrechner (`app/studio/postprocess/umrechner.py`) bricht dann ab, statt
  heimlich hochzurechnen; das ist Absicht. Was fehlt, ist der Weg drumherum:
  ein Hochskalierer oder eine Neuerzeugung in Druckgröße.
- **Der Dropshipping-Ausbau läuft noch.** AliExpress-Kern und Bestellimport
  sind raus; in Preisen, Belegen, Modellen und Tests stehen noch Reste. Bis das
  fertig ist, ist die Testsuite nicht grün.
- **Preis & Bestand hängt am Bericht.** Verlust und dünne Marge kommen nicht aus
  `/dashboard/summary`, sondern aus dem materialisierten `reprice-report`, der
  veralten kann.
- Eigener Server für Nachtjobs und den Motiv-Radar (braucht einen Browser).
- LUCID-Registrierung und Rechtstexte vor dem ersten Verkauf.
- **Vor dem ersten Push nach außen:** Die Historie enthält in frühen Commits
  einen Ausweis-Scan und die Gewerbeanmeldung. Aus der Nachverfolgung sind sie
  raus, aus der Geschichte nicht. Entweder bereinigen oder das Archiv frisch
  aufsetzen.
- **Keine Sicherung außer Haus.** Weder dieses Projekt noch das gebündelte
  Vorprojekt (`docs/vorprojekt/pod-shop-historie.bundle`) liegen irgendwo sonst.

## Erledigt, damit es niemand doppelt sucht

- eBay-Entwurf ohne Veröffentlichen: `POST /api/v1/products/{id}/ebay-draft`,
  Knopf „eBay-Entwurf" in Liste und Detailfenster.
- Kategorie: Das native Backend ermittelt sie selbst (eBay-Vorschlag zum Titel)
  und meldet klar, wenn keine zu finden ist. Die „0" geht nirgends mehr raus.
- Der Live-Knopf verlangt `bestaetigt=true` und fragt vorher nach.
- 33 Motive aus dem Vorprojekt liegen in `studio_designs`; die einmaligen
  Übernahme-Skripte sind nach getaner Arbeit gelöscht.
- Spuren fremder Betriebe sind aus Oberfläche, Favicon, Code, Tests und
  Rechtstexten entfernt; der Kontist-Rückruf hat keine Vorgabe mehr.
- Startseite „Heute": Aufgabenband statt Jahresumsatz. Kacheln mit 0
  verschwinden, jede springt vorgefiltert an die zuständige Stelle.
- Der Probebetrieb (`MOCK_EBAY=true`) hält **jeden Schreibzugriff** auf eBay an,
  nicht nur die Knöpfe. Die Sperre sitzt im HTTP-Weg des echten Clients
  (`_NurLesenClient` in `app/integrations/ebay.py`), weil sie an den Aufrufstellen
  siebenmal fehlte — unter anderem in zwei Zeitplaner-Jobs, die **ohne Klick**
  laufen. Lesen bleibt echt (dafür gibt es den echten Client), die Anmeldung auch;
  sonst stürbe das Lesen mit. Die Kopfleiste zeigt den Zustand „Probebetrieb".
- Varianten-Werkbank unter Optimierung: Bericht, Trockenlauf, Sammelreparatur
  und Bildreparatur je Listing. Von den 15 Varianten-Adressen hatten vorher
  genau zwei einen Knopf.
- **Motiv-Radar** (`app/studio/radar/`): Shop-Link rein, Motiv-Ideen raus. Vier
  Stufen — Link erkennen, Browser ernten, Signal rechnen, eigenen Prompt
  entwerfen. Die Grenze ist eine Prüfung, keine Absicht:
  `signale.enthaelt_wortlaut()` weist jeden Entwurf ab, der ein Zitat oder vier
  Wörter am Stück aus dem fremden Titel trägt. Zwei Eigenarten der Seiten stehen
  in den Modulköpfen: `ebay.de/str/<shop>` ist nur ein Schaufenster (die Liste
  hängt am Verkäufernamen `_ssn`), und **ohne Anmeldung zeigt eBay keine
  Verkaufszahlen** — das Signal ist dann nur die Position.
- **Vom Motiv zum Produkt gibt es einen Weg** (`app/studio/produktweg.py`):
  Druckcheck ohne Wirkung, dann Printify-Entwurf mit Bestätigung. Der Umrechner
  sitzt zwingend dazwischen — `erstelle_produkt` prüft die Auflösung selbst
  nicht und hätte ein 1024er Motiv klaglos für 4500 hochgeladen.
