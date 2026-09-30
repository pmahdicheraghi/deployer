"""Validated routing updates with durable file snapshots for rollback."""
import os
import re
import time

import requests

from deployer.config import DOMAIN_RE

MAP = "map $http_upgrade $connection_upgrade { default upgrade; '' close; }\n"
ACME = "location /.well-known/acme-challenge/ { root /var/www/certbot; }\n"


class NginxRouter:
    def __init__(self, settings, runner):
        self.settings, self.runner = settings, runner
        settings.conf_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, domain):
        if not DOMAIN_RE.fullmatch(domain):
            raise ValueError("Invalid routing domain.")
        return self.settings.conf_dir / f"{domain}.conf"

    @staticmethod
    def _write(path, text):
        temporary = path.with_suffix(".tmp")
        with open(temporary, "w") as stream:
            stream.write(text); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)

    def snapshot(self, domains):
        return {domain: self._path(domain).read_text() if self._path(domain).exists() else None for domain in set(domains) if domain}

    def restore(self, snapshot, log):
        for domain, text in snapshot.items():
            path = self._path(domain)
            if text is None:
                path.unlink(missing_ok=True)
            else:
                self._write(path, text)
        self.reload(log)

    def reload(self, log):
        self.runner.run(["docker", "exec", "nginx", "nginx", "-t"], log, description="validate nginx configuration")
        self.runner.run(["docker", "exec", "nginx", "nginx", "-s", "reload"], log, description="reload nginx")

    @staticmethod
    def _proxy(container, port):
        if not re.fullmatch(r"[a-z0-9-]+", container) or not 0 < int(port) < 65536:
            raise ValueError("Invalid upstream.")
        return ("resolver 127.0.0.11 valid=10s;\nlocation / {\n"
                f"set $up http://{container}:{int(port)};\nproxy_pass $up;\n"
                "proxy_http_version 1.1;\nproxy_set_header Host $host;\n"
                "proxy_set_header X-Real-IP $remote_addr;\nproxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
                "proxy_set_header X-Forwarded-Proto $scheme;\nproxy_set_header Upgrade $http_upgrade;\n"
                "proxy_set_header Connection $connection_upgrade;\n}\n")

    def provision(self, domain, container, port, log):
        path = self._path(domain)
        self._write(self.settings.conf_dir / "00-upgrade-map.conf", MAP)
        proxy = self._proxy(container, port)
        marker = f"add_header X-Deployer-Deployment {container} always;\n"
        if not (self.settings.le_dir / "live" / domain / "fullchain.pem").exists():
            self._write(path, f"server {{ listen 80; server_name {domain};\n" + marker + ACME + proxy + "}\n")
            self.reload(log)
            self.cancel_certificate(container)
            try:
                self.runner.run(["docker", "run", "--rm", "--name", f"deployer-cert-{container}", "--network", self.settings.network,
                    "-v", f"{self.settings.le_volume}:/etc/letsencrypt", "-v", f"{self.settings.www_volume}:/var/www/certbot",
                    self.settings.certbot_image, "certonly", "--webroot", "-w", "/var/www/certbot", "-d", domain,
                    "--email", self.settings.acme_email, "--agree-tos", "--no-eff-email", "-n"], log,
                    timeout=self.settings.cert_timeout, description="obtain HTTPS certificate")
            finally:
                self.cancel_certificate(container)
        self._write(path, f"server {{ listen 80; server_name {domain};\n" + marker + ACME +
            "location / { return 301 https://$host$request_uri; }\n}\n" +
            f"server {{ listen 443 ssl; http2 on; server_name {domain};\n" + marker +
            f"ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;\n"
            f"ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;\n"
            "ssl_protocols TLSv1.2 TLSv1.3;\nclient_max_body_size 100m;\n" + proxy + "}\n")
        self.reload(log)
        self.verify(domain, container)

    def verify(self, domain, container):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                response = requests.get("http://nginx/", headers={"Host": domain}, timeout=2, allow_redirects=False)
                if response.headers.get("X-Deployer-Deployment") == container:
                    return
            except requests.RequestException:
                pass
            time.sleep(.2)
        raise RuntimeError("Nginx did not confirm the replacement route.")

    def cancel_certificate(self, container):
        self.runner.run(["docker", "rm", "-f", f"deployer-cert-{container}"], check=False,
                        description="cleanup certificate helper")

    def remove(self, domain, log):
        self._path(domain).unlink(missing_ok=True)
        self.reload(log)
