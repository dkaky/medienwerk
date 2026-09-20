<#
  Richtet das Intranet auf einem gemieteten Linux-Server ein - in einem Rutsch.

  Voraussetzung: ein Server (Ubuntu) mit IP-Adresse und Root-Passwort.
  Aufruf:        Doppelklick auf INTRANET-EINRICHTEN.bat im Projektordner.

  Erstmalig:     alles hochladen (Programm, Daten, Zugangsdaten) und starten.
  Spaeter:       mit -NurProgramm nur neue Programmdateien hochladen. Daten und
                 Zugangsdaten auf dem Server bleiben dabei unberuehrt.
  -NurVorbereiten baut das Paket nur zusammen und zeigt es an (ohne Server).
#>
param(
    [string]$Ip,
    [string]$Domain = "intranet.medienwerk-kw.de",
    [string]$Benutzer = "medienwerk",
    [string]$Passwort,
    [switch]$NurProgramm,
    [switch]$NurVorbereiten
)
$ErrorActionPreference = "Stop"
$Wurzel = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Wurzel

function Frage($text, $vorgabe) {
    $a = Read-Host "$text [$vorgabe]"
    if ([string]::IsNullOrWhiteSpace($a)) { $vorgabe } else { $a.Trim() }
}

foreach ($werkzeug in "tar", "ssh", "scp", "robocopy") {
    if (-not (Get-Command $werkzeug -ErrorAction SilentlyContinue)) {
        throw "'$werkzeug' fehlt. Windows 10/11 bringt es mit; bitte 'Optionale Features > OpenSSH-Client' aktivieren."
    }
}

if (-not $NurVorbereiten -and -not $Ip) { $Ip = Read-Host "IP-Adresse des Servers" }
if (-not $NurVorbereiten -and [string]::IsNullOrWhiteSpace($Ip)) { throw "Ohne IP-Adresse geht es nicht." }
if (-not $NurProgramm) {
    if (-not $Passwort) {
        $sicher = Read-Host "Passwort fuers Intranet (Benutzername: $Benutzer)" -AsSecureString
        $Passwort = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sicher))
    }
    if ($Passwort.Length -lt 8) { throw "Das Passwort braucht mindestens 8 Zeichen." }
    if ($Passwort -match '[\$\s"''`\\]') { throw "Im Passwort bitte keine Leerzeichen, Anfuehrungszeichen oder Sonderzeichen wie Dollar, Backslash und Backtick verwenden." }
}

# ---------------------------------------------------------------- Paket zusammenstellen
$Stage = Join-Path ([IO.Path]::GetTempPath()) ("medienwerk-paket-" + [guid]::NewGuid().ToString("N"))
$Ziel = Join-Path $Stage "deploy\intranet"
New-Item -ItemType Directory -Force -Path $Ziel | Out-Null
try {
    function Kopiere($quelle, $ziel, $ausser) {
        $null = robocopy $quelle $ziel /E /NFL /NDL /NJH /NJS /NP /XD @($ausser) /XF *.pyc *.bak *.log
        if ($LASTEXITCODE -ge 8) { throw "Kopieren von $quelle fehlgeschlagen (robocopy $LASTEXITCODE)" }
    }
    Kopiere "app" (Join-Path $Stage "app") @("__pycache__")
    New-Item -ItemType Directory -Force -Path (Join-Path $Stage "docs") | Out-Null
    Copy-Item "docs\PROMPT-REGELN-BILD.md" (Join-Path $Stage "docs")
    Copy-Item "requirements.txt", ".dockerignore" $Stage
    foreach ($datei in "Dockerfile", "docker-compose.yml", "Caddyfile", "env.beispiel") {
        Copy-Item (Join-Path $PSScriptRoot $datei) $Ziel
    }

    if (-not $NurProgramm) {
        # Daten mitnehmen, aber weder das Browserprofil (78 MB Zwischenspeicher) noch Testreste.
        Kopiere "data" (Join-Path $Ziel "daten") @("browser_profile", "pytest-tmp", "__pycache__")

        # .env fuer den Server: die Werte vom PC, plus die Einstellungen fuer den Betrieb im Internet.
        $ueberschrieben = [ordered]@{
            APP_ENV = "production"; DASHBOARD_USER = $Benutzer; DASHBOARD_PASSWORD = $Passwort
            INTRANET_DOMAIN = $Domain; STUDIO_ENABLED = "true"; BACKGROUND_JOBS_ENABLED = "true"
        }
        $zeilen = New-Object System.Collections.Generic.List[string]
        $gesetzt = @{}
        if (-not (Test-Path ".env")) { throw "Die .env im Projektordner fehlt - daraus kommen die eBay- und KI-Zugaenge." }
        foreach ($z in Get-Content ".env" -Encoding UTF8) {
            if ($z -match '^\s*([A-Z0-9_]+)\s*=') {
                $schluessel = $Matches[1]
                if ($ueberschrieben.Contains($schluessel)) {
                    $zeilen.Add("$schluessel=$($ueberschrieben[$schluessel])"); $gesetzt[$schluessel] = $true; continue
                }
            }
            $zeilen.Add($z)
        }
        foreach ($schluessel in $ueberschrieben.Keys) {
            if (-not $gesetzt.ContainsKey($schluessel)) { $zeilen.Add("$schluessel=$($ueberschrieben[$schluessel])") }
        }
        [IO.File]::WriteAllLines((Join-Path $Ziel ".env"), $zeilen, (New-Object Text.UTF8Encoding($false)))
    }

    $Archiv = Join-Path ([IO.Path]::GetTempPath()) "medienwerk.tgz"
    tar czf $Archiv -C $Stage .
    if ($LASTEXITCODE -ne 0) { throw "Paket konnte nicht gepackt werden." }
    $mb = [math]::Round((Get-Item $Archiv).Length / 1MB, 1)
    Write-Host "Paket bereit: $mb MB" -ForegroundColor Green

    if ($NurVorbereiten) {
        Write-Host "Inhalt (oberste Ebene):"
        tar tzf $Archiv | Where-Object { $_ -match '^\./[^/]+/?$|deploy/intranet/[^/]+/?$' } | ForEach-Object { "  $_" }
        Write-Host "Das Testpaket wird jetzt wieder geloescht."
        return
    }

    # ------------------------------------------------------------ auf den Server
    $server = "root@$Ip"
    $ssh = @("-o", "StrictHostKeyChecking=accept-new")
    Write-Host "`nVerbinde mit $Ip - das Server-Passwort wird gleich abgefragt (mehrmals)." -ForegroundColor Cyan

    if (-not $NurProgramm) {
        $vorhanden = (ssh @ssh $server "test -d /opt/medienwerk/deploy/intranet/daten && echo JA || echo NEIN").Trim()
        if ($vorhanden -eq "JA") {
            throw "Auf dem Server laufen schon Daten. Zum Aktualisieren bitte mit -NurProgramm aufrufen, sonst wuerden sie ueberschrieben."
        }
    }
    scp @ssh $Archiv "${server}:/root/medienwerk.tgz"
    if ($LASTEXITCODE -ne 0) { throw "Hochladen fehlgeschlagen." }

    $fernbefehl = @'
set -e
export DEBIAN_FRONTEND=noninteractive
if ! command -v docker >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq docker.io docker-compose-v2 ufw >/dev/null
fi
ufw allow 22 >/dev/null; ufw allow 80 >/dev/null; ufw allow 443 >/dev/null; ufw --force enable >/dev/null
mkdir -p /opt/medienwerk
tar xzf /root/medienwerk.tgz -C /opt/medienwerk
rm /root/medienwerk.tgz
chmod 600 /opt/medienwerk/deploy/intranet/.env
cd /opt/medienwerk/deploy/intranet
docker compose up -d --build
docker compose ps
'@
    ssh @ssh $server $fernbefehl
    if ($LASTEXITCODE -ne 0) { throw "Einrichten auf dem Server fehlgeschlagen." }

    Write-Host "`nFertig. Der Server laeuft." -ForegroundColor Green
    if (-not $NurProgramm) {
        Write-Host @"

Noch zwei Handgriffe:
 1. Strato: Domain $($Domain -replace '^[^.]+\.', '') > DNS-Einstellungen > neuer A-Record
    Name '$($Domain.Split('.')[0])'  Wert '$Ip'   (www bleibt unveraendert)
 2. Lovable: den Satz aus ANLEITUNG.md (Schritt 7) in den Chat kopieren.

Danach ist das Intranet unter https://$Domain erreichbar (Benutzer: $Benutzer).
Wichtig: Das Dashboard auf diesem PC ab jetzt nicht mehr starten - der Server ist der neue Stand.
"@
    }
}
finally {
    Remove-Item -Recurse -Force $Stage -ErrorAction SilentlyContinue
    if ($Archiv) { Remove-Item -Force $Archiv -ErrorAction SilentlyContinue }
}
