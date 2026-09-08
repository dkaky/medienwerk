# CLAUDE.md — POD Shop

## Was das ist

Handels- und Automatisierungssystem für **medienwerk** (eingetragenes
Einzelunternehmen). Store-Marke bei eBay ist **Druckhelden**, das Projekt heißt
**POD Shop**.

Zwei Verkaufsspuren:

1. **Dropshipping** — Handelsware von AliExpress über eBay. Das ist der aktive
   Schwerpunkt, hier soll das erste Geld verdient werden.
2. **Print-on-Demand** — eigene KI-Motive über Printify. Im Aufbau
   („Studio-Trakt", `app/studio/`), noch kein Umsatz.

Später geplant: weitere Verkaufskanäle (Etsy und andere).

## Herkunft — bitte genau lesen

Dieses Projekt ist eine **Kopie** eines erprobten eBay-Systems, das einer GbR
(Maison d'Aristide / Kaky & Syed GbR) gehört. Kopiert wurde ausschließlich
**Code**. Nicht kopiert und niemals zu verwenden:

- Zugangsdaten, Konten, Schlüssel der GbR
- deren Geschäftsdaten
- deren Deploy-Ziele

**Das Original liegt unter `C:\Users\HP\Projekte\eBay-Automation` und läuft auf
einem VPS der GbR. Dieses Projekt hat damit nichts zu tun.** Wer hier einen
Deploy-Vorgang findet, der auf `/opt/ebay-automation` zeigt, hat einen Fehler
gefunden — nicht eine Anleitung.

Ein zweites Vorprojekt lag unter `C:\Users\HP\Projekte\POD-Shop` (kleiner,
12.000 Zeilen). Es ist **abgeräumt**: alles Brauchbare steckt im Studio-Trakt,
seine Versionsgeschichte liegt als Bündel unter
`uebernommen/pod-shop-historie.bundle`, seine 33 Motive in `studio_designs`.
Der Ordner selbst wurde gelöscht. Wer in einer Datei noch einen Verweis darauf
findet, hat einen Rest gefunden — kein gültiges Ziel.

Alles aus dem Vorprojekt, das nicht in den laufenden Code wanderte, liegt unter
`uebernommen/`. Darunter `web-referenz/` — die alte React-Oberfläche, die als
Design-Vorlage dient und **nicht** gestartet wird.

## Start und Tests

- Umgebung: `.venv\Scripts\python.exe` (Python 3.11)
- **Port 8030.** Nicht 8000 — das ist der Server der GbR. (8010 gehörte dem
  gelöschten Vorprojekt und ist jetzt frei, bleibt aber gemieden.)
- Start: `START-DASHBOARD.bat`
- Tests: `python -m pytest -q` — über 1400 Tests, müssen vor jedem Commit grün
  sein. Ein voller Lauf dauert rund 15 Minuten.

## Aufbau

- `app/` — FastAPI, SQLAlchemy, SQLite. Der Handelsteil, aus dem Original
  übernommen und erprobt.
- `app/studio/` — der neue Kreativteil: eigene Motive, Bilderzeugung, Printify.
  Standardmäßig **ausgeschaltet** (`STUDIO_ENABLED`).
- `app/static/index.html` — die alte Oberfläche (5.800 Zeilen, ein Stück).
- `app/static/studio.html` — der erste Bereich im neuen Design. Weitere Bereiche
  werden nach und nach nachgezogen.
- `docs/BAUPLAN-STUDIO-ETAPPE1.md` — die 20 Stellen, an denen der Riegel
  zwischen Studio und Handel sitzen muss.

## Eiserne Regeln

1. **Propose-only.** Nichts geht ohne Klick eines Menschen live. Kein Angebot,
   keine Preisänderung, keine Bestellung.
2. **Niemals automatisch löschen** — weder Listings noch Produkte. Nur als
   „Aktion erforderlich" kennzeichnen.
3. **Keine Schätzungen.** Fehlt ein Wert, bleibt er leer. Lieber eine Lücke als
   eine erfundene Zahl.
4. **Der Riegel `is_studio()`** trennt Studio von Handel. Wer eine Automatik
   baut, die Angebote anfasst, hängt ihn ein — sonst behandelt der Handelsteil
   ein Print-on-Demand-Angebot wie einen China-Artikel.
5. **Kostenbremse.** Bilderzeugung nur mit gesetztem Tagesbudget. Ohne Budget
   wird nichts erzeugt.
6. **Marken- und Rechteprüfung** vor der Erzeugung, nicht danach. Ein
   verworfenes Bild kostet trotzdem Geld.
7. **Keine fremden Keys.** Siehe Herkunft oben.
8. §19-Kleinbetragsrechnungen, Nummern im Format `MW-JJJJ-NNNN`.

## Offene Punkte

- **Hochskalierung fehlt.** Von den 48 Motiven sind 19 druckfähig (4500×5400),
  28 zu klein — 1024×1536 ergibt nur 51 DPI bei 15 Zoll Druckbreite. Der
  Umrechner (`app/studio/postprocess/umrechner.py`) bricht dann ab, statt
  heimlich hochzurechnen; das ist Absicht. Was fehlt, ist der Weg drumherum:
  ein Hochskalierer oder eine Neuerzeugung in Druckgröße.
- **Preis & Bestand hängt am Bericht.** Verlust und dünne Marge kommen nicht aus
  `/dashboard/summary`, sondern aus dem materialisierten `reprice-report`, der
  veralten kann.
- Eigener Server für Nachtjobs und den Shop-Scanner (braucht einen Browser).
  Soll mit KakyOS zusammen laufen.
- LUCID-Registrierung und Rechtstexte vor dem ersten Verkauf.
- **Vor dem ersten Push nach außen:** Die Historie enthält in frühen Commits
  einen Ausweis-Scan und die Gewerbeanmeldung (`uebernommen/Marketingagentur/
  Alte Anmeldung/`). Aus der Nachverfolgung sind sie raus, aus der Geschichte
  nicht. Entweder bereinigen oder das Archiv frisch aufsetzen.
- **Keine Sicherung außer Haus.** Weder dieses Projekt noch das gebündelte
  Vorprojekt (`uebernommen/pod-shop-historie.bundle`) liegen irgendwo sonst.

## Erledigt, damit es niemand doppelt sucht

- eBay-Entwurf ohne Veröffentlichen: `POST /api/v1/products/{id}/ebay-draft`,
  Knopf „eBay-Entwurf" in Liste und Detailfenster.
- Kategorie: Das native Backend ermittelt sie selbst (eBay-Vorschlag zum Titel)
  und meldet klar, wenn keine zu finden ist. Die „0" geht nirgends mehr raus.
- Der Live-Knopf verlangt `bestaetigt=true` und fragt vorher nach.
- 33 Motive aus dem Vorprojekt sind übernommen
  (`scripts/uebernimm_altmotive.py`), das Vorprojekt ist gelöscht.
- Spuren des fremden Betriebs sind aus Oberfläche, Favicon und Code entfernt;
  der Kontist-Rückruf zeigt nicht mehr auf dessen Server.
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
- Eine Zählweise für „ausverkauft" statt drei (`lieferstatus()`). Eigenbestand
  schlägt jetzt jedes Lieferantensignal, gerettete Varianten zählen nicht mehr
  als ausverkauft, und Kachel und Liste zeigen dieselbe Menge.
- **Der Shop-Import macht weiter, wo er aufhörte.** 100 Artikel je Lauf bleiben
  der Deckel; der zweite Lauf beginnt bei 101, der dritte bei 201
  (`store_gesehen:<shop>` in den Einstellungen). Gescheiterte zählen als
  durchgesehen — sonst bekäme der nächste Lauf genau die wieder vorgesetzt, die
  schon einmal nicht funktioniert haben.
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
