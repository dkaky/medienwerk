# Intranet: Dashboard unter intranet.medienwerk-kw.de

Ziel: Das Dashboard läuft nicht mehr auf deinem PC, sondern auf einem kleinen Server und ist
jederzeit über `https://intranet.medienwerk-kw.de` erreichbar. Die Homepage
(www.medienwerk-kw.de bei Lovable) bekommt in der Fußzeile nur einen Link dorthin.

```
Fußzeile www.medienwerk-kw.de  ──Link──►  https://intranet.medienwerk-kw.de
   (Lovable)                               (dein Server, Login mit Passwort)
```

## Was du brauchst
- **Einen kleinen Linux-Server** (Ubuntu 24.04, 2 GB RAM, ca. 4–6 € im Monat), z. B. Hetzner Cloud
  CX22 oder ein Strato-VPS. Buchen musst du selbst; danach hast du eine **IP-Adresse** und
  einen **SSH-Zugang** (Benutzer `root` und ein Passwort oder Schlüssel).
- Zugang zu deiner **Strato-Domainverwaltung** (DNS-Einstellungen).
- Deine lokale `.env` (daraus schreibst du die Werte ab).

## Schritt 1: Adresse bei Strato anlegen
Strato → Domains → `medienwerk-kw.de` → DNS-Einstellungen → neuer **A-Record**:
Name `intranet`, Wert = die IP deines Servers. `www` bleibt unverändert (das ist Lovable).
Es dauert bis zu einer Stunde, bis die Adresse gilt.

## Schritt 2: Server vorbereiten (einmalig)
```bash
ssh root@DEINE-SERVER-IP
apt update && apt install -y docker.io docker-compose-v2 ufw
ufw allow 22 && ufw allow 80 && ufw allow 443 && ufw --force enable
mkdir -p /opt/medienwerk && exit
```

## Schritt 3: Programm hochladen (vom PC, im Projektordner)
Es werden nur die Programmdateien eingepackt - keine `.env`, keine Zugangsdaten, keine Historie, keine Daten:
```bash
tar czf medienwerk.tgz --exclude=__pycache__ --exclude=.env     app docs/PROMPT-REGELN-BILD.md requirements.txt .dockerignore deploy/intranet
scp medienwerk.tgz root@DEINE-SERVER-IP:/opt/medienwerk/
ssh root@DEINE-SERVER-IP "cd /opt/medienwerk && tar xzf medienwerk.tgz && rm medienwerk.tgz"
```
(Auf Windows geht der `tar`-Befehl in der PowerShell genauso; die Zeilenfortsetzung `\` dort weglassen und alles in eine Zeile schreiben.)

## Schritt 4: Daten mitnehmen (einmalig)
Dein bisheriger Stand (Motive, Angebote, Bestellungen, Belege, eBay-Schlüssel) liegt in `data/`:
```bash
scp -r data root@DEINE-SERVER-IP:/opt/medienwerk/deploy/intranet/daten
```

## Schritt 5: Zugangsdaten auf dem Server eintragen
```bash
ssh root@DEINE-SERVER-IP
cd /opt/medienwerk/deploy/intranet
cp env.beispiel .env
nano .env          # Werte aus deiner PC-.env abschreiben, DASHBOARD_PASSWORD selbst wählen (mind. 12 Zeichen)
chmod 600 .env
```
Ohne Passwort startet die Anwendung im Internet absichtlich nicht.

## Schritt 6: Starten
```bash
docker compose up -d --build
docker compose logs -f app       # Strg+C zum Beenden der Anzeige
```
Danach `https://intranet.medienwerk-kw.de` öffnen. Das HTTPS-Zertifikat holt sich der Server selbst.

## Schritt 7: Link in der Fußzeile (Lovable)
Im Lovable-Chat eingeben:

> Füge in der Fußzeile der Website einen unauffälligen Textlink „Intranet" hinzu. Er führt zu
> https://intranet.medienwerk-kw.de und öffnet in einem neuen Tab (target="_blank",
> rel="noopener noreferrer"). Er soll zu den anderen Fußzeilen-Links passen und nicht
> hervorgehoben werden.

## Danach unbedingt
- **Lokal nicht mehr benutzen.** Zwei Programme mit zwei Datenständen laufen auseinander. Auf dem PC
  `START-DASHBOARD.bat` nicht mehr starten (oder dort `BACKGROUND_JOBS_ENABLED=false` setzen).
- **Rückruf-Adressen umstellen**, falls sie auf `localhost` zeigen (Kontist, eBay-RuName): die neue
  Adresse ist `https://intranet.medienwerk-kw.de/...`.
- **Sicherung:** Der Ordner `/opt/medienwerk/deploy/intranet/daten` ist dein ganzer Betrieb. Die
  Server-Sicherung des Anbieters aktivieren (bei Hetzner ca. 20 % Aufpreis) oder regelmäßig kopieren.
- **Updates einspielen:** neue Dateien wie in Schritt 3 hochladen, dann
  `cd /opt/medienwerk/deploy/intranet && docker compose up -d --build`.

## Sicherheit in Kürze
- Alles außer der Anmeldeseite verlangt das Passwort (auch die API-Dokumentation).
- Nach 5 falschen Versuchen sperrt die Anmeldung die IP-Adresse zeitweise.
- Die Anwendung selbst ist aus dem Internet nicht direkt erreichbar, nur über HTTPS (Caddy).
- Die Seite wird von Suchmaschinen nicht indexiert (`noindex`).
- `.env` und `data/` liegen nur auf dem Server, nie im Programm-Abbild und nie in Git.
