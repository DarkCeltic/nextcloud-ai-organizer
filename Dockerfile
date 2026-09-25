FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

COPY requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /app/requirements.txt

COPY exapp /app/exapp
COPY python_organizer_local_llm /app/python_organizer_local_llm
COPY config.example.yaml /app/config.yaml
COPY appinfo /app/appinfo

EXPOSE 23000

CMD ["sh", "-c", "uvicorn exapp.main:app --host ${APP_HOST:-0.0.0.0} --port ${APP_PORT:-23000}"]