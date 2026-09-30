"""Deployment stages form a recoverable transaction around external effects."""
import os
import time
from contextlib import contextmanager

from deployer.integrations.commands import redact
from deployer.integrations.git import parse_env
from .readiness import wait_ready


class DeploymentService:
    def __init__(self, settings, apps, jobs, repository, docker, router):
        self.settings, self.apps, self.jobs = settings, apps, jobs
        self.repository, self.docker, self.router = repository, docker, router
        self.readiness = wait_ready

    @contextmanager
    def log(self, name):
        directory = self.settings.data_dir / "logs"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f"{name}.log"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.chmod(path, 0o600)
        with os.fdopen(fd, "a") as stream:
            yield stream

    def _check_not_deleted(self, name):
        app = self.apps.get(name)
        if not app or app["delete_requested"]:
            raise ValueError("Deployment cancelled because deletion was requested.")

    def execute(self, job):
        with self.log(job.name) as log:
            log.write(f"\nJob {job.id}: {job.kind}\n"); log.flush()
            if job.kind == "deploy":
                self.deploy(job, log)
            else:
                self.lifecycle(job, log)

    def deploy(self, job, log):
        app = self.apps.get(job.name)
        if not app or app["delete_requested"]:
            self.jobs.finish(job.id, "cancelled")
            return
        frozen = job.payload.get("config", app)
        config = {k: v for k, v in frozen.items() if k not in {"active", "delete_requested", "error", "status"}}
        previous = job.payload.get("previous", app["active"])
        candidate = f"deployer-{job.name}-j{job.id}"
        payload = {"config": config, "previous": previous, "candidate": candidate}
        self.jobs.progress(job.id, "preparing", payload)
        self.apps.status(job.name, "deploying")
        values = [config.get("token", ""), *parse_env(config.get("env", "")).values()]
        try:
            sha, context = self.repository.prepare(config, job.id, log)
            image = f"deployer-{job.name}:{sha}"
            self.docker.build(image, context, log, secrets=values)
            self._check_not_deleted(job.name)
            self.jobs.progress(job.id, "starting", payload)
            self.docker.remove(candidate, log)
            self.docker.start_candidate(candidate, image, config, log)
            if not self.readiness(self.docker, candidate, config["port"],
                timeout=self.settings.readiness_timeout, health_path=config.get("health_path", "")):
                raise RuntimeError("Replacement failed readiness; previous deployment retained.")
            self.docker.restart_policy(candidate, log)
            self._check_not_deleted(job.name)
            domains = [config["domain"], previous["domain"] if previous else ""]
            payload["routing"] = self.router.snapshot(domains)
            # Record rollback data BEFORE provision can overwrite a file or reload nginx.
            self.jobs.progress(job.id, "switching", payload)
            self.router.provision(config["domain"], candidate, config["port"], log)
            if not self.ready(candidate, config):
                raise RuntimeError("Replacement lost readiness during routing; previous deployment retained.")
            self._check_not_deleted(job.name)
            active = {**config, "container": candidate, "sha": sha, "deployed_at": int(time.time())}
            self.apps.activate(job.name, active, job.id)
            self.cleanup_committed(payload, log)
            self.jobs.finish(job.id)
            log.write(f"Deployment succeeded: {sha}\n"); log.flush()
            self.repository.cleanup(job.id)
        except Exception as error:
            current = self.jobs.get(job.id)
            if current.stage == "committed":
                # The new deployment is authoritative. Recovery retries cleanup only.
                log.write("New deployment active; cleanup will resume on worker restart.\n")
                raise
            self.rollback(current, log)
            message = redact(str(error), values)
            log.write(f"FAILED: {message}\n"); log.flush()
            self.apps.status(job.name, "failed", message)
            self.jobs.finish(job.id, "failed", message)

    def rollback(self, job, log):
        if job.payload.get("candidate"):
            self.router.cancel_certificate(job.payload["candidate"])
        if job.payload.get("routing"):
            # If restoring routing fails, retain both containers and the running job.
            self.router.restore(job.payload["routing"], log)
        if job.payload.get("candidate"):
            self.docker.remove(job.payload["candidate"], log)
        self.repository.cleanup(job.id)

    def cleanup_committed(self, payload, log):
        if not self.ready(payload["candidate"], payload["config"]):
            raise RuntimeError("Activated container stopped before cleanup; previous container retained.")
        previous = payload.get("previous")
        config = payload["config"]
        if previous:
            if previous["domain"] != config["domain"]:
                self.router.remove(previous["domain"], log)
            if previous["container"] != payload["candidate"]:
                self.docker.remove(previous["container"], log)

    def ready(self, container, config):
        return self.readiness(self.docker, container, config["port"],
            timeout=self.settings.readiness_timeout, health_path=config.get("health_path", ""))

    def lifecycle(self, job, log):
        app = self.apps.get(job.name)
        if not app:
            self.jobs.finish(job.id, "cancelled")
            return
        active = app["active"]
        if job.kind == "delete":
            if active:
                self.router.remove(active["domain"], log)
                self.docker.remove(active["container"], log)
            # Legacy containers without a successfully deployed record can still exist.
            self.docker.remove(f"deployer-{job.name}-next", log)
            self.docker.remove(f"deployer-{job.name}", log)
            self.repository.remove(job.name)
            self.apps.remove(job.name)
        elif active:
            self.docker.action(job.kind, active["container"], log)
            self.apps.status(job.name, "exited" if job.kind == "stop" else "running")
        else:
            raise ValueError("No active container; deploy the app first.")
        self.jobs.finish(job.id)

    def recover_job(self, job):
        with self.log(job.name) as log:
            log.write(f"Recovering interrupted job {job.id} at {job.stage}.\n")
            if job.kind != "deploy":
                self.jobs.retry(job.id)
            elif job.stage == "committed":
                if not self.ready(job.payload["candidate"], job.payload["config"]):
                    previous = job.payload.get("previous")
                    if previous and self.docker.inspect(previous["container"]).get("Status") == "running":
                        self.rollback(job, log)
                        self.apps.restore_active(job.name, previous, "Replacement stopped during activation; previous deployment restored.")
                    else:
                        self.apps.status(job.name, "failed", "Activated container is unavailable; retained containers need attention.")
                    self.jobs.finish(job.id, "failed", "Activated container unavailable during recovery.")
                    return
                self.cleanup_committed(job.payload, log)
                self.repository.cleanup(job.id)
                self.jobs.finish(job.id)
            else:
                self.rollback(job, log)
                app = self.apps.get(job.name)
                if not app or app["delete_requested"]:
                    self.jobs.finish(job.id, "cancelled")
                else:
                    self.apps.status(job.name, "recovering")
                    self.jobs.retry(job.id)
