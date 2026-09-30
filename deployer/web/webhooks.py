"""Verified push notifications are accepted only after jobs are persisted."""
import hashlib
import hmac

from flask import Blueprint, abort, request

from . import services

bp = Blueprint("webhooks", __name__)


def signed(secret):
    expected = "sha256=" + hmac.new(secret.encode(), request.get_data(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(request.headers.get("X-Hub-Signature-256", ""), expected)


def enqueue(name):
    try:
        return services()["jobs"].enqueue(name, "deploy")
    except ValueError as error:
        abort(409, str(error))


@bp.post("/webhook/github")
def github():
    settings = services()["settings"]
    if not settings.github_enabled:
        abort(404)
    if not signed(settings.github_secret):
        abort(401)
    if request.headers.get("X-GitHub-Event") != "push":
        return {"status":"ignored"}, 200
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        abort(400)
    repository = payload.get("repository") or {}
    if not isinstance(repository, dict):
        abort(400)
    triggered = []
    for app in services()["apps"].list():
        if app["method"] == "github_app" and app.get("repo_full_name") == repository.get("full_name") and payload.get("ref") == f"refs/heads/{app['branch']}":
            if not app["delete_requested"]:
                enqueue(app["name"]); triggered.append(app["name"])
    return {"status":"queued", "apps":triggered}, 202


@bp.post("/webhook/<name>")
def manual(name):
    app = services()["apps"].get(name)
    if not app or app["method"] != "manual":
        abort(404)
    gitlab = request.headers.get("X-Gitlab-Token", "")
    if not (signed(app["secret"]) if request.headers.get("X-Hub-Signature-256") else gitlab and hmac.compare_digest(gitlab, app["secret"])):
        abort(401)
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        abort(400)
    if payload.get("ref") != f"refs/heads/{app['branch']}":
        return {"status":"ignored"}, 200
    job_id = enqueue(name)
    return {"status":"queued", "job_id":job_id}, 202
