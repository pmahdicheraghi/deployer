import json
import pytest

from deployer.storage.database import Database
from deployer.storage.apps import AppStore
from deployer.storage.jobs import JobStore
from deployer.storage.networks import NetworkStore
from test_storage import config
from test_deployment import FakeDocker, FakeRouter, FakeGit


def test_network_store_crud(tmp_path):
    db = Database(tmp_path)
    store = NetworkStore(db, default_network="web")
    networks = store.list()
    assert any(n["name"] == "web" for n in networks)

    # Create network
    created = store.create("backend-net")
    assert created == "backend-net"
    assert any(n["name"] == "backend-net" for n in store.list())

    # Invalid names
    with pytest.raises(ValueError, match="lowercase letters, digits or dashes"):
        store.create("INVALID_NET")
    with pytest.raises(ValueError, match="already in use"):
        store.create("backend-net")

    # Cannot delete default
    with pytest.raises(ValueError, match="Cannot delete the default network"):
        store.delete("web")

    # Cannot delete network in use by app
    apps = AppStore(db)
    apps.create({**config("dbapp"), "network": "backend-net", "domain": "db.example.com"})
    with pytest.raises(ValueError, match="in use by app"):
        store.delete("backend-net")

    # After app removed, can delete
    apps.remove("dbapp")
    store.delete("backend-net")
    assert not any(n["name"] == "backend-net" for n in store.list())


def test_validation_for_docker_image_and_internal_service(tmp_path):
    from deployer import create_app
    from deployer.config import Settings
    from deployer.web.validation import validate

    settings = Settings(tmp_path, "apps.example.com", "test@example.com", "password", "session-secret",
                        panel_domain="deploy.example.com")
    application = create_app(settings, dependencies={"docker": FakeDocker(), "router": FakeRouter(),
                             "repository": FakeGit(tmp_path)})

    with application.app_context():
        # Valid image app, internal only
        cfg = validate({
            "name": "postgres",
            "method": "image",
            "image": "postgres:16-alpine",
            "is_public_present": "1",
            # is_public not checked -> internal only
            "network": "web",
            "port": "5432",
            "stateful": "1",
            "mount_path": "/var/lib/postgresql/data",
            "registry_user": "user",
            "registry_password": "pass"
        })
        assert cfg["method"] == "image"
        assert cfg["image"] == "postgres:16-alpine"
        assert cfg["is_public"] is False
        assert cfg["domain"] == ""
        assert cfg["subdomain"] == ""
        assert cfg["port"] == 5432
        assert cfg["stateful"] is True
        assert cfg["mount_path"] == "/var/lib/postgresql/data"
        assert cfg["registry_user"] == "user"
        assert cfg["registry_password"] == "pass"

        # Invalid image name
        with pytest.raises(ValueError, match="Invalid Docker image reference"):
            validate({"name": "bad", "method": "image", "image": "bad image with spaces", "port": "80", "is_public_present": "1"})

        # Stateful without absolute mount path
        with pytest.raises(ValueError, match="Mount path must be an absolute path"):
            validate({"name": "bad", "method": "image", "image": "postgres:16", "port": "5432",
                      "is_public_present": "1", "stateful": "1", "mount_path": "relative/path"})

        # Non-existent network
        with pytest.raises(ValueError, match="Selected network does not exist"):
            validate({"name": "bad", "method": "image", "image": "postgres:16", "port": "5432",
                      "is_public_present": "1", "network": "nonexistent-net"})


def test_web_routes_networks_and_volumes(tmp_path):
    from deployer import create_app
    from deployer.config import Settings

    class MockDocker(FakeDocker):
        def __init__(self):
            super().__init__()
            self.vols = ["deployer-data-demo", "deployer-data-orphaned"]
            self.nets = ["web"]
        def list_volumes(self, prefix="deployer-data-"):
            return [v for v in self.vols if v.startswith(prefix)]
        def remove_volume(self, volume, log=None):
            self.vols.remove(volume)
        def ensure_network(self, network, log=None):
            if network not in self.nets: self.nets.append(network)
        def remove_network(self, network, log=None):
            if network in self.nets: self.nets.remove(network)

    docker = MockDocker()
    settings = Settings(tmp_path, "apps.example.com", "test@example.com", "password", "session-secret",
                        panel_domain="deploy.example.com", conf_dir=tmp_path / "conf")
    application = create_app(settings, dependencies={"docker": docker, "router": FakeRouter(),
                             "repository": FakeGit(tmp_path)})
    application.config["TESTING"] = True
    services = application.extensions["deployer"]
    services["apps"].create({**config(), "stateful": True, "mount_path": "/data"})

    client = application.test_client()
    with client.session_transaction(base_url="https://deploy.example.com") as session:
        session["auth"] = True

    # Test /networks GET
    r = client.get("/networks", base_url="https://deploy.example.com")
    assert r.status_code == 200
    assert "web" in r.text

    # Test /networks/new POST
    from test_web import csrf
    token = csrf(client, "/networks")
    r = client.post("/networks/new", base_url="https://deploy.example.com",
                    data={"csrf_token": token, "name": "custom-net"})
    assert r.status_code == 302
    assert "custom-net" in docker.nets

    # Test /volumes GET
    r = client.get("/volumes", base_url="https://deploy.example.com")
    assert r.status_code == 200
    assert "deployer-data-demo" in r.text
    assert "deployer-data-orphaned" in r.text

    # Test deleting orphaned volume
    token = csrf(client, "/volumes")
    r = client.post("/volumes/deployer-data-orphaned/delete", base_url="https://deploy.example.com",
                    data={"csrf_token": token})
    assert r.status_code == 302
    assert "deployer-data-orphaned" not in docker.vols

    # Test deleting attached volume is blocked
    r = client.post("/volumes/deployer-data-demo/delete", base_url="https://deploy.example.com",
                    data={"csrf_token": token})
    assert r.status_code == 302
    assert "deployer-data-demo" in docker.vols

    # Delete app with delete_volume option
    token = csrf(client, "/apps/demo")
    r = client.post("/apps/demo/delete", base_url="https://deploy.example.com",
                    data={"csrf_token": token, "delete_volume": "1"})
    assert r.status_code == 302
    jobs = services["jobs"].list()
    delete_job = [j for j in jobs if j.kind == "delete"][0]
    assert delete_job.payload.get("delete_volume") is True


def test_deployment_service_docker_image_and_stateful(tmp_path):
    from deployer.config import Settings
    from deployer.deployment.service import DeploymentService
    from deployer.deployment.worker import Worker

    class TrackingDocker(FakeDocker):
        def __init__(self):
            super().__init__()
            self.pulled = []
            self.stopped = []
            self.started_candidate = None
            self.removed_vols = []
        def pull(self, image, log, auth=None):
            self.pulled.append((image, auth))
        def action(self, operation, name, log):
            if operation == "stop":
                self.stopped.append(name)
            super().action(operation, name, log)
        def start_candidate(self, name, image, conf, log):
            self.started_candidate = (name, image, conf)
            super().start_candidate(name, image, conf, log)
        def remove_volume(self, volume, log=None):
            self.removed_vols.append(volume)

    docker = TrackingDocker()
    router = FakeRouter()
    git = FakeGit(tmp_path)
    db = Database(tmp_path)
    apps, jobs = AppStore(db), JobStore(db)
    settings = Settings(tmp_path, "apps.example.com", "test@example.com", "password", "session-secret",
                        conf_dir=tmp_path / "conf")
    service = DeploymentService(settings, apps, jobs, git, docker, router)
    worker = Worker(service, apps, jobs)

    # 1. Image deployment (internal only, stateful)
    app_cfg = {
        "name": "my-pg",
        "method": "image",
        "image": "postgres:16",
        "is_public": False,
        "domain": "",
        "subdomain": "",
        "network": "web",
        "port": 5432,
        "stateful": True,
        "mount_path": "/var/lib/postgresql/data",
        "secret": "hook"
    }
    apps.create(app_cfg, deploy=True)
    worker.step()

    # Image was pulled, not built with git
    assert len(docker.pulled) == 1
    assert docker.pulled[0][0] == "postgres:16"
    assert docker.started_candidate[1] == "postgres:16"
    # Router was NOT provisioned because it is internal-only
    assert "" not in router.routes
    assert apps.get("my-pg")["status"] == "running"

    # 2. Redeploy stateful app with previous container: previous must be stopped before candidate starts
    jobs.enqueue("my-pg", "deploy")
    worker.step()
    # The previous container was stopped before candidate launched
    assert any("my-pg" in c for c in docker.stopped)

    # 3. Delete app with delete_volume
    jobs.enqueue("my-pg", "delete", payload={"delete_volume": True})
    worker.step()
    assert "deployer-data-my-pg" in docker.removed_vols
    assert apps.get("my-pg") is None


def test_docker_adapter_methods(tmp_path):
    from deployer.config import Settings
    from deployer.integrations.docker import Docker

    class MockRunner:
        def __init__(self):
            self.commands = []
        def run(self, cmd, log=None, **kwargs):
            self.commands.append((cmd, kwargs))
            cmd_str = " ".join(cmd)
            if "network inspect" in cmd_str:
                return "Error: No such network: custom-net"
            if "volume ls" in cmd_str:
                return "deployer-data-app1\ndeployer-data-app2\nother-vol\n"
            if "inspect" in cmd_str:
                return '{"Status": "running"}'
            return ""

    runner = MockRunner()
    settings = Settings(tmp_path, "apps.example.com", "test@example.com", "password", "session-secret")
    docker = Docker(settings, runner)

    # ensure_network
    docker.ensure_network("custom-net")
    assert any("network create" in " ".join(c[0]) for c in runner.commands)

    # remove_network
    docker.remove_network("custom-net")
    assert any("network rm" in " ".join(c[0]) for c in runner.commands)

    # connect_network
    docker.connect_network("web", "my-container", alias="my-alias")
    assert any("network connect --alias my-alias web my-container" in " ".join(c[0]) for c in runner.commands)

    # list_volumes
    vols = docker.list_volumes()
    assert vols == ["deployer-data-app1", "deployer-data-app2"]

    # remove_volume
    docker.remove_volume("deployer-data-app1")
    assert any("volume rm -f deployer-data-app1" in " ".join(c[0]) for c in runner.commands)

    # pull with auth
    docker.pull("ghcr.io/myorg/myimg:v1", None, auth={"user": "u", "password": "p"})
    assert any("login -u u ghcr.io --password-stdin" in " ".join(c[0]) for c in runner.commands)
    assert any("pull ghcr.io/myorg/myimg:v1" in " ".join(c[0]) for c in runner.commands)

    # start_candidate with custom network, alias, and stateful volume
    conf = {
        "name": "my-service",
        "network": "custom-net",
        "is_public": True,
        "stateful": True,
        "mount_path": "/var/lib/data",
        "env": "FOO=bar"
    }
    docker.start_candidate("candidate-1", "postgres:16", conf, None)
    run_cmds = [" ".join(c[0]) for c in runner.commands if "docker run" in " ".join(c[0])]
    assert len(run_cmds) == 1
    assert "--network custom-net" in run_cmds[0]
    assert "--network-alias my-service" in run_cmds[0]
    assert "-v deployer-data-my-service:/var/lib/data" in run_cmds[0]
    # And connects to web as secondary since is_public=True
    assert any("network connect web candidate-1" in " ".join(c[0]) for c in runner.commands)
