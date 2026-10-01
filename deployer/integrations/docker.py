"""Docker operations; secrets are passed without logging argument lists."""
import json

from .git import parse_env
from .commands import redact


class Docker:
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
        self.runner.run(["docker", "build", "-t", image, str(context)], log,
            timeout=self.settings.build_timeout, secrets=secrets, description="build image")

    def start_candidate(self, container, image, config, log):
        values = parse_env(config.get("env", ""))
        command = ["docker", "run", "-d", "--name", container, "--network", self.settings.network,
                   "--label", "deployer.app=" + config["name"]]
        for key, value in values.items():
            command.extend(["-e", f"{key}={value}"])
        self.runner.run(command + [image], log, secrets=list(values.values()), description="start candidate")

    def remove(self, container, log):
        if self.inspect(container).get("Status") != "missing":
            self.runner.run(["docker", "rm", "-f", container], log, description="remove container")

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
