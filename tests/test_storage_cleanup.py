import io
import json
from pathlib import Path

import pytest

from deployer.config import Settings
from deployer.integrations.commands import CommandError
from deployer.integrations.docker import Docker
from deployer.integrations.git import GitRepository
from test_deployment import Crash, FakeDocker, FakeGit, FakeRouter, system
from test_storage import config, store


def test_volume_delete_failure_is_not_reported_as_success(tmp_path):
    class Runner:
        def run(self, cmd, *args, **kwargs):
            if cmd[1:3] == ["volume", "ls"]:
                return "deployer-data-demo\n"
            if kwargs.get("check", True):
                raise CommandError("volume is in use")
            return "volume is in use"
    docker = Docker(Settings(tmp_path, "apps.example.com", "email", "password", "secret"), Runner())
    with pytest.raises(CommandError):
        docker.remove_volume("deployer-data-demo")


def test_container_removal_cleans_anonymous_volumes(tmp_path):
    commands = []
    class Runner:
        def run(self, cmd, *args, **kwargs):
            commands.append(cmd)
            return '{"Status":"exited"}'
    Docker(Settings(tmp_path, "apps.example.com", "email", "password", "secret"), Runner()).remove("candidate", io.StringIO())
    assert "-v" in commands[-1]


def test_build_cleanup_failure_keeps_job_recoverable(system):
    apps, jobs, docker, router, service, worker = system
    service.repository.cleanup = lambda job_id: (_ for _ in ()).throw(Crash())
    jobs.enqueue("demo", "deploy")
    with pytest.raises(Crash):
        worker.step()
    assert jobs.list()[0].state == "running"
    assert jobs.list()[0].stage == "committed"


def test_delete_image_app_also_removes_old_git_checkout(system):
    apps, jobs, docker, router, service, worker = system
    removed = []
    service.repository.remove = lambda name: removed.append(name)
    apps.edit("demo", {**config(), "method": "image", "image": "postgres:16"})
    jobs.enqueue("demo", "delete")
    worker.step()
    assert removed == ["demo"]


def test_finished_jobs_discard_secret_payloads_and_bound_history(tmp_path):
    apps, jobs = store(tmp_path)
    apps.create(config())
    for _ in range(6):
        job_id = jobs.enqueue("demo", "deploy")
        jobs.claim()
        jobs.finish(job_id)
    jobs.prune_history(keep=2)
    assert len(jobs.list()) == 2
    assert all(job.payload == {} for job in jobs.list())
    running_id = jobs.enqueue("demo", "deploy")
    jobs.claim()
    jobs.enqueue("demo", "restart")
    jobs.prune_history(keep=0)
    assert {job.state for job in jobs.list()} == {"pending", "running"}
    assert jobs.get(running_id).payload["config"]["env"]


def test_app_delete_removes_old_job_history(tmp_path):
    apps, jobs = store(tmp_path)
    apps.create(config(), deploy=True)
    first = jobs.claim()
    jobs.finish(first.id)
    delete_id = jobs.enqueue("demo", "delete")
    jobs.claim()
    apps.remove("demo")
    assert [job.id for job in jobs.list()] == [delete_id]


def test_deployment_log_has_bounded_storage(system):
    apps, jobs, docker, router, service, worker = system
    with service.log("demo") as log:
        log.write("old line\n" * 200000)
        log.write("latest line\n")
    path = service.settings.data_dir / "logs/demo.log"
    assert path.stat().st_size <= 1024 * 1024
    assert path.read_text().endswith("latest line\n")


def test_repository_cleanup_errors_propagate(tmp_path, monkeypatch):
    repo = GitRepository(tmp_path, None, None)
    (tmp_path / "builds/1").mkdir(parents=True)
    def fail(*args, **kwargs):
        if not kwargs.get("ignore_errors", False):
            raise PermissionError("cannot delete build")
    monkeypatch.setattr("deployer.integrations.git.shutil.rmtree", fail)
    with pytest.raises(PermissionError):
        repo.cleanup(1)


def test_orphan_sweep_preserves_live_builds_and_repos(tmp_path):
    repo = GitRepository(tmp_path, None, None)
    for name in ("builds/1", "builds/2", "repos/live", "repos/deleted"):
        (tmp_path / name).mkdir(parents=True)
    repo.cleanup_orphans({2}, {"live"})
    assert not (tmp_path / "builds/1").exists()
    assert not (tmp_path / "repos/deleted").exists()
    assert (tmp_path / "builds/2").exists()
    assert (tmp_path / "repos/live").exists()


def test_readiness_uses_network_ip_and_logs_wrong_port(tmp_path, monkeypatch):
    from deployer.deployment.readiness import wait_ready
    addresses = []
    class Adapter:
        def inspect(self, container, **kwargs):
            return {"Status": "running"}
        def ensure_readiness_network(self, network, **kwargs): return None
        def readiness_address(self, container, network, **kwargs): return "172.20.0.8"
    def connect(address, **kwargs):
        addresses.append(address)
        raise ConnectionRefusedError("connection refused")
    monkeypatch.setattr("deployer.deployment.readiness.socket.create_connection", connect)
    log = io.StringIO()
    assert not wait_ready(Adapter(), "candidate", 80, network="backend", timeout=.01, log=log)
    assert addresses and all(address == ("172.20.0.8", 80) for address in addresses)
    assert "80" in log.getvalue() and "connection refused" in log.getvalue()


def test_unused_app_images_are_removed_without_global_prune(tmp_path):
    commands = []
    class Runner:
        def run(self, cmd, *args, **kwargs):
            commands.append(cmd)
            if cmd[1:3] == ["image", "ls"]:
                return "deployer-demo:old\ndeployer-demo:live\n"
            if cmd[1] == "ps":
                return "container-id\n" if "ancestor=deployer-demo:live" in cmd else ""
            return ""
    docker = Docker(Settings(tmp_path, "apps.example.com", "email", "password", "secret"), Runner())
    docker.cleanup_images("demo", io.StringIO())
    assert [cmd for cmd in commands if cmd[1:3] == ["image", "rm"]] == [["docker", "image", "rm", "deployer-demo:old"]]
    assert not any("prune" in cmd for cmd in commands)


def test_build_cache_uses_an_isolated_builder(tmp_path):
    commands = []
    class Runner:
        def run(self, cmd, *args, **kwargs):
            commands.append(cmd)
            return ""
    docker = Docker(Settings(tmp_path, "apps.example.com", "email", "password", "secret"), Runner())
    docker.build("deployer-demo:sha", tmp_path, io.StringIO())
    docker.cleanup_build_cache(io.StringIO())
    build = next(cmd for cmd in commands if "build" in cmd)
    assert build[1:3] == ["buildx", "build"]
    assert "--load" in build and "--builder" in build
    prune = next(cmd for cmd in commands if "prune" in cmd)
    assert prune[prune.index("--builder") + 1] == build[build.index("--builder") + 1]
    assert "--max-used-space" in prune and "2GB" in prune


def test_orphan_cleanup_runs_on_worker_recovery(system):
    apps, jobs, docker, router, service, worker = system
    swept = []
    service.repository.cleanup_orphans = lambda live_jobs, live_apps: swept.append((live_jobs, live_apps))
    worker.recover()
    assert swept == [(set(), {"demo"})]


def test_pulled_image_tracking_survives_finished_job(tmp_path):
    apps, jobs = store(tmp_path)
    jobs.track_image("sha256:new-image", "postgres:16")
    assert jobs.tracked_images() == [{"image_id": "sha256:new-image", "reference": "postgres:16"}]
    jobs.forget_image("sha256:new-image")
    assert jobs.tracked_images() == []


def test_network_delete_failure_retains_database_record(tmp_path):
    from deployer.storage.networks import NetworkStore
    apps, jobs = store(tmp_path)
    networks = NetworkStore(apps.db)
    networks.create("backend")
    def fail(name):
        raise CommandError("network is in use")
    with pytest.raises(CommandError):
        networks.delete("backend", cleanup=fail)
    assert networks.get("backend")


def test_command_output_does_not_spool_unbounded_scratch(tmp_path, monkeypatch):
    import subprocess
    import sys
    from deployer.integrations.commands import CommandRunner
    real_popen = subprocess.Popen
    def popen(*args, **kwargs):
        assert kwargs["stdout"] == subprocess.PIPE
        return real_popen(*args, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", popen)
    result = CommandRunner().run([sys.executable, "-c", "print('x' * (3 * 1024 * 1024)); print('last line')"])
    assert len(result) <= 2 * 1024 * 1024
    assert result.splitlines()[-1] == "last line"


def test_deleted_app_log_file_is_removed(system):
    apps, jobs, docker, router, service, worker = system
    with service.log("demo") as log:
        log.write("deployment output")
    jobs.enqueue("demo", "delete")
    worker.step()
    assert not (service.settings.data_dir / "logs/demo.log").exists()


def test_cleanup_refuses_symlinked_managed_root(tmp_path):
    repo = GitRepository(tmp_path / "data", None, None)
    outside = tmp_path / "outside"
    (outside / "1").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    try:
        (tmp_path / "data/builds").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is unavailable on this host")
    with pytest.raises(ValueError):
        repo.cleanup(1)
    assert (outside / "1").exists()


def test_delete_recovery_preserves_requested_volume_removal(system):
    apps, jobs, docker, router, service, worker = system
    original = docker.remove_volume
    docker.remove_volume = lambda *args: (_ for _ in ()).throw(CommandError("temporary failure"))
    job_id = jobs.enqueue("demo", "delete", payload={"delete_volume": True})
    with pytest.raises(RuntimeError):
        worker.step()
    docker.remove_volume = original
    worker.recover()
    assert jobs.get(job_id).payload["delete_volume"] is True


@pytest.mark.parametrize("reference,tags", [("postgres", ["postgres:latest"]), ("docker.io/library/postgres:16", ["postgres:16"]), ("postgres:16", ["postgres:16", "other:latest"])])
def test_pulled_cleanup_handles_canonical_names_without_removing_other_tags(tmp_path, reference, tags):
    commands = []
    class Runner:
        def run(self, cmd, *args, **kwargs):
            commands.append(cmd)
            if "{{.Id}}" in cmd: return "sha256:test-image"
            if "{{json .RepoTags}}" in cmd: return json.dumps(tags)
            return ""
    docker = Docker(Settings(tmp_path, "apps.example.com", "email", "password", "secret"), Runner())
    removed = docker.remove_unused_image("sha256:test-image", reference, io.StringIO())
    assert removed is (len(tags) == 1)
    removals = [cmd for cmd in commands if cmd[1:3] == ["image", "rm"]]
    assert bool(removals) is (len(tags) == 1)


def test_tracking_preserves_multiple_owned_tags_for_one_image(tmp_path):
    apps, jobs = store(tmp_path)
    jobs.track_image("sha256:same", "postgres:16")
    jobs.track_image("sha256:same", "postgres:16-alias")
    assert {row["reference"] for row in jobs.tracked_images()} == {"postgres:16", "postgres:16-alias"}


def test_readiness_connects_to_real_tcp_listener_without_hostname_resolution():
    import socket
    from deployer.deployment.readiness import wait_ready
    class Adapter:
        def inspect(self, container, **kwargs): return {"Status": "running"}
        def ensure_readiness_network(self, network, **kwargs): return None
        def readiness_address(self, container, network, **kwargs): return "127.0.0.1"
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        assert wait_ready(Adapter(), "unresolvable-container-name", server.getsockname()[1], network="backend", timeout=1)
