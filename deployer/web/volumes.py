"""Routes for viewing and managing Docker volumes."""
from flask import Blueprint, flash, redirect, render_template, url_for

from . import services
from .auth import login_required

bp = Blueprint("volumes", __name__)


@bp.get("/volumes")
@login_required
def index():
    docker = services()["docker"]
    app_store = services()["apps"]
    volume_names = docker.list_volumes(prefix="deployer-data-")
    all_apps = {app["name"]: app for app in app_store.list()}

    volumes = []
    for vname in volume_names:
        app_name = vname[len("deployer-data-"):]
        app = all_apps.get(app_name)
        is_attached = bool(app and not app.get("delete_requested") and app.get("stateful"))
        volumes.append({
            "name": vname,
            "app_name": app_name,
            "app_exists": bool(app),
            "is_attached": is_attached,
            "mount_path": app.get("mount_path", "") if app else ""
        })

    return render_template("volumes.html", volumes=volumes)


@bp.post("/volumes/<name>/delete")
@login_required
def delete(name):
    docker = services()["docker"]
    app_store = services()["apps"]
    if not name.startswith("deployer-data-"):
        flash("Invalid volume name.")
        return redirect(url_for("volumes.index"))

    app_name = name[len("deployer-data-"):]
    app = app_store.get(app_name)
    if app and not app.get("delete_requested") and app.get("stateful"):
        flash(f"Cannot delete volume '{name}' because it is attached to active app '{app_name}'.")
        return redirect(url_for("volumes.index"))

    try:
        docker.remove_volume(name)
        flash(f"Volume '{name}' deleted.")
    except Exception as error:
        flash(str(error))
    return redirect(url_for("volumes.index"))
