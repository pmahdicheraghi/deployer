import io
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_commands_redact_raw_encoded_and_environment_secrets():
    from deployer.integrations.commands import CommandRunner
    log = io.StringIO()
    runner = CommandRunner()
    runner.run([sys.executable, "-c", "print('synthetic/token+secret synthetic%2Ftoken%2Bsecret env-secret')"],
        log, secrets=["synthetic/token+secret", "env-secret"], description="build")
    assert "secret" not in log.getvalue()
    assert "[redacted]" in log.getvalue()


def test_commands_kill_timed_out_process():
    from deployer.integrations.commands import CommandRunner, CommandError
    with pytest.raises(CommandError, match="timed out"):
        CommandRunner().run([sys.executable, "-c", "import time; time.sleep(20)"], io.StringIO(), timeout=.1)


def test_repository_checkout_never_stores_credentials_or_builds_git_metadata(tmp_path):
    from deployer.integrations.git import GitRepository
    class LocalGit:
        def run(self, cmd, log=None, **kwargs):
            if "fetch" in cmd:
                assert kwargs["env"]["GIT_AUTH_TOKEN"] == "synthetic/token+secret"
                assert "synthetic/token+secret" not in " ".join(cmd)
                cmd = [*cmd]; cmd[cmd.index("origin")] = str(source)
            result = subprocess.run(cmd, capture_output=True, text=False, check=True)
            return result.stdout.decode()
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    for filename in ("Dockerfile", ".env", "github-app-private-key.pem", ".env.example"):
        (source / filename).write_text("synthetic")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "-qm", "initial"], check=True)
    checkout = GitRepository(tmp_path / "data", LocalGit(), None)
    sha, context = checkout.prepare(dict(name="demo",method="manual",provider="github",
        token="synthetic/token+secret",repo_url="https://example.com/repo.git",branch="main"), 1, io.StringIO())
    assert len(sha) == 40
    assert "secret" not in (tmp_path / "data/repos/demo/.git/config").read_text()
    assert not (context / ".git").exists()
    assert not (context / ".env").exists()
    assert not (context / "github-app-private-key.pem").exists()
    assert (context / ".env.example").exists()


def test_settings_require_explicit_session_secret(tmp_path):
    from deployer.config import Settings
    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings.from_env(dict(DATA_DIR=str(tmp_path),APP_BASE_DOMAIN="apps.example.com",
            ACME_EMAIL="test@example.com",ADMIN_PASSWORD="password"))


@pytest.mark.parametrize("state,healthy,expected", [
    ("running", "healthy", True), ("running", "unhealthy", False), ("exited", None, False),
])
def test_readiness_honors_container_health(state, healthy, expected):
    from deployer.deployment.readiness import wait_ready
    class Docker:
        def inspect(self, name, **kwargs):
            return {"Status": state, **({"Health": {"Status": healthy}} if healthy else {})}
    assert wait_ready(Docker(), "candidate", 80, timeout=.01) is expected


def test_nginx_restores_prior_files_on_switch_failure(tmp_path):
    from deployer.integrations.nginx import NginxRouter
    from types import SimpleNamespace
    settings = SimpleNamespace(conf_dir=tmp_path, le_dir=tmp_path / "certs", network="web",
        le_volume="le", www_volume="www", acme_email="test@example.com", command_timeout=1, cert_timeout=1,
        certbot_image="certbot/certbot:v5.1.0")
    (tmp_path / "demo.apps.example.com.conf").write_text("previous")
    class Runner:
        def run(self, cmd, *args, **kwargs):
            if "certonly" in cmd: raise RuntimeError("certificate failure")
            return ""
    router = NginxRouter(settings, Runner())
    snapshot = router.snapshot(["demo.apps.example.com"])
    with pytest.raises(RuntimeError):
        router.provision("demo.apps.example.com", "candidate", 80, io.StringIO())
    router.restore(snapshot, io.StringIO())
    assert (tmp_path / "demo.apps.example.com.conf").read_text() == "previous"


def test_docker_inspection_does_not_treat_daemon_failure_as_missing(tmp_path):
    from deployer.integrations.docker import Docker
    from deployer.config import Settings
    class Runner:
        def run(self, *a, **kw): return "Cannot connect to the Docker daemon"
    with pytest.raises(RuntimeError):
        Docker(Settings(tmp_path,"apps.example.com","email","password","secret"),Runner()).inspect("candidate")


def test_command_redactor_removes_legacy_credential_urls():
    from deployer.integrations.commands import redact
    assert "synthetic-token" not in redact("fatal: https://x-access-token:synthetic-token@github.com/repo.git")


def test_existing_remote_is_scrubbed_without_fetching_or_building(tmp_path):
    from deployer.integrations.git import GitRepository
    from deployer.integrations.commands import CommandRunner
    repo = tmp_path / "repos/demo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    "https://x-access-token:synthetic-secret@example.com/repo.git"], check=True)
    repository = GitRepository(tmp_path, CommandRunner(), None)
    repository.sanitize_remote(dict(name="demo",method="manual",repo_url="https://example.com/repo.git"))
    assert "synthetic-secret" not in (repo / ".git/config").read_text()


def test_command_redaction_does_not_leak_secret_at_output_tail_boundary():
    from deployer.integrations.commands import CommandRunner
    log = io.StringIO()
    secret = "boundary-synthetic-secret"
    script = "import sys; sys.stdout.write('" + secret + "' + 'x' * (2 * 1024 * 1024 - 20))"
    CommandRunner().run([sys.executable, "-c", script], log, secrets=[secret])
    assert "synthetic-secret" not in log.getvalue()


def test_readiness_inspection_uses_remaining_deadline():
    from deployer.deployment.readiness import wait_ready
    observed = []
    class Docker:
        def inspect(self, container, **kwargs):
            observed.append(kwargs.get("timeout"))
            return {"Status":"missing"}
    assert not wait_ready(Docker(), "candidate", 80, timeout=.01)
    assert observed[0] is not None and observed[0] <= .01


def test_timed_out_certbot_container_is_removed(tmp_path):
    from deployer.config import Settings
    from deployer.integrations.nginx import NginxRouter
    commands = []
    class Runner:
        def run(self, cmd, *args, **kw):
            commands.append(cmd)
            if "certonly" in cmd: raise RuntimeError("certificate timeout")
            return ""
    settings = Settings(tmp_path,"apps.example.com","email","password","secret",
                        conf_dir=tmp_path / "conf",le_dir=tmp_path / "certs")
    with pytest.raises(RuntimeError):
        NginxRouter(settings, Runner()).provision("demo.apps.example.com", "candidate", 80, io.StringIO())
    assert commands[-1][:3] == ["docker","rm","-f"]
