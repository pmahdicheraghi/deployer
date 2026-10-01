from pathlib import Path

import pytest

from test_storage import config, store


class Crash(BaseException):
    """Simulate process death, bypassing normal error rollback."""


class FakeDocker:
    def __init__(self):
        self.containers = {"old": {"Status": "running"}}
        self.fail_build = False
        self.fail_start = False
        self.on_build = lambda: None

    def inspect(self, name, **kwargs): return self.containers.get(name, {"Status": "missing"})
    def build(self, image, context, log, secrets=()):
        self.on_build()
        if self.fail_build: raise RuntimeError("build failed")
    def start_candidate(self, name, image, conf, log):
        self.containers[name] = {"Status": "running", "Health": {"Status": "healthy"}}
        if self.fail_start: raise RuntimeError("start failed")
    def remove(self, name, log): self.containers.pop(name, None)
    def restart_policy(self, name, log): pass
    def action(self, operation, name, log):
        self.containers[name]["Status"] = "exited" if operation == "stop" else "running"
    def pull(self, image, log, auth=None): pass
    def ensure_network(self, network, log=None): pass
    def remove_network(self, network, log=None): pass
    def connect_network(self, network, container, alias=None, log=None): pass
    def list_volumes(self, prefix="deployer-data-"): return []
    def remove_volume(self, volume, log=None): pass
    def ensure_readiness_network(self, network, **kwargs): return None
    def release_readiness_network(self, network, worker): pass
    def logs(self, container, log, secrets=()): pass


class FakeGit:
    def __init__(self, tmp): self.directory = tmp
    def prepare(self, conf, job_id, log): return "newsha", self.directory
    def cleanup(self, job_id): pass
    def remove(self, name): pass
    def sanitize_remote(self, config): pass


class FakeRouter:
    def __init__(self):
        self.routes = {"demo.apps.example.com": "old"}
        self.fail = False
        self.crash = False
        self.on_switch = lambda: None
    def snapshot(self, domains): return {d: self.routes.get(d) for d in domains if d}
    def provision(self, domain, container, port, log):
        self.routes[domain] = container
        self.on_switch()
        if self.crash: raise Crash()
        if self.fail: raise RuntimeError("route failed")
    def restore(self, snapshot, log):
        for domain, target in snapshot.items():
            if target is None: self.routes.pop(domain, None)
            else: self.routes[domain] = target
    def remove(self, domain, log): self.routes.pop(domain, None)
    def cancel_certificate(self, container): pass


@pytest.fixture
def system(tmp_path):
    from deployer.config import Settings
    from deployer.deployment.service import DeploymentService
    from deployer.deployment.worker import Worker
    apps, jobs = store(tmp_path)
    apps.create(config())
    apps.activate("demo", {**config(), "container": "old", "sha": "oldsha"})
    settings = Settings(tmp_path, "apps.example.com", "test@example.com", "password", "secret")
    docker, router = FakeDocker(), FakeRouter()
    service = DeploymentService(settings, apps, jobs, FakeGit(tmp_path), docker, router)
    return apps, jobs, docker, router, service, Worker(service, apps, jobs)


@pytest.mark.parametrize("failure", ["readiness", "routing", "build", "start"])
def test_failed_replacement_preserves_active_container_and_route(system, failure):
    apps, jobs, docker, router, service, worker = system
    if failure == "readiness": service.readiness = lambda *a, **kw: False
    if failure == "routing": router.fail = True
    if failure == "build": docker.fail_build = True
    if failure == "start": docker.fail_start = True
    jobs.enqueue("demo", "deploy"); worker.step()
    assert set(docker.containers) == {"old"}
    assert router.routes == {"demo.apps.example.com": "old"}
    assert apps.get("demo")["active"]["sha"] == "oldsha"
    assert jobs.list()[0].state == "failed"


def test_success_switches_route_before_old_container_cleanup(system):
    apps, jobs, docker, router, service, worker = system
    router.on_switch = lambda: (_ for _ in ()).throw(AssertionError()) if "old" not in docker.containers else None
    jobs.enqueue("demo", "deploy"); worker.step()
    active = apps.get("demo")["active"]
    assert active["sha"] == "newsha"
    assert router.routes[active["domain"]] == active["container"]
    assert "old" not in docker.containers
    assert jobs.list()[0].state == "done"


def test_push_during_build_runs_followup(system):
    apps, jobs, docker, router, service, worker = system
    jobs.enqueue("demo", "deploy")
    docker.on_build = lambda: jobs.enqueue("demo", "deploy")
    worker.step()
    docker.on_build = lambda: None
    assert len(jobs.list("pending")) == 1
    worker.step()
    assert len(jobs.list("done")) == 2


def test_delete_during_build_prevents_candidate_activation(system):
    apps, jobs, docker, router, service, worker = system
    jobs.enqueue("demo", "deploy")
    docker.on_build = lambda: jobs.enqueue("demo", "delete")
    worker.step()
    assert router.routes == {"demo.apps.example.com": "old"}
    worker.step()
    assert apps.get("demo") is None
    assert docker.containers == {}
    assert router.routes == {}


def test_stop_is_serialized_after_deployment(system):
    apps, jobs, docker, router, service, worker = system
    jobs.enqueue("demo", "deploy")
    docker.on_build = lambda: jobs.enqueue("demo", "stop")
    worker.step(); worker.step()
    assert docker.inspect(apps.get("demo")["active"]["container"])["Status"] == "exited"


def test_saved_domain_applies_only_on_success(system):
    apps, jobs, docker, router, service, worker = system
    apps.edit("demo", {**config(), "domain": "changed.apps.example.com"})
    assert router.routes == {"demo.apps.example.com": "old"}
    router.fail = True
    jobs.enqueue("demo", "deploy"); worker.step()
    assert router.routes == {"demo.apps.example.com": "old"}
    router.fail = False
    jobs.enqueue("demo", "deploy"); worker.step()
    assert set(router.routes) == {"changed.apps.example.com"}


def test_recovery_after_switch_restores_previous_deployment(system):
    apps, jobs, docker, router, service, worker = system
    router.crash = True
    jobs.enqueue("demo", "deploy")
    with pytest.raises(Crash): worker.step()
    assert "old" in docker.containers
    router.crash = False
    worker.recover()
    assert router.routes == {"demo.apps.example.com": "old"}
    assert set(docker.containers) == {"old"}
    assert len(jobs.list("pending")) == 1
    worker.step()
    assert jobs.list()[0].state == "done"


def test_recovery_after_commit_keeps_new_container(system):
    apps, jobs, docker, router, service, worker = system
    original_remove = docker.remove
    def crash(name, log):
        if name == "old": raise Crash()
        original_remove(name, log)
    docker.remove = crash
    jobs.enqueue("demo", "deploy")
    with pytest.raises(Crash): worker.step()
    active = apps.get("demo")["active"]
    assert jobs.list()[0].stage == "committed"
    docker.remove = original_remove
    worker.recover()
    assert set(docker.containers) == {active["container"]}
    assert router.routes[active["domain"]] == active["container"]
    assert jobs.list()[0].state == "done"


def test_recovery_before_candidate_start_requeues_safely(system):
    apps, jobs, docker, router, service, worker = system
    docker.on_build = lambda: (_ for _ in ()).throw(Crash())
    jobs.enqueue("demo", "deploy")
    with pytest.raises(Crash): worker.step()
    worker.recover()
    assert set(docker.containers) == {"old"}
    assert len(jobs.list("pending")) == 1


def test_recovery_keeps_old_when_committed_candidate_is_dead(system):
    apps, jobs, docker, router, service, worker = system
    original_remove = docker.remove
    docker.remove = lambda name, log: (_ for _ in ()).throw(Crash()) if name == "old" else original_remove(name, log)
    jobs.enqueue("demo", "deploy")
    with pytest.raises(Crash): worker.step()
    candidate = apps.get("demo")["active"]["container"]
    docker.containers[candidate]["Status"] = "exited"
    docker.remove = original_remove
    worker.recover()
    assert set(docker.containers) == {"old"}
    assert apps.get("demo")["active"]["container"] == "old"
    assert router.routes == {"demo.apps.example.com": "old"}


def test_failed_delete_is_recoverable_without_reenabling_deploy(system):
    apps, jobs, docker, router, service, worker = system
    original_remove = router.remove
    router.remove = lambda *args: (_ for _ in ()).throw(RuntimeError("temporary routing failure"))
    jobs.enqueue("demo", "delete")
    with pytest.raises(RuntimeError): worker.step()
    assert jobs.list()[0].state == "running"
    assert apps.get("demo")["delete_requested"]
    router.remove = original_remove
    worker.recover(); worker.step()
    assert apps.get("demo") is None


def test_inflight_domain_stays_reserved_after_another_edit(system):
    apps, jobs, docker, router, service, worker = system
    apps.edit("demo", {**config(), "domain":"target.apps.example.com"})
    def edit_during_build():
        apps.edit("demo", {**config(), "domain":"later.apps.example.com"})
        with pytest.raises(ValueError): apps.create(config("other", "target.apps.example.com"))
    docker.on_build = edit_during_build
    jobs.enqueue("demo", "deploy"); worker.step()
    assert apps.get("demo")["active"]["domain"] == "target.apps.example.com"
    assert apps.get("demo")["domain"] == "later.apps.example.com"


def test_candidate_becoming_unhealthy_during_routing_does_not_displace_old(system):
    apps, jobs, docker, router, service, worker = system
    def unhealthy():
        for name in docker.containers:
            if name != "old": docker.containers[name]["Health"] = {"Status":"unhealthy"}
    router.on_switch = unhealthy
    jobs.enqueue("demo", "deploy"); worker.step()
    assert set(docker.containers) == {"old"}
    assert router.routes == {"demo.apps.example.com":"old"}


def test_claim_freezes_configuration_before_concurrent_edit(system):
    apps, jobs, docker, router, service, worker = system
    jobs.enqueue("demo", "deploy")
    job = jobs.claim()
    apps.edit("demo", {**config(), "branch":"later"})
    service.execute(job)
    assert apps.get("demo")["active"]["branch"] == "main"


def test_delete_then_recreate_does_not_reuse_previous_logs(system):
    apps, jobs, docker, router, service, worker = system
    with service.log("demo") as log:
        log.write("OLD DEPLOYMENT HISTORY\n")
    jobs.enqueue("demo", "delete")
    worker.step()
    apps.create(config(), deploy=True)
    worker.step()
    text = (service.settings.data_dir / "logs/demo.log").read_text()
    assert "OLD DEPLOYMENT HISTORY" not in text
    assert "Deployment succeeded" in text


def test_failed_first_deploy_records_startup_logs_without_claiming_previous(system):
    apps, jobs, docker, router, service, worker = system
    jobs.enqueue("demo", "delete")
    worker.step()
    apps.create({**config(), "env": "PASSWORD=sensitive-value"}, deploy=True)
    service.readiness = lambda *args, **kwargs: False
    def logs(container, log, secrets=()):
        from deployer.integrations.commands import redact
        assert container in docker.containers
        log.write(redact("database initialization failed: sensitive-value\n", secrets))
    docker.logs = logs
    worker.step()
    app = apps.get("demo")
    assert app["status"] == "failed"
    assert "previous deployment retained" not in app["error"]
    text = (service.settings.data_dir / "logs/demo.log").read_text()
    assert "database initialization failed" in text
    assert "sensitive-value" not in text


def test_internal_readiness_waits_through_initial_container_state(system):
    apps, jobs, docker, router, service, worker = system
    docker.containers["starting-db"] = {"Status": "created"}
    checked = []
    service.readiness = lambda *args, **kwargs: checked.append(args[1]) or True
    assert service.ready("starting-db", {"is_public": False, "port": 5432, "network": "backend"})
    assert checked == ["starting-db"]


def test_first_deployment_losing_readiness_during_routing_has_no_previous(system):
    apps, jobs, docker, router, service, worker = system
    jobs.enqueue("demo", "delete")
    worker.step()
    apps.create(config(), deploy=True)
    results = iter([True, False])
    service.readiness = lambda *args, **kwargs: next(results)
    worker.step()
    app = apps.get("demo")
    assert app["status"] == "failed"
    assert "previous deployment retained" not in app["error"].lower()
    assert not router.routes
