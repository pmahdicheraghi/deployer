"""GitHub installation connection and authenticated discovery endpoints."""
import hmac
import secrets
import time

import requests
from flask import Blueprint, abort, flash, jsonify, redirect, request, session, url_for

from . import services
from .auth import login_required
from .validation import REPOSITORY

bp = Blueprint("github", __name__)


@bp.get("/connect/github")
@login_required
def connect():
    settings = services()["settings"]
    if not settings.github_enabled:
        abort(404)
    state = secrets.token_urlsafe(32)
    session.update(gh_state=state, gh_state_time=time.time())
    return redirect(f"https://github.com/apps/{settings.github_slug}/installations/new?state={state}")


@bp.get("/github/callback")
@login_required
def callback():
    expected = session.pop("gh_state", None)
    issued = session.pop("gh_state_time", 0)
    received = request.args.get("state", "")
    if not received or not expected or time.time() - issued > 600 or not hmac.compare_digest(received, expected):
        flash("GitHub connection expired, please try again.")
        return redirect(url_for("apps.new"))
    installation = request.args.get("installation_id", "")
    if not services()["settings"].github_enabled or not installation.isdecimal():
        abort(400)
    try:
        info = services()["github"].installation(installation)
    except requests.RequestException:
        abort(502, "GitHub installation lookup failed.")
    account = info.get("account", {}).get("login", "unknown")
    services()["apps"].connect(installation, {"account":account})
    flash(f"Connected GitHub account: {account}")
    return redirect(url_for("apps.new", installation_id=installation))


def connected_installation():
    installation = request.args.get("installation_id", "")
    if installation not in services()["apps"].installations():
        abort(404)
    return installation


@bp.get("/api/github/repos")
@login_required
def repos():
    installation = connected_installation()
    try:
        return jsonify(repos=services()["github"].repositories(installation))
    except requests.RequestException:
        return jsonify(error="GitHub repository lookup failed."), 502


@bp.get("/api/github/branches")
@login_required
def branches():
    installation = connected_installation()
    repo = request.args.get("repo", "")
    if not REPOSITORY.fullmatch(repo):
        abort(400)
    try:
        return jsonify(branches=services()["github"].branches(installation, repo))
    except requests.RequestException:
        return jsonify(error="GitHub branch lookup failed."), 502
