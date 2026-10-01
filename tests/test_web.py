import hashlib
import hmac
import json
import re

import pytest

from test_storage import config


@pytest.fixture
def web(tmp_path):
    from deployer import create_app
    from deployer.config import Settings
    from test_deployment import FakeDocker, FakeRouter, FakeGit
    settings = Settings(tmp_path, "apps.example.com", "test@example.com", "password", "session-secret",
                        panel_domain="deploy.example.com", conf_dir=tmp_path / "conf")
    application = create_app(settings, dependencies={"docker": FakeDocker(), "router": FakeRouter(),
                             "repository": FakeGit(tmp_path)})
    application.config["TESTING"] = True
    services = application.extensions["deployer"]
    services["apps"].create(config())
    client = application.test_client()
    with client.session_transaction(base_url="https://deploy.example.com") as session:
        session["auth"] = True
    return client, services


def csrf(client, path="/apps/demo"):
    response = client.get(path, base_url="https://deploy.example.com")
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


@pytest.mark.parametrize("image,user_port,expected_port", [("postgres:16", None, "5432"), ("postgres:16", "5544", "5544"), ("other/postgres:16", None, "80"), ("postgres,postgres:16", None, "5432")])
def test_new_postgres_image_suggests_service_settings(web, image, user_port, expected_port):
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute form JavaScript")
    client, services = web
    page = client.get("/apps/new", base_url="https://deploy.example.com").text
    function = re.search(r"function onImageChange\(\)\{.*?\n\}", page, re.S).group()
    harness = "const fields = " + json.dumps({
        "image": {"value": image}, "port": {"value": user_port or "80", "dataset": {"userSet": "1"} if user_port else {}},
        "is_public": {"checked": True, "dataset": {}}, "stateful": {"checked": False, "dataset": {}},
        "mount_path": {"value": "", "dataset": {}}, "method": {"value": "image"}
    }) + "; const document={getElementById:id=>fields[id]}; function suggestName(){}; function togglePublic(){}; function toggleStateful(){};"
    run = "for(const image of " + json.dumps(image.split(",")) + "){fields.image.value=image;onImageChange();}console.log(JSON.stringify(fields));"
    result = subprocess.run([node], input=harness + function + "\n" + run, text=True, capture_output=True, check=True)
    fields = json.loads(result.stdout)
    assert fields["port"]["value"] == expected_port
    if image.endswith("postgres:16") and not image.startswith("other/"):
        assert fields["is_public"]["checked"] is False
        assert fields["stateful"]["checked"] is True
        assert fields["mount_path"]["value"] == "/var/lib/postgresql/data"


@pytest.mark.parametrize("image,mount", [("postgres:18-alpine", "/var/lib/postgresql"), ("postgres:alpine", ""), ("postgres@sha256:abcdef", "")])
def test_postgres_mount_defaults_require_known_version(web, image, mount):
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute form JavaScript")
    client, services = web
    page = client.get("/apps/new", base_url="https://deploy.example.com").text
    function = re.search(r"function onImageChange\(\)\{.*?\n\}", page, re.S).group()
    fields = {"image": {"value": image}, "method": {"value": "image"}, "port": {"value": "80", "dataset": {}},
              "is_public": {"checked": True, "dataset": {}}, "stateful": {"checked": False, "dataset": {}},
              "mount_path": {"value": "", "dataset": {}}}
    harness = "const fields=" + json.dumps(fields) + ";const document={getElementById:id=>fields[id]};function suggestName(){};function togglePublic(){};function toggleStateful(){};"
    result = subprocess.run([node], input=harness + function + "\nonImageChange();console.log(JSON.stringify(fields));", text=True, capture_output=True, check=True)
    assert json.loads(result.stdout)["mount_path"]["value"] == mount


@pytest.mark.parametrize("action", ["deploy", "stop", "start", "restart", "delete", "edit"])
def test_admin_actions_require_csrf_token(web, action):
    client, services = web
    response = client.post("/apps/demo/" + action, base_url="https://deploy.example.com")
    assert response.status_code == 400
    assert services["jobs"].list() == []
    assert not services["apps"].get("demo")["delete_requested"]


def test_sibling_origin_post_is_rejected_even_with_token(web):
    client, services = web
    response = client.post("/apps/demo/stop", base_url="https://deploy.example.com",
        data={"csrf_token": csrf(client)}, headers={"Origin":"https://myapp.apps.example.com"})
    assert response.status_code == 403
    assert services["jobs"].list() == []


def test_valid_admin_action_persists_job_without_executing_docker(web):
    client, services = web
    response = client.post("/apps/demo/deploy", base_url="https://deploy.example.com",
                           data={"csrf_token": csrf(client)})
    assert response.status_code == 302
    assert services["jobs"].list()[0].state == "pending"
    assert set(services["docker"].containers) == {"old"}


def test_domain_edit_keeps_active_route(web):
    client, services = web
    response = client.post("/apps/demo/edit", base_url="https://deploy.example.com", data={
        "csrf_token": csrf(client), "method":"manual", "repo_url":"https://example.com/demo.git",
        "subdomain":"changed", "port":"80", "branch":"main"})
    assert response.status_code == 302
    assert services["router"].routes == {"demo.apps.example.com":"old"}
    assert services["jobs"].list() == []


def test_callback_without_state_does_not_connect_installation(web):
    client, services = web
    response = client.get("/github/callback?installation_id=123", base_url="https://deploy.example.com")
    assert response.status_code == 302
    assert services["apps"].installations() == {}


def test_github_callback_consumes_valid_state_once(web, monkeypatch):
    client, services = web
    monkeypatch.setattr(type(services["settings"]), "github_enabled", property(lambda self: True))
    monkeypatch.setattr(services["github"], "installation", lambda id: {"account":{"login":"owner"}})
    client.get("/connect/github", base_url="https://deploy.example.com")
    with client.session_transaction(base_url="https://deploy.example.com") as session: state = session["gh_state"]
    client.get(f"/github/callback?installation_id=123&state={state}", base_url="https://deploy.example.com")
    assert services["apps"].installations()["123"]["account"] == "owner"
    with client.session_transaction(base_url="https://deploy.example.com") as session: assert "gh_state" not in session


def test_signed_webhook_needs_no_csrf_and_coalesces_followup(web):
    client, services = web
    body = json.dumps({"ref":"refs/heads/main"}).encode()
    signature = "sha256=" + hmac.new(b"hook", body, hashlib.sha256).hexdigest()
    def send():
        return client.post("/webhook/demo", base_url="https://deploy.example.com", data=body,
            content_type="application/json", headers={"X-Hub-Signature-256":signature})
    assert send().status_code == 202
    services["jobs"].claim()
    assert send().status_code == 202
    assert send().status_code == 202
    assert len(services["jobs"].list("pending")) == 1
    bad = client.post("/webhook/demo", base_url="https://deploy.example.com", data=body, headers={"X-Hub-Signature-256":"sha256=wrong"})
    assert bad.status_code == 401


def test_login_requires_csrf_and_issues_authentication(web):
    client, services = web
    client = client.application.test_client()
    assert client.post("/login", data={"password":"password"}).status_code == 400
    token = csrf(client, "/login")
    response = client.post("/login", base_url="https://deploy.example.com",
        data={"password":"password", "csrf_token":token})
    assert response.status_code == 302
    with client.session_transaction(base_url="https://deploy.example.com") as session: assert session["auth"]


@pytest.mark.parametrize("field,value", [("repo_url","https://user:secret@example.com/repo.git"),
    ("branch","--upload-pack=bad"), ("port","0"), ("health_path","https://evil.example"),
    ("env","BAD KEY=value"), ("method","unknown")])
def test_invalid_configuration_is_rejected(web, field, value):
    client, services = web
    data = dict(method="manual",repo_url="https://example.com/demo.git",subdomain="demo",port="80",branch="main",
                csrf_token=csrf(client))
    data[field] = value
    response = client.post("/apps/demo/edit", base_url="https://deploy.example.com", data=data)
    assert response.status_code == 400
    assert services["apps"].get("demo")["repo_url"] == "https://example.com/repo.git"


def test_historical_log_secrets_are_redacted_when_served(web):
    client, services = web
    directory = services["settings"].data_dir / "logs"
    directory.mkdir()
    (directory / "demo.log").write_text("PASSWORD=synthetic-secret https://x-access-token:old-token@github.com/repo.git")
    response = client.get("/apps/demo/log.json", base_url="https://deploy.example.com")
    assert "synthetic-secret" not in response.json["log"]
    assert "old-token" not in response.json["log"]
