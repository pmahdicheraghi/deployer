"""Dashboard routes only read state or persist jobs and configuration."""
import shutil
import time

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for

from . import services
from .auth import login_required
from .validation import validate
from deployer.integrations.commands import redact
from deployer.integrations.git import parse_env

bp = Blueprint("apps", __name__)


def get_app(name):
    app = services()["apps"].get(name)
    if not app:
        abort(404)
    return app


def presentation(app):
    active = app["active"]
    runtime = services()["docker"].inspect(active["container"]).get("Status", "missing") if active else "missing"
    jobs = [job for job in services()["jobs"].list() if job.name == app["name"] and job.state in {"pending", "running"}]
    status = "deleting" if app["delete_requested"] else ("deploying" if any(j.kind == "deploy" and j.state == "running" for j in jobs)
        else "queued" if jobs else "failed" if app["status"] == "failed" else runtime if active else "new")
    return {**app, "live": status, "runtime": runtime, "active_domain": active.get("domain", "") if active else app.get("domain", ""),
            "sha": active.get("sha", "") if active else "", "deployed_at": active.get("deployed_at", 0) if active else 0}


@bp.get("/")
@login_required
def index():
    return render_template("index.html", apps=[presentation(app) for app in services()["apps"].list()],
                           base_domain=services()["settings"].base_domain)


def form_context(app, editing):
    settings = services()["settings"]
    networks = services()["networks"].list()
    return dict(app=app, editing=editing, base_domain=settings.base_domain,
                gh_enabled=settings.github_enabled, installations=services()["apps"].installations(),
                networks=networks, default_network=settings.network)


@bp.route("/apps/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        try:
            config = validate(request.form)
            services()["apps"].create(config, deploy=True)
        except ValueError as error:
            flash(str(error))
            return render_template("form.html", **form_context(request.form, False)), 400
        return redirect(url_for("apps.detail", name=config["name"]))
    settings = services()["settings"]
    return render_template("form.html", **form_context(dict(port=80, provider="github",
        method="github_app" if settings.github_enabled else "manual", is_public=True, network=settings.network,
        installation_id=request.args.get("installation_id", "")), False))


@bp.route("/apps/<name>/edit", methods=["GET", "POST"])
@login_required
def edit(name):
    app = get_app(name)
    if request.method == "POST":
        try:
            config = validate(request.form, app)
            services()["apps"].edit(name, config)
        except ValueError as error:
            flash(str(error))
            return render_template("form.html", **form_context({**app, **request.form}, True)), 400
        flash("Saved. Click Deploy to apply; the current app keeps its existing settings and route.")
        return redirect(url_for("apps.detail", name=name))
    app.setdefault("subdomain", app["name"])
    return render_template("form.html", **form_context(app, True))


@bp.get("/apps/<name>")
@login_required
def detail(name):
    app = presentation(get_app(name))
    settings = services()["settings"]
    base = f"https://{settings.panel_domain}" if settings.panel_domain else request.host_url.rstrip("/")
    webhook = f"{base}/webhook/github (shared)" if app["method"] == "github_app" else f"{base}/webhook/{name}"
    return render_template("detail.html", app=app, status=app["live"], webhook=webhook, base_domain=settings.base_domain)


@bp.post("/apps/<name>/<operation>")
@login_required
def action(name, operation):
    get_app(name)
    if operation not in {"deploy", "stop", "start", "restart", "delete"}:
        abort(404)
    payload = {}
    if operation == "delete":
        payload["delete_volume"] = request.form.get("delete_volume") in ("1", "true", "True", "on")
    try:
        services()["jobs"].enqueue(name, operation, payload=payload)
    except ValueError as error:
        abort(409, str(error))
    flash(f"{operation.capitalize()} queued.")
    return redirect(url_for("apps.index" if operation == "delete" else "apps.detail", **({} if operation == "delete" else {"name":name})))


@bp.get("/apps/<name>/log.json")
@login_required
def log_json(name):
    app = presentation(get_app(name))
    path = services()["settings"].data_dir / "logs" / f"{name}.log"
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            offset = max(0, stream.tell() - 30000)
            stream.seek(offset)
            captured = stream.read()
            if offset:
                captured = captured.partition(b"\n")[2]
            log = captured.decode(errors="replace")
    except FileNotFoundError:
        log = "(no deploys yet)"
    secrets = [app.get("token", ""), *parse_env(app.get("env", "")).values()]
    if app["active"]:
        secrets.extend([app["active"].get("token", ""), *parse_env(app["active"].get("env", "")).values()])
    return jsonify(status=app["live"], runtime=app["runtime"], error=app["error"], log=redact(log, secrets))


@bp.get("/apps/<name>/stats.json")
@login_required
def stats_json(name):
    app = get_app(name)
    stats = services()["docker"].stats(app["active"]["container"]) if app["active"] else {}
    total, used, _ = shutil.disk_usage(services()["settings"].data_dir)
    stats.update(disk=f"{used / 1024**3:.1f} GB / {total / 1024**3:.1f} GB", disk_perc=f"{used / total * 100:.1f}%")
    return jsonify(stats=stats)


@bp.app_template_filter("timestamp")
def timestamp(value):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))
