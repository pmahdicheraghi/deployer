"""Durable, coalescing queue; only a worker may execute these operations."""
import json
import time

from deployer.models import Job


class JobStore:
    def __init__(self, db):
        self.db = db

    def enqueue(self, name, kind, payload=None):
        if kind not in {"deploy", "delete", "stop", "start", "restart"}:
            raise ValueError("Unknown operation.")
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM apps WHERE name=?", (name,)).fetchone()
            if not row or row["delete_requested"]:
                raise ValueError("App is missing or awaiting deletion.")
            if kind == "delete":
                conn.execute("UPDATE apps SET delete_requested=1 WHERE name=?", (name,))
                conn.execute("UPDATE jobs SET state='cancelled' WHERE name=? AND state='pending'", (name,))
            if kind == "deploy":
                pending = conn.execute("SELECT id FROM jobs WHERE name=? AND kind='deploy' AND state='pending'", (name,)).fetchone()
                if pending:
                    return pending["id"]
            return conn.execute("INSERT INTO jobs(name,kind,payload,created_at) VALUES (?,?,?,?)",
                                (name, kind, json.dumps(payload or {}), time.time())).lastrowid

    def claim(self):
        with self.db.transaction() as conn:
            # A crashed operation must be reconciled before any subsequent job.
            if conn.execute("SELECT 1 FROM jobs WHERE state='running'").fetchone():
                return None
            row = conn.execute("SELECT * FROM jobs WHERE state='pending' ORDER BY id LIMIT 1").fetchone()
            if not row:
                return None
            payload = json.loads(row["payload"])
            if row["kind"] == "deploy":
                app = conn.execute("SELECT * FROM apps WHERE name=?", (row["name"],)).fetchone()
                if app:
                    payload = {"config": json.loads(app["config"]),
                               "previous": json.loads(app["active"]) if app["active"] else None,
                               "candidate": f"deployer-{row['name']}-j{row['id']}"}
            serialized = json.dumps(payload)
            conn.execute("UPDATE jobs SET state='running',payload=? WHERE id=?", (serialized, row["id"]))
            return Job.from_row({**dict(row), "state": "running", "payload": serialized})

    def get(self, job_id):
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return Job.from_row(row) if row else None

    def list(self, state=None):
        with self.db.transaction() as conn:
            rows = conn.execute("SELECT * FROM jobs" + (" WHERE state=?" if state else "") + " ORDER BY id", (state,) if state else ())
            return [Job.from_row(row) for row in rows]

    def progress(self, job_id, stage, payload):
        with self.db.transaction() as conn:
            conn.execute("UPDATE jobs SET stage=?,payload=? WHERE id=?", (stage, json.dumps(payload), job_id))

    def finish(self, job_id, state="done", error=""):
        with self.db.transaction() as conn:
            conn.execute("UPDATE jobs SET state=?,error=?,payload='{}' WHERE id=?", (state, error, job_id))
        self.prune_history()

    def prune_history(self, keep=1000):
        with self.db.transaction() as conn:
            conn.execute("UPDATE jobs SET payload='{}' WHERE state IN ('done','failed','cancelled')")
            conn.execute("DELETE FROM jobs WHERE id IN (SELECT id FROM jobs "
                         "WHERE state IN ('done','failed','cancelled') ORDER BY id DESC LIMIT -1 OFFSET ?)", (keep,))

    def track_image(self, image_id, reference):
        with self.db.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO image_resources VALUES (?, ?)", (image_id, reference))

    def tracked_images(self):
        with self.db.transaction() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM image_resources ORDER BY image_id")]

    def forget_image(self, image_id):
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM image_resources WHERE image_id=?", (image_id,))

    def retry(self, job_id):
        with self.db.transaction() as conn:
            row = conn.execute("SELECT name,kind FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row["kind"] == "deploy":
                conn.execute("UPDATE jobs SET state='cancelled' WHERE name=? AND kind='deploy' AND state='pending'", (row["name"],))
                conn.execute("UPDATE jobs SET state='pending',stage='queued',payload='{}' WHERE id=?", (job_id,))
            else:
                conn.execute("UPDATE jobs SET state='pending',stage='queued' WHERE id=?", (job_id,))
