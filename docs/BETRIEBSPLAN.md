# Betriebsplan POD Shop

Stand: 08.09.2026. Ziel ist ein sicherer, messbarer Betrieb ohne unkontrollierte
Marktplatz-, Beschaffungs- oder Bildgenerierungsaktionen.

## Phase 0 – Lokal lauffähig machen

- [x] Isolierte Python-Umgebung `.venv` anlegen.
- [x] Abhängigkeiten aus `requirements.txt` installieren.
- [x] Lokale `.env` im Mock-Modus anlegen; Studio, Growth und Bildbudget bleiben aus.
- [ ] Vollständige Testsuite erfolgreich abschließen.
- [ ] Lokalen Server starten und `/health` sowie Dashboard automatisiert prüfen.

Erfolgskriterium: Das Dashboard ist nur auf `127.0.0.1:8030` erreichbar und
alle externen Integrationen bleiben im Mock-Modus.

## Phase 1 – Nachvollziehbarkeit und Sicherung

- [ ] Git-Repository mit sauberer Ausgangsbasis einrichten oder die Originalhistorie
  wieder anbinden.
- [ ] Verschlüsseltes externes Backup für Code, SQLite-Daten und Belege einrichten.
- [ ] Wiederherstellung aus einem Backup testen.
- [ ] Laufzeitprotokolle und eine einfache Betriebsstatusseite festlegen.

Erfolgskriterium: Jeder Zustand ist versioniert, gesichert und wiederherstellbar.

## Phase 2 – Datenqualität vor Automatisierung

- [ ] Echte eBay-Zugangsdaten ausschließlich lesend hinterlegen.
- [ ] Verkäufe, Gebühren, Live-Preise und Listing-Statistiken importieren.
- [ ] Lieferantenkosten, Stornos, Erstattungen und Tracking gegen die Verkäufe prüfen.
- [ ] Datenlücken sichtbar machen: unbekannte Gebühren, EK, Lieferzeit und Varianten.

Erfolgskriterium: Deckungsbeitrag wird nur als bestätigt angezeigt, wenn Erlös,
eBay-Gebühr und Einkaufskosten belegbar sind.

## Phase 3 – Entscheidungen vorbereiten

- [ ] Tagesansicht für Ausnahmen: Storno, fehlender EK, Lieferproblem, Preisdrift,
  Ausverkauf und fehlender Beleg.
- [ ] Portfolioanalyse nach Deckungsbeitrag, Stornoquote, Lieferzeit, Kategorie und
  Quelle ausführen.
- [ ] Nur überprüfbare Chancen als Empfehlung ausgeben; keine automatische Änderung.
- [ ] Je Hypothese ein kleines, reversibles Experiment mit Erfolgs- und Abbruchregel
  festlegen.

Erfolgskriterium: Der Inhaber entscheidet wenige gut belegte Fälle statt viele
unstrukturierte Einzelfälle.

## Phase 4 – POD-Studio schließen

- [ ] Motivbibliothek und Rechteprüfung vervollständigen.
- [ ] Für nicht druckfähige Motive einen kontrollierten Hochskalierungs- oder
  Neuerzeugungsweg bauen.
- [ ] Printify ausschließlich als Entwurf anbinden; Veröffentlichung bleibt manuell.
- [ ] Druck-, Kosten- und Rechtscheck vor jeder Produktfreigabe erzwingen.

Erfolgskriterium: Motiv → druckfähige Datei → Printify-Entwurf → manuelle
Freigabe ist durchgängig, nachvollziehbar und kostenbegrenzt.

## Phase 5 – Kontrollierter Live-Betrieb

- [ ] Rechtstexte, LUCID, Steuer-/Belegprozess und Marketplace-Policy prüfen.
- [ ] Dashboard-Zugangsschutz, HTTPS, Server-Backups und Monitoring einrichten.
- [ ] Zuerst lesende Integrationen, dann einzelne manuell bestätigte Entwürfe aktivieren.
- [ ] Nach jedem Schritt Kennzahlen, Fehlerraten und Rückabwicklung prüfen.

Erfolgskriterium: Keine externe Schreibaktion erfolgt ohne nachvollziehbare
Mensch-Freigabe; Rückabwicklung und Audit-Spur sind vorhanden.

## Entscheidungen des Inhabers (erst in Phase 2/5 nötig)

1. Darf eine lesende eBay-Anbindung mit den eigenen Zugangsdaten eingerichtet werden?
2. Wo soll das verschlüsselte externe Backup liegen?
3. Welche Rechts-/Steuerberatung gibt die Freigabe für den ersten echten Verkauf?
4. Welche einzelnen Entwürfe dürfen nach Prüfung live gehen?
