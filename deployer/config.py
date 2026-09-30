"""Validated configuration; importing modules has no runtime side effects."""
import os
import re
from dataclasses import dataclass
from pathlib import Path

DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    base_domain: str
    acme_email: str
    admin_password: str
    secret_key: str
    panel_domain: str = ""
    conf_dir: Path = Path("/etc/nginx/conf.d")
    le_dir: Path = Path("/etc/letsencrypt")
    network: str = "web"
    le_volume: str = "deployer_letsencrypt"
    www_volume: str = "deployer_certbot-www"
    github_slug: str = ""
    github_id: str = ""
    github_secret: str = ""
    github_key: Path = Path("/secrets/github-app-private-key.pem")
    command_timeout: float = 60
    build_timeout: float = 900
    cert_timeout: float = 180
    readiness_timeout: float = 30
    certbot_image: str = "certbot/certbot:v5.1.0"

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        def required(key):
            value = env.get(key, "").strip()
            if not value:
                raise ValueError(f"{key} must be explicitly configured.")
            return value
        domain = required("APP_BASE_DOMAIN").lower()
        panel = env.get("PANEL_DOMAIN", "").strip().lower()
        if not DOMAIN_RE.fullmatch(domain) or (panel and not DOMAIN_RE.fullmatch(panel)):
            raise ValueError("Invalid APP_BASE_DOMAIN or PANEL_DOMAIN.")
        timeouts = {}
        for field, key, default in (("command_timeout", "COMMAND_TIMEOUT", 60),
            ("build_timeout", "BUILD_TIMEOUT", 900), ("cert_timeout", "CERT_TIMEOUT", 180),
            ("readiness_timeout", "READINESS_TIMEOUT", 30)):
            value = float(env.get(key, default))
            if not 0 < value <= 86400:
                raise ValueError(f"{key} must be between 0 and 86400 seconds.")
            timeouts[field] = value
        return cls(data_dir=Path(env.get("DATA_DIR", "/data")), base_domain=domain,
            acme_email=required("ACME_EMAIL"), admin_password=required("ADMIN_PASSWORD"),
            secret_key=required("SECRET_KEY"), panel_domain=panel,
            conf_dir=Path(env.get("NGINX_CONF_DIR", "/etc/nginx/conf.d")),
            le_dir=Path(env.get("LE_DIR", "/etc/letsencrypt")), network=env.get("DOCKER_NETWORK", "web"),
            le_volume=env.get("LE_VOLUME", "deployer_letsencrypt"),
            www_volume=env.get("WWW_VOLUME", "deployer_certbot-www"),
            github_slug=env.get("GITHUB_APP_SLUG", ""), github_id=env.get("GITHUB_APP_ID", ""),
            github_secret=env.get("GITHUB_APP_WEBHOOK_SECRET", ""),
            github_key=Path(env.get("GITHUB_APP_PRIVATE_KEY_PATH", "/secrets/github-app-private-key.pem")),
            certbot_image=env.get("CERTBOT_IMAGE", cls.certbot_image), **timeouts)

    @property
    def github_enabled(self):
        return bool(self.github_slug and self.github_id and self.github_secret and self.github_key.is_file())
