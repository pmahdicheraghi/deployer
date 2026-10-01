# Deployer

A Flask dashboard for deploying trusted GitHub/GitLab repositories to one Docker server, with Nginx and Let's Encrypt routing.

## Architecture

`app.py` is only the Gunicorn entry point. `deployer/web` contains authenticated routes and webhook handlers; routes save configuration and durable jobs. `deployer/storage` owns SQLite transactions and imports legacy JSON. `deployer/deployment` runs jobs, readiness checks, rollback and recovery. `deployer/integrations` contains bounded Git, Docker, GitHub and Nginx adapters. Templates live under `deployer/templates`.

One separate worker executes all mutations in queue order. SQLite claims plus an OS process lock prevent concurrent workers. Requests arriving during deployment coalesce into one follow-up deployment. Configuration is frozen atomically at job claim, and in-progress domains stay reserved even if saved settings change again. Builds for different apps are currently serialized as well.

## Setup

1. Copy `.env.example` to `.env` and set domains, ACME email, a strong admin password, and a random `SECRET_KEY`. Both web and worker need the same settings. A missing session secret prevents startup. Configure DNS before starting HTTPS provisioning.
2. Place the GitHub App private key in `github-app-private-key.pem`. For manual-only operation, create an empty regular file at that path and leave GitHub App settings blank. Compose requires the file to exist, and will not create a directory there.
3. Run `docker compose up -d --build` on the server.
4. Open the configured panel domain. New apps need a Dockerfile and must listen on `0.0.0.0` at their configured container port.

Docker socket access is privileged, including the dashboard's inspection connection. This remains a trusted-repository, single-admin tool. Secrets in `.env`, the private key, SQLite state and logs must stay private on the host.

## Deployment behavior

The worker fetches and builds before starting a uniquely named candidate. It honors a Docker health check, optionally checks an HTTP readiness path (2xx), or otherwise checks TCP connectivity. TCP alone is not a full application health check.

For internal services on a custom network, the Docker worker temporarily joins that network during readiness checks and disconnects afterward. Failed candidates have their last 100 startup log lines captured before rollback, with configured secrets redacted. Deleting an app clears its deployment log so recreating the same name starts with fresh history; its data volume is retained unless **Delete volume** is selected.

For PostgreSQL 16, use `postgres:16`, internal port `5432`, internal-only visibility, and an environment variable `POSTGRES_PASSWORD` with a strong password. Enable stateful storage with mount path `/var/lib/postgresql/data`. Leave the HTTP health path empty so readiness checks use TCP. A fresh PostgreSQL data volume requires initialization credentials; startup logs show initialization failures.

The old deployment stays running while the candidate becomes ready and Nginx switches. Nginx configuration is written atomically, validated before reload, and the applied route is checked using a deployment marker. Certificate or routing failure restores the saved configuration before removing the candidate. Failed deployment status is shown separately from the active container's runtime status.

Saved configuration and active deployment are separate records. Editing a domain or port does not change the running application. Both active and pending domains are reserved until deployment completes. Lifecycle actions run through the same worker. Delete immediately prevents later deployment requests and cancels pending operations; an in-progress build checks for deletion before starting/switching its candidate.

Git remotes contain no access tokens. Fetch uses a temporary askpass helper; token values are supplied through its environment. Docker builds receive a separate context excluding `.git`, `.env`, private `.env.*` files and `github-app-private-key.pem`. Example/sample/template env files are retained. Symlinks escaping the context are rejected. Applications should receive secrets through configured environment variables rather than committed env files.

## Migration, backups and recovery

On first startup, `state.json` is imported transactionally into `/data/state.sqlite3`. The original JSON contents are preserved and its permissions restricted; migration is recorded so it is not repeated. Credential-bearing legacy repository URLs are split into a clean URL and private token. The worker also scrubs existing Git origins at startup, before any new build. Historical logs are redacted when served. Existing `deployer-<name>` containers are recognized. Interrupted legacy deployments are queued for retry; subsequent deployments use the durable stage journal.

Before upgrading, stop the original deployer and back up its data volume, Nginx configuration and certificate volumes. Do not run the old thread-based deployer alongside the new worker. After migration SQLite is authoritative; reverting to the old JSON file would lose subsequent edits and jobs.

For a consistent backup, stop the web and worker services and copy their data volume, Nginx configuration and certificates. The original JSON backup also contains credentials; protect it accordingly.

Startup recovery restores routing and removes candidates for interrupted pre-commit deployments, then retries the latest configuration. Once activation has been committed, recovery checks readiness before finishing cleanup. If the replacement has stopped and the previous container remains running, recovery restores the previous deployment. If restoring routing fails, both containers and the running journal are retained, and the worker restarts rather than processing later jobs against uncertain state. Transient deletion failures retain the delete job for recovery, with new deployments still blocked. Fix the infrastructure/configuration problem and restart the worker; inspect `docker compose logs worker` and app logs under `/data/logs`.

Failed jobs are not retried forever. Correct the failure and click Deploy again. Process shutdown may interrupt a job; the journal survives and is reconciled on restart. Job/config data, including credentials and environment values, is stored in a private SQLite file; this is access restriction, not encryption at rest.

## Development verification

Use Python 3.12+ and install `requirements-dev.txt` into a virtual environment:

```sh
python -m pip install -r requirements-dev.txt
python -m pip check
python -m compileall -q app.py deployer
python -m pytest -q
```

The default suite uses real SQLite, Flask clients, local Git and subprocesses, with external Docker/GitHub/routing boundaries replaced for deterministic failure and recovery testing. Opt-in Docker tests run with `RUN_DOCKER_TESTS=1`; they create their own containers and network and never use the server's application containers. Do not run them against a production Docker host.

Python packages and external image versions are pinned. Image tags still permit upstream rebuilds; immutable digest pinning can be added after validating images on the deployment platform. Application repository Dockerfiles remain their owners' responsibility.
