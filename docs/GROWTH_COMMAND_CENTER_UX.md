# Growth Command Center — UX-Spezifikation (V1)

Autor: Claude (Produkt-Architekt/UX). Stand 19.08.2026. Zweck: Codex baut das
Backend der Growth Engine; DIESES Dokument definiert die Besitzer-Oberflaeche so,
dass sie sich nativ in den bestehenden POD Shop einfuegt. Der Besitzer (Kaky)
interagiert NIE mit Codex-/Claude-Prompts — nur mit diesem Dashboard.

## 0. Nicht verhandelbare Rahmenbedingungen (aus der Projekt-Historie)

1. **EINE Oberflaeche.** Jede UI-Aenderung kommt in `app/static/index.html` — und
   nur dorthin. Bis zum 25.08.2026 galt hier das Gegenteil („beide Designs"),
   weil `index_classic.html` als zweite, dunkle Fassung derselben Oberflaeche
   gepflegt wurde. Die Datei ist entfallen: 5.500 Zeilen und 63 dieselben
   Serveradressen doppelt zu pflegen, nur um andere Farben zu haben, hat mehr
   veraltete Staende erzeugt als Nutzen gebracht. Hell und Dunkel macht
   `index.html` selbst (`:root` dunkel, `body.light` hell, Knopf oben rechts).
2. **Deutsch, ELI5-tauglich.** Alle Texte deutsch; jede Meldung sagt, was der
   Besitzer selbst tun kann. Keine internen Codes ohne Uebersetzung (Vorbild:
   `_klartext_ablehnung` in order_service).
3. **Bestehende UI-Sprache.** Kein neues Framework, kein Build-Schritt. Vanilla
   JS im vorhandenen Stil: `nav.tabs` + `section.view`, `.kpis`/`.kpi`-Karten,
   `.card`/`.body`, `.table-scroll` (KEIN Innen-Scroll — Nutzer-Entscheid 15.08.,
   der Scrollbalken gehoert ganz rechts an die Seite), `.badge`,
   `.btn sm [primary|ghost]`, `toast()` (Fehler bleiben 6,2 s), `esc()` fuer
   JEDEN dynamischen Text (XSS), Status-Label-Maps wie `ORDER_STATUS_DE`.
4. **Ruhe als Grundzustand.** Der Standard-Bildschirm ist entscheidungs-
   orientiert und still. Komplexitaet erst hinter DETAILS. **Null offene
   Entscheidungen ist ein gueltiger und guter Zustand** — positiv darstellen,
   nie als leerer Fehler.
5. **Freigaben laufen ueber BESTEHENDE, abgesicherte Pfade.** Ein APPROVE-Knopf
   erzeugt nie einen neuen Schreibweg an eBay/AliExpress vorbei an den
   vorhandenen Guards (Details: GROWTH_APPROVAL_POLICY.md).

## 1. Einordnung in die Seiten-Hierarchie

**Besitzer-Entscheid 19.08. („wieso nicht unter optimierung?"): KEIN neuer Tab.**
Das Growth Command Center wird OBEN in den bestehenden Tab **„Optimierung"**
(`view-optimization`) integriert:

- Der Optimierung-Tab ist bereits die Entscheidungs-Oberflaeche des Besitzers
  (Titel-Vorschlaege mit Freigabe, 0-Klick-Liste, Ladenhueter/Aufraeum-Buckets,
  Vorher-Nachher-Verfolgung) — die Growth-„Entscheidungen" sind dasselbe
  Interaktionsmuster. EIN Ort fuer „Was braucht mich?", ein Zaehler-Badge.
- Reihenfolge im Tab: Growth-Abschnitte [1]–[5] (Abschnitt 2) ZUERST, danach
  die BESTEHENDEN Optimierungs-Werkzeuge unveraendert (kein Umbau, kein
  Umbenennen, keine Verhaltensaenderung — der gewohnte Arbeitsfluss bleibt).
- Der Tab traegt einen **Zaehler-Badge NUR fuer offene Growth-Entscheidungen**
  („Optimierung (2)"). Keine Badges fuer Pipeline/Experimente.
- V2-Perspektive (nur notiert, nicht V1): die heutigen Titel-Vorschlaege
  koennen spaeter zu normalen Entscheidungs-Karten werden — Konvergenz statt
  Dauer-Doppelstruktur.
- KEINE eigene App, KEIN eigener Login, keine neuen Seiten-Routen — nur
  JSON-Endpoints des Growth-Backends unter `/api/v1/growth/...`.

## 2. Informations-Hierarchie der Seite (oben → unten)

```text
[1] GESCHAEFTSLAGE   – 8 KPI-Karten (.kpis), immer sichtbar
[2] ENTSCHEIDUNGEN   – 0..5 Karten; DER Kern der Seite
[3] PIPELINE         – kompakte Tabelle, standardmaessig ZUGEKLAPPT
[4] EXPERIMENTE      – kompakte Tabelle, standardmaessig ZUGEKLAPPT
[5] GELERNT          – knappe Lehrsaetze, standardmaessig ZUGEKLAPPT
```

Auf-/Zuklappen mit dem bestehenden `<details>`-Akkordeon-Muster (OHNE
::before-Pfeil-Symbole — die wurden auf Nutzerwunsch entfernt; Encoding-Vorfall
0x15). Zustand je Abschnitt in `localStorage` merken.

## 3. Abschnitt GESCHAEFTSLAGE (Business Health)

Acht `.kpi`-Karten in bestehender Optik (label / val / sub). Pflichtprinzip:
**Herkunfts-Ehrlichkeit** — jede Zahl nennt in der `sub`-Zeile ihre Datenbasis;
unvollstaendige Basis wird offen angezeigt (Eiserne Regel: Finanzzahlen nie
schaetzen; unbekannt heisst „unbekannt").

| Karte | Wert | sub-Zeile (Beispiel) | Warnzustand (`warn`/`neg`) |
|---|---|---|---|
| Umsatz 30 T | Summe `Sale.price_eur` abgeschlossener Verkaeufe. ACHTUNG: `price_eur` IST die Zeilensumme inkl. Versandanteil — NIE mit `quantity` multiplizieren (Codex-Snapshot-Vorfall 18.08.) | „ohne Storno/Erstattung (§ 19)" | — |
| Deckungsbeitrag (abgeschlossen) | Umsatz − echte Gebuehren (`fee_eur_actual`) − echter EK (`cost_source` api/receipt/manual), NUR Verkaeufe mit belegten Kosten | „aus N von M Verkaeufen mit echten Kosten" | Abdeckung < 90 % |
| Deckungsbeitrags-Quote | Deckungsbeitrag / zugehoeriger Umsatz | „nur belegte Verkaeufe" | < 15 % |
| Gebuehren-Last | echte Gebuehren / Umsatz (nur Zeilen mit `fee_eur_actual`) | „echte Gebuehren, N Zeilen" | > 25 % |
| EK-Anteil | belegter Einkauf / Umsatz | „Herkunft: api/beleg/manuell" | > 55 % |
| Aktive Listings | Anzahl aktiv | „davon X Eigenbestand" | — |
| Gewinner-Konzentration | Umsatzanteil der Top-5-Listings (30 T) | „Top 5 von N Listings" | > 60 % |
| Reife Ladenhueter | aktive Listings aelter 60 T ohne Verkauf | „aelter als 60 Tage" | > 30 % des Bestands |

Klick auf eine Karte oeffnet den DETAILS-Drawer (Abschnitt 8) mit Definition,
Datenherkunft, einfacher Zeitreihe (Inline-SVG, keine Bibliothek) und den
zugrunde liegenden Zeilen (verlinkt in die bestehenden Tabs). Karten sind auch
ohne Klick vollstaendig verstaendlich.

## 4. Abschnitt ENTSCHEIDUNGEN (Herzstueck)

Nur Empfehlungen, die das Backend als **ausreichend verifiziert** markiert.
Sortierung: erwarteter Wert × Dringlichkeit. **Maximal 5 Karten gleichzeitig**
— der Rest wartet in der Pipeline als „bereit zur Pruefung".

### 4.1 Karten-Aufbau (eine Karte = eine Entscheidung)

```text
┌──────────────────────────────────────────────────────────────────┐
│ [Typ-Badge]  Empfehlung in EINEM Klartext-Satz                    │
│ Warum: ein Satz.                                                  │
│ 💶 Erwartet: +X–Y €/Monat  🎯 Sicherheit: hoch/mittel  ⚠ Risiko: … │
│ Noch unbekannt: ein ehrlicher Satz (oder „nichts Wesentliches").  │
│ Bei Freigabe passiert: EXAKTER Effekt + ausfuehrender Pfad.       │
│             [✓ Freigeben]   [✕ Ablehnen]   [Details]              │
└──────────────────────────────────────────────────────────────────┘
```

Feld-Regeln:
- **Empfehlungs-Satz:** Verb zuerst, konkret („Preis von 12,95 € auf 14,95 €
  anheben bei ‚…Titel…'"). Nie Systemjargon.
- **Erwarteter Wert:** €-Spanne oder ehrlich „unbekannt" — NIE vorgetaeuschte
  Praezision. Geschaetzte Werte tragen sichtbar „geschaetzt".
- **Sicherheit:** dreistufig (hoch/mittel); „niedrig" wird gar nicht erst als
  Karte gezeigt. Tooltip erklaert die Stufe.
- **Risiko:** das konkrete Verlust-Szenario in einem Satz.
- **Evidenz:** NICHT auf der Karte — im Drawer als Liste echter Belege
  (Datenzeilen, Experiment-IDs, Quellen-Links).
- **„Bei Freigabe passiert":** MUSS den ausfuehrenden BESTANDS-Pfad nennen
  (z. B. „aendert den eBay-Preis ueber den Preis-Check-Mechanismus"), damit
  klar ist: kein neuer Automatismus.

### 4.2 Knoepfe & Freigabefluss

- **[✓ Freigeben]** (`.btn sm primary`): Bei Geld-/Live-Wirkung folgt IMMER ein
  Bestaetigungsschritt im Drawer mit exakten Vorher/Nachher-Werten
  (Zwei-Schritte-Prinzip wie beim Bestellen — Eiserne Regel 10: EIN Klick darf
  nie direkt Geld bewegen). Danach Karte → „⏳ wird ausgefuehrt …" (Buttons
  gesperrt) → „✓ umgesetzt" mit Ergebnis-Zeile. Fehler: Klartext +
  Selbsthilfe-Weg + [↻ erneut] (Muster `createIdea`-Statuskette).
- **[✕ Ablehnen]**: optionales Grund-Feld (ein Klick ohne Grund erlaubt);
  Ablehnung = Lern-Signal ans Backend; Karte verschwindet sofort.
- **[Details]**: oeffnet den Drawer.
- Alle Aktionen sind POSTs mit SERVER-seitiger Policy-Pruefung — die UI ist nie
  die Durchsetzungsinstanz.

### 4.3 Leerzustand

> „✓ Keine Entscheidungen offen — das System beobachtet weiter.
> Naechster Analyse-Lauf: HH:MM." (`.empty`-Muster, ruhig und positiv)

## 5. Abschnitt PIPELINE

Zweck: Vertrauen durch Sichtbarkeit — WAS untersucht das System gerade?
Kompakte Tabelle (kein Kanban — passt nicht zur bestehenden Optik):

| Chance | Typ | Stufe | seit | naechster Schritt |

Stufen-Badges (deutsche Label-Map): 🔍 Entdeckt · 📊 In Analyse · 🧪 In
Pruefung · ✅ Bereit zur Pruefung. „Bereit" ist der Wartepuffer fuer Abschnitt 4.
Zeilen-Klick → Drawer. Filter: einfacher `<select>` nach Stufe (Muster
`orderFilter`); zusaetzliche Option „verworfen" blendet automatisch abgelehnte
Chancen ein (Transparenz ohne Dauer-Laerm).

Leerzustand: „Die Pipeline ist leer — der naechtliche Scanner fuellt sie
automatisch." Warnzeile ueber der Tabelle (`.neg`), wenn der letzte Lauf
fehlschlug: „⚠ Letzter Analyse-Lauf fehlgeschlagen am … — Details im
Aktivitaets-Log."

## 6. Abschnitt EXPERIMENTE

| Experiment | Hypothese (ein Satz) | Status | Laufzeit | Ergebnis |

Status-Badges: 🟢 laeuft · 🏆 gewonnen · ❌ verloren · ➖ unklar. „Ergebnis"
nennt Kennzahl + Richtung („+12 % Klicks", „kein messbarer Effekt").
V1-Grundsatz (s. Policy): Jedes Experiment, das etwas an eBay VERAENDERT, wurde
vorher als Entscheidung freigegeben — hier steht Freigegebenes im Vollzug plus
reine Beobachtungs-Experimente. Drawer je Zeile: Metrik-Verlauf, Start-/
Endkriterium, **[Experiment stoppen]** (mit Bestaetigung; Stopp = Ruecksetzung
auf den vorherigen Zustand ueber DENSELBEN Bestands-Pfad, der die Aenderung
machte). Leerzustand: „Keine Experimente aktiv."

## 7. Abschnitt GELERNT (Recent Learning)

Maximal 10 Eintraege, je EIN Satz Geschaefts-Lehre + Datum + Quell-Link (in den
Drawer). Beispiel: „Auto-Detailing-Sets verkaufen sich mit ‚Geschenk'-Keyword
im Titel deutlich haeufiger (Experiment #14, Aug 2026)." Keine Logs, keine
Metrik-Tapeten — Lehrsaetze. Aeltere hinter „alle anzeigen".

## 8. DETAILS-Drawer (EIN Muster fuer alles)

Rechtsseitiger Overlay-Drawer (~480 px, mobil 100 %) im Stil der bestehenden
Dialoge; Schliessen per ✕ / ESC / Hintergrund-Klick. Inhalt je Kontext:

- **Entscheidung:** voller Text · nachvollziehbarer Rechenweg des erwarteten
  Werts (Zeilen, keine Formel-Prosa) · Evidenz-Liste mit Links in bestehende
  Tabs · „Noch unbekannt" ausfuehrlich · Risiko-Szenario · Pruef-Historie ·
  bei Geld-Wirkung der BESTAETIGUNGSBLOCK (Vorher/Nachher + [Jetzt ausfuehren]).
- **KPI:** Definition in einem Satz · Datenherkunft (Tabellen/Felder,
  Ausschluesse) · Mini-Zeitreihe · „Zahl wirkt falsch?"-Hinweis mit Verweis auf
  den zustaendigen Tab.
- **Pipeline/Experiment:** Verlauf, Roh-Kennzahlen, beteiligte Objekte.

Der Drawer ist die EINZIGE Stelle mit Komplexitaet.

## 9. Warn- und Fehlerzustaende

- Growth-Backend aus/nicht erreichbar: Tab zeigt genau EINE Karte
  „Growth Engine ist nicht aktiv" mit Klartext-Grund — nie halbleere UI.
- Veraltete Empfehlung (Datenlage geaendert): Karte ausgegraut, Badge
  „veraltet — wird neu geprueft", Buttons gesperrt. Der SERVER prueft bei jeder
  Freigabe final die Aktualitaet — die UI fuehrt nie eine veraltete Freigabe aus.
- Teil-Ausfuehrung: ehrlich „teilweise umgesetzt (2/3)" + was fehlt + was der
  Besitzer tun kann.
- Fehlertexte: deutsch, Selbsthilfe-Weg, 6,2-s-Toast UND persistente Anzeige an
  der Karte (Toast allein reicht bei Entscheidungen nicht).

## 10. Implementierungs-Hinweise Frontend

- EIN Lade-Endpoint `GET /api/v1/growth/overview` liefert alle Abschnitte
  (Muster `loadOrders`); Polling nur waehrend laufender Ausfuehrung (Muster
  `_importPoll`, 8 s, selbststoppend).
- Namen im Bestandsstil: `loadGrowth()`, `growthApprove(id)`,
  `growthReject(id)`, `growthDrawer(typ, id)`; `loadGrowth()` wird aus dem
  bestehenden Loader des Optimierung-Tabs mit aufgerufen (BEIDE Designs) —
  ein Growth-Backend-Ausfall darf die bestehenden Optimierungs-Werkzeuge
  nie mitreissen (eigener try-Block).
- Keine neuen Abhaengigkeiten/Webfonts; Zeitreihen als Inline-SVG.
- Nur UTF-8-Literale (0x15-Vorfall), Emojis direkt, kein CSS-Escape fuer
  Symbole. Jeder Button mit ELI5-`title`-Tooltip (Konvention).
- Platzierungs-Frage ist vom Besitzer entschieden (19.08.): Integration in den
  Optimierung-Tab, kein neuer Reiter. Weitere UI-Detailfragen (z. B. Badge-
  Optik) im Zweifel kurz vorschlagen statt still entscheiden (Memory-Regel).
