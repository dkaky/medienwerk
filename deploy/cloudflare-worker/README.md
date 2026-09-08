# eBay Marketplace Account Deletion — Cloudflare Worker

Dauerhaft erreichbarer, kostenloser HTTPS-Endpunkt für eBays Pflicht-Validierung.
Schaltet das eBay-**Production-Keyset** frei (Voraussetzung für die Sell-API).

## Deploy (Dashboard, ohne Kommandozeile)

1. Kostenloses Konto: **dash.cloudflare.com** → registrieren.
2. **Workers & Pages → Create → Workers → Create Worker** → Name z. B. `ebay-deletion` → **Deploy**.
   - Du erhältst eine URL wie `https://ebay-deletion.<dein-subdomain>.workers.dev`. **Notieren.**
3. **Edit code** → den Inhalt von [`worker.js`](worker.js) einfügen → **Deploy**.
4. **Settings → Variables and Secrets → Add**:
   - `VERIFICATION_TOKEN` = ein zufälliger String, 32–80 Zeichen (selbe Wert wie auf eBay).
   - `ENDPOINT_URL` = exakt die Worker-URL aus Schritt 2, **ohne** Slash am Ende.
   - **Deploy** (speichern).
5. Die Worker-URL + den Token auf **developer.ebay.com/my/push** (Production →
   Marketplace Account Deletion) eintragen → **Save**. eBay validiert → Keyset wird aktiv.

## Test
`https://<deine-worker-url>/?challenge_code=test123` im Browser öffnen →
es muss ein JSON `{"challengeResponse":"<64-stelliger hex>"}` erscheinen.
