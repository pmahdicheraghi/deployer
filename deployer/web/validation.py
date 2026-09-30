"""Request validation shared by new and edited app configurations."""
import re
import secrets
from urllib.parse import urlsplit

from deployer.config import DOMAIN_RE
from . import services

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
SUBDOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def validate(form, existing=None):
    settings = services()["settings"]
    name = existing["name"] if existing else form.get("name", "").strip().lower()
    subdomain = form.get("subdomain", "").strip().lower()
    custom = form.get("custom_domain", "").strip().lower()
    if not NAME.fullmatch(name):
        raise ValueError("Name must contain lowercase letters, digits or dashes (up to 31 characters).")
    if not SUBDOMAIN.fullmatch(subdomain) or subdomain == "www":
        raise ValueError("Invalid subdomain.")
    if custom and not DOMAIN_RE.fullmatch(custom):
        raise ValueError("Invalid custom domain.")
    default_domain = f"{subdomain}.{settings.base_domain}"
    domain = custom or default_domain
    if settings.panel_domain in {domain, default_domain}:
        raise ValueError("The panel domain is reserved.")
    try:
        port = int(form.get("port", ""))
    except ValueError:
        raise ValueError("Invalid container port.") from None
    if not 0 < port < 65536:
        raise ValueError("Invalid container port.")
    branch = form.get("branch", "main").strip() or "main"
    if (branch.startswith(("-", "/", ".")) or branch.endswith(("/", ".", ".lock")) or
        any(part.startswith(".") for part in branch.split("/")) or
        any(value in branch for value in ("..", "@{", "//")) or
        re.search(r"[\s~^:?*\[\]\\\x00-\x1f\x7f]", branch)):
        raise ValueError("Invalid Git branch.")
    health_path = form.get("health_path", "").strip()
    if health_path and (not health_path.startswith("/") or health_path.startswith("//") or any(c in health_path for c in "\r\n")):
        raise ValueError("Readiness path must be a relative HTTP path beginning with /.")
    environment = form.get("env", "")
    for line in environment.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "=" not in line or not ENV_KEY.fullmatch(line.split("=", 1)[0].strip()):
            raise ValueError("Environment variables must use valid KEY=value lines.")
    config = dict(name=name, subdomain=subdomain, custom_domain=custom, domain=domain,
        base_domain=settings.base_domain, port=port, branch=branch, env=environment, health_path=health_path,
        secret=existing["secret"] if existing else secrets.token_hex(32), method=form.get("method", "manual"))
    if config["method"] == "github_app":
        installation = form.get("installation_id", "")
        repo = form.get("repo_full_name", "")
        if not settings.github_enabled or installation not in services()["apps"].installations() or not REPOSITORY.fullmatch(repo):
            raise ValueError("Choose a connected GitHub installation and repository.")
        config.update(installation_id=installation, repo_full_name=repo, provider="github")
    elif config["method"] == "manual":
        url = form.get("repo_url", "").strip()
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment or any(c in url for c in "\r\n"):
            raise ValueError("Repository URL must be HTTPS and contain no credentials.")
        provider = form.get("provider", "github")
        if provider not in {"github", "gitlab"}:
            raise ValueError("Unsupported repository provider.")
        token = form.get("token", "").strip() or (existing or {}).get("token", "")
        config.update(repo_url=url, provider=provider, token=token)
    else:
        raise ValueError("Unsupported repository access method.")
    return config
