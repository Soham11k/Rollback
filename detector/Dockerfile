FROM python:3.11-slim

WORKDIR /detector

RUN apt-get update && apt-get install -y --no-install-recommends \
      bash curl ca-certificates ca-certificates \
    && ARCH="$(dpkg --print-architecture)" \
    && curl -fsSL "https://download.docker.com/linux/static/stable/$(uname -m)/docker-27.5.1.tgz" \
      | tar -xz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && chmod +x /usr/local/bin/docker \
    && rm -rf /tmp/docker /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY collect.py train.py monitor.py evaluate.py snapshot.py ./

# Bake latest monitor into the image; scenario also bind-mounts /project for overrides.

ENV MODE=idle \
    PROMETHEUS_URL=http://prometheus:9090 \
    MODEL_PATH=/models/baseline.joblib \
    PROJECT_ROOT=/project \
    METRICS_PORT=9091

EXPOSE 9091

CMD ["python", "monitor.py", "--mode", "idle"]
