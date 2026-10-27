FROM python:3.11-slim

WORKDIR /rewind

RUN apt-get update && apt-get install -y --no-install-recommends \
      bash curl ca-certificates \
    && curl -fsSL "https://download.docker.com/linux/static/stable/$(uname -m)/docker-27.5.1.tgz" \
      | tar -xz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && chmod +x /usr/local/bin/docker \
    && rm -rf /tmp/docker /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY templates ./templates
COPY static ./static

ENV REWIND_DATA=/data \
    REWIND_NETWORK=rollbackai \
    OLLAMA_URL=http://host.docker.internal:11434 \
    OLLAMA_MODEL=qwen2.5-coder:7b \
    PORT=8090 \
    VERIFY_WINDOW_SECONDS=60 \
    SAMPLE_INTERVAL_SECONDS=5 \
    MAJORITY_WINDOW=3

EXPOSE 8090

VOLUME ["/data"]

HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health')" || exit 1

CMD ["gunicorn", "-b", "0.0.0.0:8090", "-w", "1", "--threads", "8", "server:app"]
