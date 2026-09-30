FROM python:3.12.14-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=docker:29.8.1-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:29.8.1-cli /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY deployer deployer
ENV PYTHONUNBUFFERED=1
CMD ["gunicorn", "-b", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "120", "app:app"]
