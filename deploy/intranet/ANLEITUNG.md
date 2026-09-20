# Intranet: Dashboard unter intranet.medienwerk-kw.de

Die Homepage (www.medienwerk-kw.de, Lovable) bekommt in der Fußzeile den Link „Intranet". Er führt zu
`https://intranet.medienwerk-kw.de`, dort meldet man sich mit Benutzername und Passwort an. Das Dashboard
läuft dafür auf einem kleinen Server (Lovable kann nur Webseiten ausliefern).

## In vier Schritten

**1. Server mieten** (einmalig, ca. 4–6 € im Monat): z. B. Hetzner Cloud „CX22" mit **Ubuntu 24.04**.
Du bekommst eine **IP-Adresse** und ein **Root-Passwort**.

**2. Doppelklick auf `INTRANET-EINRICHTEN.bat`** im Projektordner.
Es fragt nach der IP-Adresse und dem Intranet-Passwort (Benutzername: `medienwerk`) und erledigt den Rest:
Programm, Daten und Zugangsdaten hochladen, Server einrichten, starten. Das Server-Passwort wird dabei
mehrmals abgefragt. Der Upload ist rund 540 MB groß und dauert ein paar Minuten.

**3. Strato:** Domain `medienwerk-kw.de` → DNS-Einstellungen → neuer **A-Record**,
Name `intranet`, Wert = die IP-Adresse. `www` bleibt unverändert.

**4. Lovable:** Diesen Satz in den Chat kopieren:

> Füge in der Fußzeile der Website einen unauffälligen Textlink „Intranet" hinzu. Er führt zu
> https://intranet.medienwerk-kw.de und öffnet in einem neuen Tab (target="_blank",
> rel="noopener noreferrer"). Er soll zu den anderen Fußzeilen-Links passen.

## Danach
- Das Dashboard auf dem PC **nicht mehr starten** – der Server ist der neue Stand, zwei Stände laufen auseinander.
- Neue Programmversion einspielen: `INTRANET-EINRICHTEN.bat -NurProgramm` (Daten und Zugangsdaten auf dem
  Server bleiben unberührt).
- Rückruf-Adressen (Kontist, eBay-RuName), die auf `localhost` zeigen, auf `https://intranet.medienwerk-kw.de/...` umstellen.
- Sicherung: Die Server-Sicherung des Anbieters aktivieren. Der Ordner `/opt/medienwerk/deploy/intranet/daten` ist der ganze Betrieb.

## Sicherheit
- Alles außer der Anmeldeseite verlangt Benutzername und Passwort (auch die Programmschnittstelle).
- Nach 5 falschen Versuchen wird die IP-Adresse 15 Minuten gesperrt.
- HTTPS mit selbst erneuerndem Zertifikat; die Anwendung ist nicht direkt aus dem Internet erreichbar.
- Ein Passwort mit 8 Zeichen ist der Mindestwert. Für ein Dashboard mit eBay-Zugängen und Buchhaltung
  ist ein längeres Passwort deutlich sicherer.
