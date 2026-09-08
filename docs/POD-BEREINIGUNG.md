# POD-Bereinigung

## Entfernt

- AliExpress-, AutoDS- und eBay-Dropshipping-Adapter;
- eBay-Listing-Link zwischen Studio und Handel;
- Handelsdatenmodell und Scheduler aus dem aktiven Anwendungspfad;
- Produkt-, Auftrags-, Repricing-, Monitoring- und Fulfillment-Routen;
- alte Dashboard-Navigation zu Listings, Bestellungen und Buchhaltung;
- lokale Laufzeitkonfiguration fuer Marketplace-Mocks.

## Beibehalten

- Designbibliothek, Rechtefilter, Kostenbremse und Bildgenerierung;
- Druckcheck und Printify-Entwurf;
- Motiv-Radar als reine, optionale Marktinspiration;
- Authentifizierung, lokale Datenbank und Logging.

## Historische Reste

Einige nicht erreichbare Altdateien (z. B. Buchhaltung und Analyse) bleiben
vorerst im Arbeitsordner, weil sie nicht eindeutig auf Dropshipping beschraenkt
sind. Sie werden von `app.main` nicht importiert und haben keine API-Routen.
Die bisherige `data/ebay_store.db` bleibt als lokaler Rückfallbestand erhalten;
die POD-App verwendet stattdessen `data/pod_studio.db`.
