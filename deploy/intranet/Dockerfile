# Medienwerk-Dashboard als Server-Abbild.
# Gebaut wird aus dem Projektordner (siehe docker-compose.yml, context: ../..).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Berlin

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY docs/PROMPT-REGELN-BILD.md ./docs/PROMPT-REGELN-BILD.md

# Alle veraenderlichen Daten (Datenbank, Motive, Belege, Schluessel) liegen unter /app/data
# und kommen als Volume von aussen - nie ins Abbild.
VOLUME /app/data
EXPOSE 8030

# --proxy-headers: hinter dem HTTPS-Proxy (Caddy) sieht die Anwendung sonst "http" und
# setzt das Login-Cookie ohne Secure-Merkmal.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8030", \
     "--proxy-headers", "--forwarded-allow-ips=*"]
