"""Saved configuration and the last successfully activated deployment."""
import json
import time


def decode(row):
    if row is None:
        return None
    return {**json.loads(row["config"]), "active": json.loads(row["active"]) if row["active"] else None,
            "status": row["status"], "error": row["error"], "delete_requested": bool(row["delete_requested"])}


def domains(config):
    if not config.get("is_public", True) or not config.get("domain"):
        return set()
    reserved = {config["domain"]}
    base = config.get("base_domain")
    sub = config.get("subdomain", config["name"])
    if not base and config["domain"].startswith(sub + "."):
        base = config["domain"][len(sub) + 1:]
    if base:
        reserved.add(f"{sub}.{base}")
    return reserved


class AppStore:
    def __init__(self, db):
        self.db = db

    def get(self, name):
        with self.db.transaction() as conn:
            return decode(conn.execute("SELECT * FROM apps WHERE name=?", (name,)).fetchone())

    def list(self):
        with self.db.transaction() as conn:
            return [decode(row) for row in conn.execute("SELECT * FROM apps ORDER BY name")]

    @staticmethod
    def _reserve(conn, name, config):
        requested = domains(config)
        if not requested:
            return
        for row in conn.execute("SELECT * FROM apps WHERE name<>?", (name,)):
            other = decode(row)
            occupied = domains(other)
            if other["active"] and other["active"].get("domain"):
                occupied.add(other["active"]["domain"])
            if requested & occupied:
                raise ValueError("That domain or subdomain is already reserved.")
        # An edit must not release a deployment's frozen domain while it is building.
        for row in conn.execute("SELECT payload FROM jobs WHERE name<>? AND state='running' AND kind='deploy'", (name,)):
            payload = json.loads(row["payload"])
            occupied = set()
            for key in ("config", "previous"):
                if payload.get(key):
                    occupied.update(domains(payload[key]))
            if requested & occupied:
                raise ValueError("That domain is reserved by an in-progress deployment.")

    def create(self, config, deploy=False):
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM apps WHERE name=?", (config["name"],)).fetchone():
                raise ValueError("That app name is already in use.")
            self._reserve(conn, config["name"], config)
            conn.execute("INSERT INTO apps(name,config,status) VALUES (?,?,'new')", (config["name"], json.dumps(config)))
            if deploy:
                conn.execute("INSERT INTO jobs(name,kind,created_at) VALUES (?,'deploy',?)", (config["name"], time.time()))

    def edit(self, name, config):
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM apps WHERE name=?", (name,)).fetchone()
            if not row or row["delete_requested"]:
                raise ValueError("App is missing or awaiting deletion.")
            self._reserve(conn, name, config)
            conn.execute("UPDATE apps SET config=? WHERE name=?", (json.dumps(config), name))

    def activate(self, name, active, job_id=None):
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM apps WHERE name=?", (name,)).fetchone()
            if not row or row["delete_requested"]:
                raise ValueError("App is missing or awaiting deletion.")
            conn.execute("UPDATE apps SET active=?,status='running',error='' WHERE name=?", (json.dumps(active), name))
            if job_id is not None:
                conn.execute("UPDATE jobs SET stage='committed' WHERE id=?", (job_id,))

    def status(self, name, status, error=""):
        with self.db.transaction() as conn:
            conn.execute("UPDATE apps SET status=?,error=? WHERE name=?", (status, error, name))

    def restore_active(self, name, previous, error):
        with self.db.transaction() as conn:
            conn.execute("UPDATE apps SET active=?,status='failed',error=? WHERE name=?",
                         (json.dumps(previous) if previous else None, error, name))

    def remove(self, name):
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM apps WHERE name=?", (name,))
            conn.execute("UPDATE jobs SET state='cancelled' WHERE name=? AND state='pending'", (name,))

    def installations(self):
        with self.db.transaction() as conn:
            return {row["id"]: json.loads(row["info"]) for row in conn.execute("SELECT * FROM installations")}

    def connect(self, installation, info):
        with self.db.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO installations VALUES (?,?)", (installation, json.dumps(info)))
