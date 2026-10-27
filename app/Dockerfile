FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_VERSION=1.0.0 \
    FAULT_MODE=none \
    PORT=8080

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py catalog.py seed.py ./
COPY templates ./templates
COPY static ./static
COPY data ./data

ENV REWIND_PUBLIC_URL=http://127.0.0.1:8090

EXPOSE 8080

HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')" || exit 1

CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "--threads", "4", "app:app"]
