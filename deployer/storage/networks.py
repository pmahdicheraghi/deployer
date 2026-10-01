"""User-defined Docker networks stored in SQLite."""
import json
import re
import time

NETWORK_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


class NetworkStore:
    def __init__(self, db, default_network="web"):
        self.db = db
        self.default_network = default_network

    def list(self):
        with self.db.transaction() as conn:
            rows = conn.execute("SELECT name, created_at FROM networks ORDER BY name").fetchall()
            return [{"name": row["name"], "created_at": row["created_at"]} for row in rows]

    def get(self, name):
        with self.db.transaction() as conn:
            row = conn.execute("SELECT name, created_at FROM networks WHERE name=?", (name,)).fetchone()
            return {"name": row["name"], "created_at": row["created_at"]} if row else None

    def create(self, name):
        name = (name or "").strip().lower()
        if not NETWORK_NAME.fullmatch(name):
            raise ValueError("Network name must contain lowercase letters, digits or dashes (up to 31 characters).")
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM networks WHERE name=?", (name,)).fetchone():
                raise ValueError("That network name is already in use.")
            conn.execute("INSERT INTO networks(name, created_at) VALUES (?, ?)", (name, time.time()))
        return name

    def delete(self, name, cleanup=None):
        name = (name or "").strip().lower()
        if name == self.default_network:
            raise ValueError("Cannot delete the default network.")
        with self.db.transaction() as conn:
            row = conn.execute("SELECT 1 FROM networks WHERE name=?", (name,)).fetchone()
            if not row:
                raise ValueError("Network not found.")
            for app_row in conn.execute("SELECT config FROM apps").fetchall():
                cfg = json.loads(app_row["config"])
                if cfg.get("network") == name:
                    raise ValueError(f"Cannot delete network '{name}' because it is in use by app '{cfg.get('name')}'.")
            if cleanup:
                cleanup(name)
            conn.execute("DELETE FROM networks WHERE name=?", (name,))
