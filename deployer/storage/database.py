"""Short SQLite transactions shared by the web process and worker."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .migrations import migrate


class Database:
    def __init__(self, data_dir, base_domain=""):
        self.directory = Path(data_dir)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "state.sqlite3"
        # umask alone is process-global; create the database privately before opening it.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self.transaction() as conn:
            migrate(conn, self.directory, base_domain)

    @contextmanager
    def transaction(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
