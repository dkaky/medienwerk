# eBay-Löschendpunkt im Lovable-Projekt (medienwerk-kw.de)

Die Website läuft bei Lovable mit Supabase-Backend. Dort legen wir **eine
Serverfunktion und eine Tabelle** an. An der sichtbaren Website ändert sich nichts.

Adresse des Endpunkts danach:
`https://medienwerk-kw.de/api/public/ebay-deletion`

## 1. Auftrag in Lovable einfügen

Im Lovable-Projekt der Website den Chat öffnen und das hier einfügen. Danach den
Inhalt von `index.ts` und `tabelle.sql` (beide in diesem Ordner) direkt darunter.

```
Bitte lege eine Supabase Edge Function namens genau "ebay-deletion" an und ändere
sonst NICHTS an der Website.

1. Code der Funktion: exakt der Code aus index.ts unten, unverändert.
2. Datenbank: exakt das SQL aus tabelle.sql unten als Migration ausführen. Die
   Tabelle darf NICHT im Frontend verwendet werden und bekommt keine RLS-Policies.
3. In supabase/config.toml eintragen:
   [functions.ebay-deletion]
   verify_jwt = false
   (eBay ruft die Funktion ohne Supabase-Anmeldung auf.)
4. Diese drei Secrets anlegen und mich nach den Werten fragen:
   EBAY_VERIFICATION_TOKEN, EBAY_ENDPOINT_URL, EBAY_ABHOL_TOKEN
5. Die Funktion deployen.
```

## 2. Secrets eintragen, wenn Lovable fragt

Die Werte stehen in der `.env` im Projektordner:

| Secret in Lovable | Wert aus der `.env` |
|---|---|
| `EBAY_VERIFICATION_TOKEN` | `EBAY_IPN_VERIFICATION_TOKEN` |
| `EBAY_ENDPOINT_URL` | `EBAY_DELETION_ENDPOINT_URL` |
| `EBAY_ABHOL_TOKEN` | `EBAY_LOESCH_ABHOL_TOKEN` |

## 3. Prüfen, bevor eBay es prüft

```
.venv\Scripts\python.exe -m scripts.pruefe_ebay_endpunkt https://medienwerk-kw.de/api/public/ebay-deletion
```

## 4. Bei eBay eintragen

developer.ebay.com → Alerts & Notifications → **Marketplace Account Deletion**:
- E-Mail: deine Adresse
- Endpoint: die Adresse oben
- Verification Token: der Wert von `EBAY_IPN_VERIFICATION_TOKEN`
- **Save**

## 5. Meldungen abholen

Von Hand: `.venv\Scripts\python.exe -m scripts.hole_ebay_loeschmeldungen`
Automatisch: läuft mit, wenn `BACKGROUND_JOBS_ENABLED=true`.

## Tatsächliche Umsetzung (12.09.2026)

Lovable durfte in diesem Projekt **keine neuen Supabase-Funktionen** anlegen und hat den
Code stattdessen als **Server-Route der Website** umgesetzt:
`https://medienwerk-kw.de/api/public/ebay-deletion` — erreichbar erst nach dem Veröffentlichen.

- `supabase/config.toml` / `verify_jwt` entfällt dadurch.
- Beim Einfügen war ein Teil des Codes abgeschnitten; Lovable hat die Mitte nachgebaut.
  Deshalb **vor dem Eintragen bei eBay** von außen prüfen (Prüfcode, Abholen, Quittieren).
- Secret `EBAY_ENDPOINT_URL` muss **diese** Adresse enthalten, nicht die Supabase-Adresse —
  der Prüfcode wird damit berechnet.
