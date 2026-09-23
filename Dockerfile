FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

COPY requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /app/requirements.txt

# AI Organizer ExApp
COPY exapp /app/exapp

# Organizer backend
COPY python_organizer_local_llm /app/python_organizer_local_llm

# Configuration
COPY config.yaml /app/config.yaml

# Nextcloud ExApp metadata
COPY appinfo /app/appinfo

EXPOSE 23001

CMD ["uvicorn", "exapp.main:app", "--host", "0.0.0.0", "--port", "23001"]