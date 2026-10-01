"""Routes for managing user-defined Docker networks."""
from flask import Blueprint, flash, redirect, render_template, request, url_for

from . import services
from .auth import login_required

bp = Blueprint("networks", __name__)


@bp.get("/networks")
@login_required
def index():
    network_store = services()["networks"]
    apps_store = services()["apps"]
    networks = network_store.list()
    all_apps = apps_store.list()
    network_usage = {}
    for app in all_apps:
        net = app.get("network") or services()["settings"].network
        network_usage.setdefault(net, []).append(app["name"])

    for item in networks:
        item["apps"] = network_usage.get(item["name"], [])
        item["is_default"] = (item["name"] == network_store.default_network)

    return render_template("networks.html", networks=networks, default_network=network_store.default_network)


@bp.post("/networks/new")
@login_required
def create():
    name = request.form.get("name", "").strip().lower()
    try:
        services()["networks"].create(name)
        services()["docker"].ensure_network(name)
        flash(f"Network '{name}' created.")
    except (ValueError, RuntimeError) as error:
        flash(str(error))
    return redirect(url_for("networks.index"))


@bp.post("/networks/<name>/delete")
@login_required
def delete(name):
    try:
        services()["networks"].delete(name, cleanup=services()["docker"].remove_network)
        flash(f"Network '{name}' deleted.")
    except (ValueError, RuntimeError) as error:
        flash(str(error))
    return redirect(url_for("networks.index"))
