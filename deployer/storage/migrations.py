"""Create the schema and import legacy JSON exactly once."""
import json
import time
from urllib.parse import unquote, urlsplit, urlunsplit


def migrate(conn, directory, base_domain=""):
    for statement in (
        "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS apps (name TEXT PRIMARY KEY, config TEXT NOT NULL, "
        "active TEXT, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', "
        "delete_requested INTEGER NOT NULL DEFAULT 0)",
        "CREATE TABLE IF NOT EXISTS installations (id TEXT PRIMARY KEY, info TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
        "kind TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', stage TEXT NOT NULL DEFAULT 'queued', "
        "payload TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL)",
        "CREATE UNIQUE INDEX IF NOT EXISTS pending_deploy ON jobs(name) "
        "WHERE kind='deploy' AND state='pending'",
    ):
        conn.execute(statement)
    if conn.execute("SELECT value FROM metadata WHERE key='legacy_import'").fetchone():
        return
    legacy = directory / "state.json"
    if legacy.exists():
        data = json.loads(legacy.read_text())
        for name, app in data.get("apps", {}).items():
            app = dict(app)
            if app.get("repo_url"):
                parts = urlsplit(app["repo_url"])
                if parts.username is not None:
                    app["token"] = app.get("token") or unquote(parts.password or "")
                    app["repo_url"] = urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[-1]))
            app.setdefault("base_domain", base_domain)
            if "subdomain" not in app:
                app["subdomain"] = app["domain"][:-(len(base_domain) + 1)] if base_domain and app["domain"].endswith("." + base_domain) else name
            status = app.get("status", "new")
            active = None if status == "new" else {**app, "container": f"deployer-{name}"}
            conn.execute("INSERT INTO apps(name,config,active,status) VALUES (?,?,?,?)",
                         (name, json.dumps(app), json.dumps(active) if active else None,
                          "recovering" if status == "deploying" else status))
            if status == "deploying":
                conn.execute("INSERT INTO jobs(name,kind,created_at) VALUES (?,'deploy',?)", (name, time.time()))
        for installation, info in data.get("installations", {}).items():
            conn.execute("INSERT INTO installations VALUES (?,?)", (installation, json.dumps(info)))
        legacy.chmod(0o600)
    conn.execute("INSERT INTO metadata VALUES ('legacy_import','1')")
