"""Single-server deployment dashboard and durable worker."""


def create_app(settings=None, *, dependencies=None):
    from flask import Flask
    from .config import Settings
    from .storage.database import Database
    from .storage.apps import AppStore
    from .storage.jobs import JobStore
    from .integrations.commands import CommandRunner
    from .integrations.github import GitHub
    from .integrations.git import GitRepository
    from .integrations.docker import Docker
    from .integrations.nginx import NginxRouter
    from .deployment.service import DeploymentService
    from .deployment.worker import Worker
    from .web import auth, apps, github, webhooks

    settings = settings or Settings.from_env()
    overrides = dependencies or {}
    application = Flask(__name__)
    application.config.update(SECRET_KEY=settings.secret_key, SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=bool(settings.panel_domain), SESSION_COOKIE_SAMESITE="Lax",
        MAX_CONTENT_LENGTH=1024 * 1024)
    if settings.panel_domain:
        application.config["TRUSTED_HOSTS"] = [settings.panel_domain]
    database = Database(settings.data_dir, settings.base_domain)
    app_store, job_store = AppStore(database), JobStore(database)
    runner = overrides.get("runner") or CommandRunner(settings.command_timeout)
    github_client = overrides.get("github") or GitHub(settings)
    repository = overrides.get("repository") or GitRepository(settings.data_dir, runner, github_client)
    docker = overrides.get("docker") or Docker(settings, runner)
    router = overrides.get("router") or NginxRouter(settings, runner)
    service = DeploymentService(settings, app_store, job_store, repository, docker, router)
    application.extensions["deployer"] = dict(settings=settings, apps=app_store, jobs=job_store,
        github=github_client, runner=runner, repository=repository, docker=docker, router=router,
        service=service, worker=Worker(service, app_store, job_store))
    for blueprint in (auth.bp, apps.bp, github.bp, webhooks.bp):
        application.register_blueprint(blueprint)
    auth.install_csrf(application)
    return application
