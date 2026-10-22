FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

# WITH_AUTH=true : installe aussi auth-client (production, AUTH_ENABLED=true)
ARG WITH_AUTH=false

COPY requirements.txt requirements-auth.txt ./
RUN if [ "$WITH_AUTH" = "true" ]; then \
        apt-get update && apt-get install -y --no-install-recommends git \
        && rm -rf /var/lib/apt/lists/* \
        && pip install --no-cache-dir -r requirements-auth.txt; \
    else \
        pip install --no-cache-dir -r requirements.txt; \
    fi

COPY app ./app
COPY wsgi.py ./

# Dossier des fichiers chiffrés : monté en volume par docker-compose (voir docker-compose.yml)
RUN useradd --system --no-create-home appuser \
    && mkdir -p /data/uploads \
    && chown appuser /data/uploads
USER appuser

ENV PORT=5000 \
    UPLOAD_FOLDER=/data/uploads
EXPOSE ${PORT}

# Timeout plus large que celui de gunicorn par défaut (30 s) : un gros upload/download doit pouvoir aller au bout.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT} --workers ${GUNICORN_WORKERS:-2} --timeout ${GUNICORN_TIMEOUT:-120} --access-logfile - wsgi:app"]
