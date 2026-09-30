"""Session authentication and signed per-session CSRF tokens."""
import hmac
import secrets
import time
from functools import wraps

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import services

bp = Blueprint("auth", __name__)


def login_required(function):
    @wraps(function)
    def authenticated(*args, **kwargs):
        if not session.get("auth"):
            return redirect(url_for("auth.login"))
        return function(*args, **kwargs)
    return authenticated


def serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="deployer-csrf")


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return serializer().dumps(session["csrf"])


def install_csrf(application):
    @application.before_request
    def protect():
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"} or request.blueprint == "webhooks":
            return
        origin = request.headers.get("Origin")
        if origin:
            settings = services()["settings"]
            expected = f"https://{settings.panel_domain}" if settings.panel_domain else request.host_url.rstrip("/")
            if origin != expected:
                abort(403, "Unexpected request origin.")
        try:
            received = serializer().loads(request.form.get("csrf_token", ""), max_age=3600)
        except (BadSignature, SignatureExpired):
            abort(400, "Missing or expired CSRF token; reload the page.")
        stored = session.get("csrf")
        if not stored or not isinstance(received, str) or not hmac.compare_digest(received, stored):
            abort(400, "Invalid CSRF token.")

    @application.context_processor
    def globals():
        return dict(csrf_token=csrf_token, gh_enabled=services()["settings"].github_enabled)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("password", ""), services()["settings"].admin_password):
            session.clear()
            session["auth"] = True
            return redirect(url_for("apps.index"))
        time.sleep(1)
        flash("Wrong password")
    return render_template("login.html")


@bp.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
