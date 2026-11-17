FROM python:3.12-slim

# Wird von der CI aus Git-Tag und Commit gesetzt.
ARG APP_VERSION=dev
ARG GIT_SHA=

LABEL org.opencontainers.image.title="Gutscheinkontor" \
      org.opencontainers.image.description="Gutscheine verwalten und rechtzeitig vor Ablauf per E-Mail erinnern. Self-Hosting für Homelabs." \
      org.opencontainers.image.source="https://github.com/teitech/Gutscheinkontor" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_VERSION=${APP_VERSION} \
    GIT_SHA=${GIT_SHA} \
    DATA_DIR=/data

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Kein Root im Container. /data gehört dem App-Benutzer: Ein benanntes Docker-Volume übernimmt
# diese Besitzer beim ersten Anlegen. Bei einem Bind-Mount muss der Ordner dem Benutzer 10001 gehören.
RUN useradd --system --uid 10001 --no-create-home app \
    && mkdir /data && chown app:app /data
USER app

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# - exec: uvicorn wird Hauptprozess und bekommt SIGTERM direkt (sauberes, schnelles Beenden).
# - --no-access-log: Einladungs- und Reset-Links enthalten ihren Token im Pfad. Sie sollen nicht im
#   Container-Log landen (der Reverse Proxy führt sein eigenes Log).
# - --no-server-header: verrät nicht, welche Software läuft.
# - TLS übernimmt der Reverse Proxy. Proxy-Header gelten nur von FORWARDED_ALLOW_IPS.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log --no-server-header --proxy-headers --forwarded-allow-ips=\"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]
