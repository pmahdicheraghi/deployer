import json
from concurrent.futures import ThreadPoolExecutor

import pytest


def store(tmp_path):
    from deployer.storage.database import Database
    from deployer.storage.apps import AppStore
    from deployer.storage.jobs import JobStore
    db = Database(tmp_path)
    return AppStore(db), JobStore(db)


def config(name="demo", domain="demo.apps.example.com"):
    return dict(name=name, domain=domain, subdomain=name, port=80, method="manual",
        provider="github", repo_url="https://example.com/repo.git", branch="main",
        env="PASSWORD=synthetic-secret", secret="hook", status="new")


def test_migration_is_repeatable_and_preserves_original(tmp_path):
    original = json.dumps({"apps": {"demo": {**config(), "status": "deploying"}},
                           "installations": {"123": {"account": "owner"}}})
    (tmp_path / "state.json").write_text(original)
    apps, jobs = store(tmp_path)
    assert apps.get("demo")["active"]["container"] == "deployer-demo"
    assert jobs.list()[0].kind == "deploy"
    assert apps.installations()["123"]["account"] == "owner"
    apps, jobs = store(tmp_path)
    assert len(jobs.list()) == 1
    assert (tmp_path / "state.json").read_text() == original


def test_edit_reserves_old_and_pending_domains(tmp_path):
    apps, _ = store(tmp_path)
    apps.create(config())
    apps.activate("demo", {**config(), "container": "old", "sha": "old"})
    apps.edit("demo", {**config(), "domain": "changed.apps.example.com"})
    assert apps.get("demo")["active"]["domain"] == "demo.apps.example.com"
    for domain in ("demo.apps.example.com", "changed.apps.example.com"):
        with pytest.raises(ValueError): apps.create(config("other", domain))


def test_pushes_coalesce_and_survive_database_reopen(tmp_path):
    apps, jobs = store(tmp_path); apps.create(config())
    first = jobs.enqueue("demo", "deploy")
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: jobs.enqueue("demo", "deploy"), range(20)))
    assert set(ids) == {first}
    running = jobs.claim()
    followup = jobs.enqueue("demo", "deploy")
    assert followup != running.id
    _, reopened = store(tmp_path)
    assert [j.state for j in reopened.list()] == ["running", "pending"]


def test_delete_request_blocks_later_deployments(tmp_path):
    apps, jobs = store(tmp_path); apps.create(config())
    jobs.enqueue("demo", "deploy")
    jobs.enqueue("demo", "delete")
    assert apps.get("demo")["delete_requested"]
    assert jobs.claim().kind == "delete"
    with pytest.raises(ValueError): jobs.enqueue("demo", "deploy")


def test_new_app_and_initial_job_are_atomic(tmp_path):
    apps, jobs = store(tmp_path)
    apps.create(config(), deploy=True)
    assert jobs.claim().name == "demo"


def test_database_rollback_leaves_no_partial_changes(tmp_path):
    apps, _ = store(tmp_path)
    with pytest.raises(RuntimeError):
        with apps.db.transaction() as conn:
            conn.execute("INSERT INTO installations VALUES (?, ?)", ("123", '{}'))
            raise RuntimeError("interrupted")
    assert apps.installations() == {}


def test_legacy_custom_domain_reserves_default_subdomain_using_configured_base(tmp_path):
    from deployer.storage.database import Database
    from deployer.storage.apps import AppStore
    legacy = {**config(), "domain":"custom.example.org", "custom_domain":"custom.example.org"}
    legacy.pop("subdomain")
    (tmp_path / "state.json").write_text(json.dumps({"apps":{"demo":legacy}}))
    apps = AppStore(Database(tmp_path, base_domain="apps.company.com"))
    with pytest.raises(ValueError):
        apps.create({**config("other", "demo.apps.company.com"), "base_domain":"apps.company.com"})


def test_migration_extracts_legacy_url_credentials_into_private_state(tmp_path):
    legacy = {**config(), "repo_url":"https://x-access-token:synthetic%2Ftoken@example.com/repo.git"}
    (tmp_path / "state.json").write_text(json.dumps({"apps":{"demo":legacy}}))
    apps, _ = store(tmp_path)
    assert apps.get("demo")["repo_url"] == "https://example.com/repo.git"
    assert apps.get("demo")["token"] == "synthetic/token"
