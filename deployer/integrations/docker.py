"""Docker operations; secrets are passed without logging argument lists."""
import json
import os
import socket

from .git import parse_env
from .commands import CommandError, redact


class Docker:
    builder = "deployer-builds"
    def __init__(self, settings, runner):
        self.settings, self.runner = settings, runner

    def inspect(self, container, *, timeout=None):
        text = self.runner.run(["docker", "inspect", "--type", "container", "-f", "{{json .State}}", container], check=False,
                               description="inspect container", timeout=timeout)
        try:
            state = json.loads(text)
        except (ValueError, TypeError):
            normalized = text.casefold()
            if "no such object" in normalized or "no such container" in normalized:
                return {"Status": "missing"}
        else:
            if isinstance(state, dict) and isinstance(state.get("Status"), str):
                return state
        diagnostic = redact(text.strip())[:1000] or "Docker returned no output."
        raise RuntimeError(f"Docker inspection failed for {container}; runtime state is unknown: {diagnostic}") from None

    def build(self, image, context, log, secrets=()):
        self.ensure_builder(log)
        self.runner.run(["docker", "buildx", "build", "--builder", self.builder, "--load",
                        "--label", "deployer.managed=1", "-t", image, str(context)], log,
            timeout=self.settings.build_timeout, secrets=secrets, description="build image")

    def ensure_builder(self, log):
        try:
            self.runner.run(["docker", "buildx", "inspect", self.builder], description="inspect app builder")
        except CommandError:
            self.runner.run(["docker", "buildx", "create", "--name", self.builder,
                             "--driver", "docker-container"], log, description="create isolated app builder")

    def cleanup_build_cache(self, log):
        # Only the deployer's dedicated cache is affected.
        self.ensure_builder(log)
        self.runner.run(["docker", "buildx", "prune", "--builder", self.builder, "--force",
                         "--all", "--max-used-space", "2GB"], log,
                        timeout=self.settings.build_timeout, description="bound app build cache")

    def image_id(self, image):
        text = self.runner.run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                               check=False, description="inspect image identity").strip()
        if text.startswith("sha256:"):
            return text
        if "no such image" in text.casefold() or "no such object" in text.casefold():
            return None
        raise RuntimeError("Could not determine Docker image identity: " + redact(text)[:1000])

    def remove_unused_image(self, image_id, reference, log):
        if not self.image_id(image_id):
            return True
        used = self.runner.run(["docker", "ps", "-aq", "--filter", f"ancestor={image_id}"],
                               description="check pulled image usage")
        if used.strip():
            return False
        text = self.runner.run(["docker", "image", "inspect", "--format", "{{json .RepoTags}}", image_id],
                               description="inspect image tags")
        tags = json.loads(text) or []
        owned = {self.canonical_image(ref) for ref in ([reference] if isinstance(reference, str) else reference)}
        if any(self.canonical_image(tag) not in owned for tag in tags):
            return False  # Leave tags belonging to other consumers alone.
        self.runner.run(["docker", "image", "rm", *(tags or [image_id])], log,
                        description="remove unused pulled image")
        return True

    @staticmethod
    def canonical_image(reference):
        for prefix in ("docker.io/", "index.docker.io/", "registry-1.docker.io/"):
            if reference.startswith(prefix):
                reference = reference[len(prefix):]
                break
        if reference.startswith("library/"):
            reference = reference[len("library/"):]
        if ":" not in reference.rsplit("/", 1)[-1] and "@" not in reference:
            reference += ":latest"
        return reference

    def pull(self, image, log, auth=None):
        secrets = []
        if auth and auth.get("password"):
            secrets.append(auth["password"])
            import tempfile
            with tempfile.TemporaryDirectory(prefix="deployer-docker-") as config_dir:
                server = image.split("/")[0] if "/" in image and ("." in image.split("/")[0] or ":" in image.split("/")[0]) else ""
                login_cmd = ["docker", "--config", config_dir, "login", "-u", auth.get("user", "")]
                if server:
                    login_cmd.append(server)
                login_cmd.append("--password-stdin")
                self.runner.run(login_cmd, log, stdin_text=auth["password"], secrets=secrets, description="docker registry login")
                self.runner.run(["docker", "--config", config_dir, "pull", image], log,
                                timeout=self.settings.build_timeout, secrets=secrets, description="pull image")
        else:
            self.runner.run(["docker", "pull", image], log,
                            timeout=self.settings.build_timeout, description="pull image")

    def ensure_network(self, network, log=None):
        try:
            self.runner.run(["docker", "network", "inspect", network], description="inspect network")
        except CommandError:
            self.runner.run(["docker", "network", "create", "--driver", "bridge", network], log,
                            description=f"create network {network}")

    def remove_network(self, network, log=None):
        self.runner.run(["docker", "network", "rm", network], log,
                        description=f"remove network {network}")

    def ensure_readiness_network(self, network, *, timeout=None):
        """Temporarily join the probe worker to a service's private bridge."""
        if not os.path.exists("/.dockerenv"):
            return None  # Host-based development uses its existing network access.
        worker = socket.gethostname()
        text = self.runner.run(["docker", "inspect", "--type", "container", "-f",
                                "{{json .NetworkSettings.Networks}}", worker],
                               timeout=timeout, description="inspect worker networks")
        networks = json.loads(text)
        if network in networks:
            return None
        self.runner.run(["docker", "network", "connect", network, worker],
                        timeout=timeout, description="connect readiness worker")
        return worker

    def release_readiness_network(self, network, worker):
        if worker:
            self.runner.run(["docker", "network", "disconnect", network, worker],
                            description="disconnect readiness worker")

    def connect_network(self, network, container, alias=None, log=None):
        cmd = ["docker", "network", "connect"]
        if alias:
            cmd.extend(["--alias", alias])
        cmd.extend([network, container])
        self.runner.run(cmd, log, check=False, description=f"connect container to {network}")

    def list_volumes(self, prefix="deployer-data-"):
        text = self.runner.run(["docker", "volume", "ls", "--format", "{{.Name}}"],
                               description="list volumes")
        names = [line.strip() for line in text.splitlines() if line.strip()]
        if prefix:
            names = [name for name in names if name.startswith(prefix)]
        return names

    def remove_volume(self, volume, log=None):
        if volume in self.list_volumes(prefix=""):
            self.runner.run(["docker", "volume", "rm", "-f", volume], log,
                            description=f"remove volume {volume}")

    def cleanup_images(self, name, log):
        text = self.runner.run(["docker", "image", "ls", "--filter", f"reference=deployer-{name}:*",
                                "--format", "{{.Repository}}:{{.Tag}}"], description="list app images")
        for image in set(text.splitlines()):
            if not image.startswith(f"deployer-{name}:"):
                continue
            used = self.runner.run(["docker", "ps", "-aq", "--filter", f"ancestor={image}"],
                                   description="check image usage")
            if not used.strip():
                self.runner.run(["docker", "image", "rm", image], log, description="remove unused app image")

    def cleanup_managed_images(self, log):
        self.runner.run(["docker", "image", "prune", "--all", "--force", "--filter", "label=deployer.managed=1"],
                        log, description="remove unused managed build images")

    def readiness_address(self, container, network, *, timeout=None):
        text = self.runner.run(["docker", "inspect", "--type", "container", "-f",
                                "{{json .NetworkSettings.Networks}}", container], timeout=timeout,
                               description="inspect readiness address")
        endpoint = json.loads(text).get(network, {})
        address = endpoint.get("IPAddress") or endpoint.get("GlobalIPv6Address")
        if not address:
            raise RuntimeError(f"Container is not connected to readiness network {network}.")
        return address

    def start_candidate(self, container, image, config, log):
        values = parse_env(config.get("env", ""))
        primary_network = config.get("network") or self.settings.network
        self.ensure_network(primary_network, log)
        command = ["docker", "run", "-d", "--name", container, "--network", primary_network,
                   "--log-driver", "json-file", "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
                   "--network-alias", config["name"],
                   "--label", "deployer.app=" + config["name"]]
        if config.get("stateful") and config.get("mount_path"):
            volume_name = f"deployer-data-{config['name']}"
            command.extend(["-v", f"{volume_name}:{config['mount_path']}"])
        for key, value in values.items():
            command.extend(["-e", f"{key}={value}"])
        self.runner.run(command + [image], log, secrets=list(values.values()), description="start candidate")
        if config.get("is_public", True) and primary_network != self.settings.network:
            self.connect_network(self.settings.network, container, log=log)

    def remove(self, container, log):
        if self.inspect(container).get("Status") != "missing":
            self.runner.run(["docker", "rm", "-f", "-v", container], log, description="remove container")

    def logs(self, container, log, secrets=()):
        self.runner.run(["docker", "logs", "--tail", "100", container], log,
                        timeout=5, secrets=secrets, check=False, description="candidate startup logs")

    def restart_policy(self, container, log):
        self.runner.run(["docker", "update", "--restart", "unless-stopped", container], log,
                        description="configure container restart policy")

    def action(self, operation, container, log):
        self.runner.run(["docker", operation, container], log, description=f"{operation} container")

    def stats(self, container):
        text = self.runner.run(["docker", "stats", "--no-stream", "--format", "{{json .}}", container],
                               timeout=3, check=False, description="container statistics")
        try:
            data = json.loads(text)
            return {"cpu": data["CPUPerc"], "mem": data["MemUsage"], "mem_perc": data["MemPerc"]}
        except (ValueError, KeyError):
            return {}
