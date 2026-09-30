"""HTTP requests submit intent; the deployment worker executes it."""
from flask import current_app


def services():
    return current_app.extensions["deployer"]
