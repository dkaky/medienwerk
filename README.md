# POD Design Studio

Lokale Werkstatt fuer eigene Print-on-Demand-Motive: Ideen sammeln, Grafiken
entwerfen, Drucktauglichkeit pruefen und Printify-Entwuerfe vorbereiten.

## Was die Anwendung macht

- eigene Motive und Prompt-Entwuerfe verwalten;
- Bildgenerierung nur hinter Rechtefilter und Tagesbudget ausfuehren;
- Druckformat und Aufloesung vor dem Produktentwurf pruefen;
- Printify-Produkte ausschliesslich als Entwurf anlegen;
- Marketplace-Seiten optional nur als Inspirations- und Trendsignal lesen.

Es gibt keinen AliExpress-, Dropshipping-, Produktimport-, Fulfillment- oder
automatischen Veroeffentlichungspfad.

## Lokal starten

`START-DASHBOARD.bat` doppelklicken. Danach oeffnet sich das Studio unter
`http://localhost:8030`.

Die lokale Konfiguration liegt in `.env`. Ohne `OPENAI_API_KEY`, `FAL_API_KEY`
und Printify-Zugangsdaten bleiben kostenpflichtige bzw. externe Schritte gesperrt.
`STUDIO_DAILY_BUDGET_USD=-1` bedeutet unbegrenzt; `0` sperrt die Erzeugung.

## Sicherheit

- Der Server lauscht nur auf `127.0.0.1`.
- Ein Printify-Aufruf braucht eine explizite Bestaetigung und erzeugt nur einen
  Entwurf, keine Veroeffentlichung.
- Die Sicherung vor der Bereinigung liegt unter
  `backups/pod-shop-vor-pod-bereinigung-20260908.zip`.
