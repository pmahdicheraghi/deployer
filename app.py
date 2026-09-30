"""Gunicorn entry point. Background jobs belong to the dedicated worker."""
from deployer import create_app

app = create_app()
