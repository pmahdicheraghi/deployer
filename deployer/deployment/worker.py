"""One dedicated worker owns external mutations; jobs survive its process."""
import os
import signal
import sys
import time
from contextlib import contextmanager


class Worker:
    def __init__(self, service, apps, jobs):
        self.service, self.apps, self.jobs = service, apps, jobs

    def step(self):
        job = self.jobs.claim()
        if job is None:
            return False
        try:
            self.service.execute(job)
        except Exception as error:
            latest = self.jobs.get(job.id)
            if job.kind in {"deploy", "delete"}:
                # An incomplete rollback/cleanup must block later work until recovery.
                raise RuntimeError("Operation recovery required; worker restarting.") from error
            self.jobs.finish(job.id, "failed", "Lifecycle operation failed; see worker log.")
            self.apps.status(job.name, "failed", "Lifecycle operation failed; see worker log.")
        return True

    def recover(self):
        for job in self.jobs.list("running"):
            self.service.recover_job(job)
        for app in self.apps.list():
            if app.get("method") != "image":
                self.service.repository.sanitize_remote(app)
            if app["active"]:
                state = self.service.docker.inspect(app["active"]["container"]).get("Status", "missing")
                if app["status"] not in {"failed", "recovering"}:
                    self.apps.status(app["name"], state)
                # Legacy deployments may have left a pre-swap candidate behind.
                with self.service.log(app["name"]) as log:
                    self.service.docker.remove(f"deployer-{app['name']}-next", log)


@contextmanager
def worker_lock(directory):
    """OS releases the exclusive lock when the worker crashes."""
    path = directory / "worker.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0); stream.write(b"0"); stream.flush(); stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def main():
    from deployer import create_app
    application = create_app()
    services = application.extensions["deployer"]
    worker = services["worker"]
    settings = services["settings"]
    stopping = False
    def stop(signum, frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with worker_lock(settings.data_dir):
        worker.recover()
        if settings.panel_domain:
            # Bootstrap is worker-owned, serialized with deployment routing updates.
            snapshot = services["router"].snapshot([settings.panel_domain])
            try:
                services["router"].provision(settings.panel_domain, "deployer", 8080, sys.stdout)
            except Exception:
                services["router"].restore(snapshot, sys.stdout)
                raise
        while not stopping:
            if not worker.step():
                time.sleep(.5)


if __name__ == "__main__":
    main()
