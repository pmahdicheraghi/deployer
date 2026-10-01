# Technical Specification: Docker Image Source, Custom Networks & Volume Management

**Date:** 2026-10-01  
**Status:** Approved  
**Author:** Pair Programming (Antigravity & User)

## 1. Overview & Goals

Deployer currently supports deploying applications only from Git repositories (via GitHub App or manual HTTPS clone) and assumes every application is an HTTP web service requiring a public domain, Nginx reverse proxying, and TLS certificates.

This specification introduces:
1. **Docker Image Application Source:** Ability to deploy applications directly from public or private Docker image repositories (e.g. Docker Hub, GHCR), bypassing Git checkout and Dockerfile builds.
2. **Custom Docker Networks & Service Discovery:** A dedicated Networks section to manage Docker bridge networks, assign apps to networks, and enable service-to-service discovery using app names as hostnames (e.g. `postgres:5432`).
3. **Optional Public Routing:** Allowing services (such as databases or internal microservices) to run without a public domain or Nginx reverse proxy.
4. **Stateful Storage & Volume Management:** Automated persistent named volumes for stateful applications, safe stateful container transitions, a dedicated Volumes dashboard, and explicit volume retention/deletion controls.

---

## 2. Storage & Database Schema

Deployer uses SQLite with short transactions and JSON serialization for app configuration and active runtime records in `deployer/storage/`.

### 2.1. Networks Table
Add a new table in `deployer/storage/migrations.py`:
```sql
CREATE TABLE IF NOT EXISTS networks (
    name TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);
```
- During migration, the base network from `settings.network` (default `"web"`) is inserted if not present:
  `INSERT OR IGNORE INTO networks (name, created_at) VALUES (?, ?)`

### 2.2. NetworkStore (`deployer/storage/networks.py`)
Encapsulates CRUD operations on `networks`:
- `list()`: Returns list of `{"name": str, "created_at": float}` ordered by name.
- `get(name)`: Returns network record or `None`.
- `create(name)`: Validates name against `^[a-z0-9][a-z0-9-]{0,30}$` and inserts into `networks`.
- `delete(name, active_apps_using_network)`:
  - Raises `ValueError("The default web network cannot be deleted.")` if `name == "web"`.
  - Raises `ValueError("Network in use by app(s)")` if any app has `config.get("network") == name`.
  - Deletes from `networks`.

### 2.3. App Configuration Schema (`apps.config` JSON)
No table alterations are needed on `apps`. The JSON `config` payload is extended with:
- `method`: `"github_app" | "manual" | "image"`
- `image`: Docker image reference (e.g. `postgres:16`, `ghcr.io/org/repo:tag`). Required if `method == "image"`.
- `registry_user`: Optional username for private image registries.
- `registry_password`: Optional token/password for private image registries.
- `is_public`: Boolean. If `False`, `domain`, `subdomain`, and `custom_domain` are empty.
- `network`: String. Name of assigned Docker network. Defaults to `settings.network` (`"web"`).
- `stateful`: Boolean. Whether persistent volume is enabled.
- `mount_path`: Container path where the volume is mounted (e.g. `/var/lib/postgresql/data`). Required if `stateful == True`.

---

## 3. Docker Integration Layer (`deployer/integrations/docker.py`)

Native Docker CLI commands wrapped in `CommandRunner`:

1. **Network Management:**
   - `ensure_network(name, log=None)`:
     Runs `docker network inspect <name>`. If non-zero exit, runs `docker network create --driver bridge <name>`.
   - `remove_network(name, log=None)`:
     Runs `docker network rm <name>`.
   - `connect_network(network, container, alias=None, log=None)`:
     Runs `docker network connect [--alias <alias>] <network> <container>`.

2. **Image Pulling:**
   - `pull(image, log, auth=None)`:
     - If `auth` contains `user` and `password`: creates a temporary directory `DOCKER_CONFIG`, executes `docker login` with credentials, runs `docker pull <image>`, and cleans up the temporary directory. Passes `auth["password"]` in `secrets` so `CommandRunner` redacts it from logs.
     - If no `auth`: executes `docker pull <image>` and streams stdout/stderr to `log`.

3. **Volume Operations:**
   - `list_volumes()`:
     Runs `docker volume ls --format "{{json .}}"` and returns list of volumes matching prefix `deployer-data-`.
   - `remove_volume(volume_name, log=None)`:
     Runs `docker volume rm <volume_name>`.

4. **Container Creation (`start_candidate`):**
   - Resolves primary network: `config.get("network") or self.settings.network`.
   - Calls `ensure_network(primary_network, log)`.
   - Adds `--network <primary_network>` and `--network-alias <config["name"]>`.
   - If `config.get("stateful")` and `config.get("mount_path")`:
     Adds `-v deployer-data-<config["name"]>:<config["mount_path"]>`.
   - If `config.get("is_public", True)` and `primary_network != self.settings.network`:
     After container start, calls `connect_network(self.settings.network, container, log=log)`.

---

## 4. Deployment Service & Worker Lifecycle

### 4.1. Image Preparation vs Build (`DeploymentService.deploy`)
- If `config["method"] == "image"`:
  - Invokes `docker.pull(config["image"], log, auth=...)`.
  - Bypasses `repository.prepare()` and `docker.build()`.
  - Sets `sha = "image"` (or image ID digest).
- If `config["method"] != "image"`:
  - Runs existing `repository.prepare()` and `docker.build()`.

### 4.2. Stateful Container Transitions
- In `deploy()`:
  - If `config.get("stateful")` and `previous` container is active:
    - Stops the previous container: `docker.action("stop", previous["container"], log)` before `start_candidate`.
    - If candidate fails readiness: rollback runs `docker.action("start", previous["container"], log)` to restore the original container.
  - If `stateful` is False: keeps existing zero-downtime blue/green behavior (candidate starts before old container is stopped).

### 4.3. Readiness Check (`DeploymentService.ready` / `wait_ready`)
- If `config.get("is_public", True)`: runs existing HTTP/TCP port probe against candidate.
- If `config.get("is_public") is False` (internal-only):
  - Checks if candidate container state is `running`.
  - If image defines a Docker `HEALTHCHECK`, waits until `state["Health"]["Status"] == "healthy"`.

### 4.4. Conditional Routing & Certbot
- If `config.get("is_public", True)`:
  - Performs routing snapshot, Nginx proxy configuration, and certificate provisioning via `router.provision()`.
- If `config.get("is_public") is False`:
  - Bypasses Nginx snapshot and `router.provision()`. No reverse proxy files or certificates are created.

### 4.5. App Deletion with Volume Cleanup
- App delete job payload receives optional `delete_volume: bool`.
- Worker cleans up candidate and active container.
- If `delete_volume` is `True`: worker executes `docker.remove_volume("deployer-data-" + name, log)`.
- If `delete_volume` is `False`: volume is preserved.

### 4.6. Worker Recovery (`Worker.recover`)
- When iterating through apps:
  - `if app.get("method") != "image": self.service.repository.sanitize_remote(app)`
  - Skips remote sanitization for image-based apps.

---

## 5. Web Dashboard & User Interface

### 5.1. Navigation (`deployer/templates/base.html`)
- Header navigation bar includes:
  - **Apps** (`/`)
  - **Networks** (`/networks`)
  - **Volumes** (`/volumes`)
  - **+ New app** (`/apps/new`)

### 5.2. Networks Page (`deployer/templates/networks.html` & `deployer/web/networks.py`)
- Route `GET /networks`: Lists all networks from `NetworkStore`, showing name, creation date, and apps connected to each network.
- Route `POST /networks`: Creates a new network in `NetworkStore` and Docker.
- Route `POST /networks/<name>/delete`: Removes network if not `web` and not in use by any app.

### 5.3. Volumes Page (`deployer/templates/volumes.html` & `deployer/web/volumes.py`)
- Route `GET /volumes`: Lists all `deployer-data-*` volumes from Docker.
  - Correlates volume name with apps in `AppStore`.
  - Status: `Attached (<app_name>)` or `Orphaned (App deleted)`.
- Route `POST /volumes/<name>/delete`: Deletes orphaned volume via `docker volume rm`. Rejects deletion if app exists.

### 5.4. App Form (`deployer/templates/form.html` & `deployer/web/validation.py`)
- **Source Selection:** Dropdown with `GitHub (connected account)`, `Manual repository URL`, and `Docker image`.
  - JavaScript toggles field sections (`gh_fields`, `manual_fields`, `image_fields`).
- **Exposure:** Checkbox `Expose with public domain`. When unchecked, hides Subdomain and Custom Domain inputs.
- **Network:** Dropdown to select network from `NetworkStore.list()`.
- **Stateful Storage:** Checkbox `Persistent storage (Stateful)`. Reveals `Mount path` input (e.g. `/var/lib/postgresql/data`).

### 5.5. App Detail Page (`deployer/templates/detail.html`)
- Displays `Image: <image>` for image-based apps instead of Git URL/webhooks.
- For internal apps, displays `Internal Service` badge with connection guide: `Host: <name>`, `Port: <port>`, `Network: <network>`.
- Displays `Volume: deployer-data-<name> -> <mount_path>` when stateful.
- Delete confirmation modal/dialog includes `[ ] Delete persistent volume (deployer-data-<name>)`.

---

## 6. Verification & Testing Plan

1. **Automated Unit & Integration Tests (`tests/`):**
   - `test_storage.py`: Test `NetworkStore` creation, validation, deletion rules, and reservation.
   - `test_web.py`: Test validation of `method="image"`, `is_public=False`, `stateful=True`, network assignment, and network/volume endpoints.
   - `test_deployment.py`:
     - Test image pull deployment workflow (mocking `docker.pull`).
     - Test internal service deployment (verifying Nginx provisioning is bypassed).
     - Test stateful deployment (verifying old container is stopped before candidate starts, and rollback starts old container).
     - Test app deletion with and without volume cleanup.
   - Run complete suite: `.venv\Scripts\python -m pytest -q` ensuring 100% pass rate.
2. **Docker Smoke Test (`tests/test_docker_smoke.py`):**
   - Run with Docker daemon: verify network creation, image pull (`postgres:16-alpine`), candidate startup with alias and volume mount, and cleanup.
