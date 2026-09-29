FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=docker:cli /usr/local/bin/docker /usr/local/bin/docker
RUN pip install --no-cache-dir flask gunicorn requests pyjwt cryptography
WORKDIR /app
COPY app.py .
COPY templates templates
# one worker so per-app deploy locks work
CMD ["gunicorn", "-b", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "0", "app:app"]
