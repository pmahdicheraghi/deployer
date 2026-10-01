"""Credential-free Git remotes and isolated, secret-free build contexts."""
import os
import shutil
import tempfile
import stat
from pathlib import Path
from urllib.parse import urlsplit


def parse_env(text):
    values = {}
    for line in (text or "").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.strip().split("=", 1)
            values[key.strip()] = value.strip()
    return values


class GitRepository:
    def __init__(self, data_dir, runner, github):
        self.directory, self.runner, self.github = Path(data_dir), runner, github

    @staticmethod
    def source_url(config):
        url = f"https://github.com/{config['repo_full_name']}.git" if config["method"] == "github_app" else config["repo_url"]
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ValueError("Repository URL must be HTTPS and contain no credentials.")
        return url

    def sanitize_remote(self, config):
        repo = self.directory / "repos" / config["name"]
        if (repo / ".git").is_dir():
            self.runner.run(["git", "-c", "credential.helper=", "-c", "core.hooksPath=" + os.devnull,
                "-C", str(repo), "remote", "set-url", "origin", self.source_url(config)],
                description="scrub legacy repository credentials")

    def prepare(self, config, job_id, log):
        name = config["name"]
        repo = self.directory / "repos" / name
        context = self.directory / "builds" / str(job_id)
        repo.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        context.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        token = self.github.token(config["installation_id"]) if config["method"] == "github_app" else config.get("token", "")
        url = self.source_url(config)
        # Overrides prevent legacy credential helpers and hooks from persisting tokens.
        prefix = ["git", "-c", "credential.helper=", "-c", "core.hooksPath=" + os.devnull, "-C", str(repo)]
        if not (repo / ".git").is_dir():
            self.runner.run(["git", "init", str(repo)], log, description="initialize checkout")
            self.runner.run(prefix + ["remote", "add", "origin", url], log, description="configure repository")
        else:
            # Scrub legacy credential-bearing origins before any fetch/build.
            self.runner.run(prefix + ["remote", "set-url", "origin", url], log, description="configure repository")
        with tempfile.TemporaryDirectory(prefix="deployer-auth-") as credentials:
            helper = Path(credentials) / "askpass.py"
            helper.write_text("#!/usr/bin/env python3\nimport os,sys\nprint(os.environ['GIT_AUTH_USER'] if 'Username' in sys.argv[1] else os.environ['GIT_AUTH_TOKEN'])\n")
            helper.chmod(0o700)
            env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": str(helper),
                   "GIT_AUTH_TOKEN": token, "GIT_AUTH_USER": "oauth2" if config.get("provider") == "gitlab" else "x-access-token"}
            self.runner.run(prefix + ["fetch", "--depth", "1", "origin", config["branch"]], log,
                            env=env, secrets=[token], description="fetch repository")
        self.runner.run(prefix + ["reset", "--hard", "FETCH_HEAD"], log, description="checkout revision")
        self.runner.run(prefix + ["clean", "-fdx"], log, description="clean checkout")
        sha = self.runner.run(prefix + ["rev-parse", "HEAD"], description="resolve revision").strip()
        self.runner.run(prefix + ["reflog", "expire", "--expire=now", "--all"], description="expire checkout history")
        self.runner.run(prefix + ["gc", "--prune=now"], description="clean checkout objects")
        self.cleanup(job_id)
        def exclude(directory, names):
            return [entry for entry in names if entry == ".git" or entry == ".env" or
                (entry.startswith(".env.") and entry not in {".env.example", ".env.sample", ".env.template"}) or
                entry == "github-app-private-key.pem"]
        # Do not follow checkout symlinks: an archive must not import host secrets.
        shutil.copytree(repo, context, ignore=exclude, symlinks=True)
        for path in context.rglob("*"):
            if path.is_symlink():
                resolved = path.resolve()
                if not resolved.is_relative_to(context.resolve()):
                    self.cleanup(job_id)
                    raise ValueError("Build context contains a symlink outside the checkout.")
        return sha, context

    def cleanup(self, job_id):
        self._remove_directory("builds", str(int(job_id)))

    def remove(self, name):
        self._remove_directory("repos", name)

    def _remove_directory(self, category, name):
        parent = (self.directory / category).resolve()
        candidate = parent / name
        target = candidate.resolve()
        if not parent.is_relative_to(self.directory.resolve()) or target != candidate.absolute() or target == parent or not target.is_relative_to(parent):
            raise ValueError("Cleanup target is outside the managed directory.")
        def readonly(function, path, error):
            if not isinstance(error[1], PermissionError):
                raise error[1]
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            function(path)
        try:
            shutil.rmtree(target, onerror=readonly)
        except FileNotFoundError:
            pass

    def cleanup_orphans(self, live_jobs, live_apps):
        for category, retained in (("builds", {str(job) for job in live_jobs}), ("repos", set(live_apps))):
            parent = self.directory / category
            if parent.exists():
                for path in parent.iterdir():
                    if path.is_dir() and path.name not in retained:
                        self._remove_directory(category, path.name)
