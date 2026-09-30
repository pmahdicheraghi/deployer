"""GitHub App authentication and repository discovery."""
import time
from datetime import datetime
from threading import Lock

import jwt
import requests


class GitHub:
    def __init__(self, settings):
        self.settings = settings
        self._tokens = {}
        self._lock = Lock()

    def _jwt(self):
        now = int(time.time())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.settings.github_id},
                          self.settings.github_key.read_text(), algorithm="RS256")

    def _request(self, path, token, *, method="GET", params=None):
        response = requests.request(method, "https://api.github.com" + path, params=params,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, timeout=15)
        response.raise_for_status()
        return response.json()

    def token(self, installation):
        with self._lock:
            cached = self._tokens.get(installation)
            if cached and cached[1] > time.time() + 60:
                return cached[0]
            data = self._request(f"/app/installations/{installation}/access_tokens", self._jwt(), method="POST")
            expiry = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
            self._tokens[installation] = (data["token"], expiry)
            return data["token"]

    def installation(self, installation):
        return self._request(f"/app/installations/{installation}", self._jwt())

    def repositories(self, installation):
        repositories = []
        for page in range(1, 101):
            batch = self._request("/installation/repositories", self.token(installation),
                                  params={"per_page": 100, "page": page})["repositories"]
            repositories.extend({key: repo[key] for key in ("full_name", "private", "default_branch")} for repo in batch)
            if len(batch) < 100:
                break
        return repositories

    def branches(self, installation, repo):
        branches = []
        for page in range(1, 4):
            batch = self._request(f"/repos/{repo}/branches", self.token(installation), params={"per_page": 100, "page": page})
            branches.extend(branch["name"] for branch in batch)
            if len(batch) < 100:
                break
        return branches
