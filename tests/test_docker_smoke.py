"""Opt-in integration test against disposable containers, without real ACME."""
import io
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
import requests

pytestmark = [pytest.mark.docker, pytest.mark.skipif(os.environ.get("RUN_DOCKER_TESTS") != "1",
                                                  reason="requires an isolated Docker test host")]


def test_nginx_switch_and_failed_candidate_preserve_previous_app(tmp_path):
    from deployer.config import Settings
    from deployer.integrations.commands import CommandRunner
    from deployer.integrations.nginx import NginxRouter, MAP

    if not shutil.which("docker"):
        pytest.fail("RUN_DOCKER_TESTS=1 but Docker CLI is unavailable")
    prefix = "deployer-test-" + uuid.uuid4().hex[:12]
    network, gateway, old, candidate = (prefix + suffix for suffix in ("-net", "-nginx", "-old", "-new"))
    image = "nginx:1.30.5-alpine"
    conf = tmp_path / "conf"; conf.mkdir()
    (conf / "00-upgrade-map.conf").write_text(MAP)
    settings = Settings(tmp_path, "apps.example.com", "test@example.com", "password", "secret", conf_dir=conf)
    base_runner = CommandRunner(60)
    def docker(*args):
        return base_runner.run(["docker", *args])
    class Runner:
        def run(self, cmd, log=None, **kwargs):
            if cmd[:3] == ["docker", "exec", "nginx"]:
                cmd = [*cmd[:2], gateway, *cmd[3:]]
            return base_runner.run(cmd, log, **kwargs)
    router = NginxRouter(settings, Runner())
    domain = "demo.apps.example.com"
    def switch(container):
        router._write(conf / f"{domain}.conf", f"server {{ listen 80; server_name {domain};\n" +
            f"add_header X-Deployer-Deployment {container} always;\n" + router._proxy(container, 80) + "}\n")
        router.reload(io.StringIO())
    try:
        docker("network", "create", network)
        for name in (old, candidate):
            docker("run", "-d", "--name", name, "--network", network, image)
        docker("run", "-d", "--name", gateway, "--network", network, "-p", "127.0.0.1::80",
               "-v", f"{conf.resolve()}:/etc/nginx/conf.d", image)
        address = docker("port", gateway, "80/tcp").strip().splitlines()[0]
        def reachable(container):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    response = requests.get("http://" + address, headers={"Host": domain}, timeout=2)
                    if response.status_code == 200 and response.headers.get("X-Deployer-Deployment") == container:
                        return True
                except requests.RequestException:
                    pass
                time.sleep(.2)
            return False
        switch(old); assert reachable(old)
        snapshot = router.snapshot([domain])
        switch(candidate); assert reachable(candidate)
        router.restore(snapshot, io.StringIO()); assert reachable(old)
        # An invalid replacement configuration must not displace the serving route.
        (conf / f"{domain}.conf").write_text("invalid configuration;")
        with pytest.raises(RuntimeError): router.reload(io.StringIO())
        assert reachable(old)
        router.restore(snapshot, io.StringIO()); assert reachable(old)
    finally:
        for name in (gateway, candidate, old):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
        subprocess.run(["docker", "network", "rm", network], capture_output=True, timeout=30)
