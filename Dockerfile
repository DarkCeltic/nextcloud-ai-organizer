FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AI_ORGANIZER_CONFIG=/app/config.yaml \
    APP_HOST=0.0.0.0 \
    APP_PORT=23000

WORKDIR /app

# Install dependencies first so Docker can reuse this layer when only source changes.
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip \
    && pip install -r /app/requirements.txt

# Application source.
COPY main.py analyze.py apply.py file_action.py /app/
COPY organizer.py classifier.py scanner.py nextcloud.py database.py ollama.py /app/
COPY config.yaml /app/config.yaml
COPY static /app/static
COPY appinfo /app/appinfo

# AppAPI normally supplies APP_PORT dynamically. 23000 is only the fallback
# used by manual/local deployments.
EXPOSE 23000

# Do not bake Nextcloud or Ollama credentials into the image.
# AppAPI injects its own ExApp variables at deployment time.
CMD ["sh", "-c", "exec uvicorn main:app --host ${APP_HOST:-0.0.0.0} --port ${APP_PORT:-23000} --proxy-headers --forwarded-allow-ips='*'"]
