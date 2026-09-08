# AliExpress-Zugang einrichten (Schritt fuer Schritt)

Damit Druckhelden Produktdaten von AliExpress holen kann (Preis, Titel, Bilder,
Versandkosten), braucht das Projekt einen eigenen API-Zugang.

> **Wichtig:** Die Zugangsdaten aus dem eBay-Projekt duerfen hier NICHT verwendet
> werden. Das ist ein anderes Gewerbe. Druckhelden braucht eine eigene App auf ein
> eigenes AliExpress-Konto.

Der Ablauf hat drei Teile. Teil A dauert am laengsten, weil AliExpress die App
freischalten muss (erfahrungsgemaess ein bis mehrere Werktage). Teil B und C sind in
zehn Minuten erledigt.

---

## Teil A: App anlegen und freischalten lassen

1. Oeffne im Browser: **https://openservice.aliexpress.com**
   Das ist die "AliExpress Open Platform", also das Entwicklerportal.

2. Melde dich mit dem AliExpress-Konto an, das zu **Druckhelden** gehoert.
   Falls du dafuer noch kein eigenes Konto hast, lege zuerst eins an. Nutze nicht das
   Konto des eBay-Projekts.

3. Suche im Portal den Bereich, in dem man eine neue Anwendung anlegt. Er heisst je
   nach Sprachversion **"Console"**, **"App Management"** oder **"Create App"**.

4. Lege eine neue App an. Trage dabei ein:
   - **App Name:** `Druckhelden POD`
   - **App Type / Kategorie:** die Variante fuer **Dropshipping**. Achte darauf, dass
     die App Zugriff auf die **Dropshipping-APIs** (die Methoden, die mit `ds.`
     beginnen) bekommt. Ohne diese Berechtigung liefert die API spaeter keine
     Produktdaten.
   - **Callback URL / Redirect URI:** `https://localhost:8010/aliexpress-callback`
     Diese Adresse muss nicht wirklich existieren. Sie ist nur die Stelle, an die
     AliExpress dich nach dem Zustimmen weiterleitet. Du liest den Code dann einfach
     aus der Adresszeile ab.
     **Schreibe dir diese Adresse exakt so auf, wie du sie eintraegst.** Ein
     abweichendes Zeichen laesst Teil B fehlschlagen.

5. Speichere die App. Jetzt bekommst du zwei Werte:
   - **App Key** (eine Zahlenfolge)
   - **App Secret** (eine lange Buchstaben-Zahlen-Folge)

   Behandle das App Secret wie ein Passwort. Es gehoert nur in die `.env`, nie in eine
   Datei, die auf GitHub landet.

6. Warte auf die Freischaltung. Solange die App auf "pending" oder "under review"
   steht, funktionieren die Aufrufe noch nicht.

---

## Teil B: Werte eintragen

7. Oeffne die Datei `C:\Users\HP\Projekte\POD-Shop\.env` in einem Editor.

8. Ergaenze am Ende diese vier Zeilen und setze deine echten Werte hinter das
   Gleichheitszeichen (keine Anfuehrungszeichen, keine Leerzeichen drumherum):

   ```
   POD_ALIEXPRESS_APP_KEY=1234567
   POD_ALIEXPRESS_APP_SECRET=deinlangesgeheimnis
   POD_ALIEXPRESS_CALLBACK_URL=https://localhost:8010/aliexpress-callback
   POD_ALIEXPRESS_ACCESS_TOKEN=
   ```

   Die Callback-URL muss **zeichengenau** der entsprechen, die du in Schritt 4
   eingetragen hast. `ACCESS_TOKEN` bleibt vorerst leer, den fuellt Teil C.

9. Speichern und den Editor schliessen.

---

## Teil C: Zugangstoken holen

10. Oeffne PowerShell im Projektordner und lass dir den Zustimmungs-Link ausgeben:

    ```powershell
    cd C:\Users\HP\Projekte\POD-Shop
    .\.venv\Scripts\pod.exe ali-authurl
    ```

    Es wird eine lange Adresse ausgegeben, die mit
    `https://api-sg.aliexpress.com/oauth/authorize?...` beginnt.

    Kommt stattdessen eine Fehlermeldung, nennt sie genau die Variable, die in der
    `.env` noch fehlt. Dann zurueck zu Schritt 8.

11. Kopiere die Adresse, fuege sie in den Browser ein und druecke Enter.

12. Melde dich mit deinem AliExpress-Konto an und bestaetige die Zustimmung.

13. Danach landest du auf deiner Callback-Adresse. Die Seite selbst zeigt vermutlich
    einen Fehler oder nichts, **das ist normal und richtig so**. Schau in die
    Adresszeile des Browsers. Dort steht etwas wie:

    ```
    https://localhost:8010/aliexpress-callback?code=3_1234_abcdefgh...
    ```

    Kopiere den Teil **hinter** `code=`, also `3_1234_abcdefgh...`.
    Falls dahinter noch ein `&` steht, kopiere nur bis vor das `&`.

14. Zurueck in PowerShell, Code einloesen (deinen Code statt `DEINCODE` einsetzen,
    die Anfuehrungszeichen mitschreiben):

    ```powershell
    .\.venv\Scripts\pod.exe ali-exchange "DEINCODE"
    ```

15. Bei Erfolg erscheint "Geschafft" plus der Ablageort des Tokens. Der Token liegt
    dann in `data\aliexpress_token.json` und wird von selbst verwendet.

    Zusaetzlich werden zwei Zeilen fuer die `.env` ausgegeben. Kopiere sie als
    Sicherung dort hinein (die leere `POD_ALIEXPRESS_ACCESS_TOKEN=`-Zeile aus
    Schritt 8 ersetzen).

---

## Wenn etwas schiefgeht

| Meldung | Ursache | Was tun |
|---|---|---|
| `In der .env fehlt noch: ...` | Variable nicht gesetzt | Schritt 8 pruefen, Datei gespeichert? |
| `Invalid signature` | App Secret falsch kopiert | Secret erneut aus dem Portal kopieren, auf Leerzeichen achten |
| Fehler mit `code` im Text | Code abgelaufen oder schon benutzt | Ein Code gilt nur einmal und nur kurz. Ab Schritt 10 wiederholen |
| `redirect_uri mismatch` | Callback-URL weicht ab | Portal und `.env` muessen zeichengenau uebereinstimmen |
| Aufruf laeuft, liefert aber keine Produktdaten | App noch nicht freigeschaltet oder Dropshipping-Berechtigung fehlt | Im Portal den Status der App pruefen |

Der Token laeuft nach einiger Zeit ab. Dann Teil C ab Schritt 10 einfach wiederholen.

---

## Was danach funktioniert

Sobald der Token liegt, kann Druckhelden Produktdaten selbst holen. Damit wird aus dem
Shop-Scan ein vollstaendiger Import: gefundene Artikel werden mit echtem Einkaufspreis,
Titel und Bildern angelegt, und die Preis-Engine rechnet den Verkaufspreis aus.

Ohne Token funktioniert der Shop-Scan trotzdem. Er listet dann die gefundenen
Produktnummern samt Links auf und trennt neu von bereits vorhanden.
