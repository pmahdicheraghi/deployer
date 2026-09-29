"""Deployer: push-to-deploy panel (GitHub App or manual GitHub/GitLab -> docker build -> nginx + Let's Encrypt)."""
import hashlib, hmac, json, os, re, secrets, shutil, subprocess, sys, threading, time
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import quote

import jwt
import requests
from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)

DATA = os.environ.get("DATA_DIR", "/data")
CONF_DIR, LE_DIR = "/etc/nginx/conf.d", "/etc/letsencrypt"
NETWORK = os.environ.get("DOCKER_NETWORK", "web")
APP_PREFIX = "deployer"
PANEL_DOMAIN = os.environ.get("PANEL_DOMAIN", "")
APP_BASE_DOMAIN = os.environ["APP_BASE_DOMAIN"].strip().lower()
ACME_EMAIL = os.environ["ACME_EMAIL"]
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
LE_VOL = os.environ.get("LE_VOLUME", "deployer_letsencrypt")
WWW_VOL = os.environ.get("WWW_VOLUME", "deployer_certbot-www")

# --- GitHub App (optional; UI hides the "Connect GitHub" flow if unset) ---
GH_APP_SLUG = os.environ.get("GITHUB_APP_SLUG", "")
GH_APP_ID = os.environ.get("GITHUB_APP_ID", "")
GH_APP_WEBHOOK_SECRET = os.environ.get("GITHUB_APP_WEBHOOK_SECRET", "")
GH_APP_KEY_PATH = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH", "/secrets/github-app-private-key.pem")
if not os.path.isfile(GH_APP_KEY_PATH):
    for _p in ("/secrets/github-app-private-key.pem", "/data/github-app-private-key.pem"):
        if os.path.isfile(_p):
            GH_APP_KEY_PATH = _p
            break
GH_APP_ENABLED = bool(GH_APP_SLUG and GH_APP_ID and GH_APP_WEBHOOK_SECRET and os.path.isfile(GH_APP_KEY_PATH))
_gh_private_key = open(GH_APP_KEY_PATH).read() if GH_APP_ENABLED else None
if GH_APP_SLUG and GH_APP_ID and GH_APP_WEBHOOK_SECRET and not GH_APP_ENABLED:
    print(f"WARNING: GitHub App configured but {GH_APP_KEY_PATH} is missing or not a file — "
          "GitHub App features disabled. If it's a directory, remove it on the host and place "
          "the .pem file there, then restart.", flush=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or hashlib.sha256(("k" + ADMIN_PASSWORD).encode()).hexdigest()
app.config.update(SESSION_COOKIE_SECURE=bool(PANEL_DOMAIN), SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax")

os.makedirs(f"{DATA}/repos", exist_ok=True)
os.makedirs(f"{DATA}/logs", exist_ok=True)
STATE = f"{DATA}/state.json"
slock, dlocks = threading.RLock(), {}
SUB_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


# ---------- state ----------
def load():
    with slock:
        try:
            return json.load(open(STATE))
        except FileNotFoundError:
            return {"apps": {}, "installations": {}}

def save(s):
    with slock:
        json.dump(s, open(STATE + ".tmp", "w"), indent=2)
        os.replace(STATE + ".tmp", STATE)

def get_apps(): return load().get("apps", {})

def update_app(name, **kw):
    with slock:
        s = load()
        if name in s.get("apps", {}):
            s["apps"][name].update(kw)
            save(s)

def logpath(name): return f"{DATA}/logs/{name}.log"

def read_log(name):
    try:
        with open(logpath(name), "rb") as f:
            f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 30000))
            return f.read().decode(errors="replace")
    except FileNotFoundError:
        return "(no deploys yet)"


# ---------- GitHub App auth ----------
_token_cache = {}  # installation_id -> (token, expiry_epoch)

def gh_app_jwt():
    now = int(time.time())
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": GH_APP_ID}, _gh_private_key, algorithm="RS256")

def gh_installation_token(installation_id):
    cached = _token_cache.get(installation_id)
    if cached and cached[1] - 60 > time.time():
        return cached[0]
    r = requests.post(f"https://api.github.com/app/installations/{installation_id}/access_tokens",
                      headers={"Authorization": f"Bearer {gh_app_jwt()}", "Accept": "application/vnd.github+json"},
                      timeout=15)
    r.raise_for_status()
    d = r.json()
    exp = datetime.fromisoformat(d["expires_at"].replace("Z", "+00:00")).timestamp()
    _token_cache[installation_id] = (d["token"], exp)
    return d["token"]

def gh_installation_info(installation_id):
    r = requests.get(f"https://api.github.com/app/installations/{installation_id}",
                     headers={"Authorization": f"Bearer {gh_app_jwt()}", "Accept": "application/vnd.github+json"},
                     timeout=15)
    r.raise_for_status()
    return r.json()

def gh_list_repos(installation_id):
    token = gh_installation_token(installation_id)
    repos, page = [], 1
    while True:
        r = requests.get("https://api.github.com/installation/repositories",
                         params={"per_page": 100, "page": page},
                         headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
                         timeout=15)
        r.raise_for_status()
        batch = r.json().get("repositories", [])
        repos += [{"full_name": x["full_name"], "private": x["private"],
                   "default_branch": x["default_branch"]} for x in batch]
        if len(batch) < 100:
            break
        page += 1
    return repos

def gh_list_branches(installation_id, repo_full_name):
    token = gh_installation_token(installation_id)
    branches, page = [], 1
    while True:
        r = requests.get(f"https://api.github.com/repos/{repo_full_name}/branches",
                         params={"per_page": 100, "page": page},
                         headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
                         timeout=15)
        r.raise_for_status()
        batch = r.json()
        branches += [b["name"] for b in batch]
        if len(batch) < 100 or len(branches) >= 300:
            break
        page += 1
    return branches


# ---------- shell / nginx / certbot ----------
def sh(cmd, lf, hide=(), check=True):
    shown = " ".join(cmd)
    for h in hide: shown = shown.replace(h, "***")
    lf.write(f"$ {shown}\n"); lf.flush()
    r = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT)
    lf.flush()
    if check and r.returncode:
        raise RuntimeError(f"command failed ({r.returncode}): {shown}")

PROXY = """
    resolver 127.0.0.11 valid=10s;
    location / {
        set $up __UP__;
        proxy_pass $up;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
    }
"""
ACME = "    location /.well-known/acme-challenge/ { root /var/www/certbot; }\n"
HTTP_ONLY = "server {\n    listen 80;\n    server_name __D__;\n" + ACME + PROXY + "}\n"
FULL = ("server {\n    listen 80;\n    server_name __D__;\n" + ACME +
        "    location / { return 301 https://$host$request_uri; }\n}\n"
        "server {\n    listen 443 ssl;\n    http2 on;\n    server_name __D__;\n"
        "    ssl_certificate /etc/letsencrypt/live/__D__/fullchain.pem;\n"
        "    ssl_certificate_key /etc/letsencrypt/live/__D__/privkey.pem;\n"
        "    ssl_protocols TLSv1.2 TLSv1.3;\n    client_max_body_size 100m;\n" + PROXY + "}\n")

def render(tpl, domain, upstream):
    return tpl.replace("__D__", domain).replace("__UP__", upstream)

def reload_nginx(lf):
    sh(["docker", "exec", "nginx", "nginx", "-s", "reload"], lf)

def provision(domain, upstream, lf):
    open(f"{CONF_DIR}/00-upgrade-map.conf", "w").write(
        "map $http_upgrade $connection_upgrade { default upgrade; '' close; }\n")
    conf = f"{CONF_DIR}/{domain}.conf"
    if not os.path.exists(f"{LE_DIR}/live/{domain}/fullchain.pem"):
        open(conf, "w").write(render(HTTP_ONLY, domain, upstream))
        reload_nginx(lf)
        sh(["docker", "run", "--rm", "-v", f"{LE_VOL}:/etc/letsencrypt", "-v", f"{WWW_VOL}:/var/www/certbot",
            "certbot/certbot", "certonly", "--webroot", "-w", "/var/www/certbot", "-d", domain,
            "--email", ACME_EMAIL, "--agree-tos", "--no-eff-email", "-n"], lf)
    open(conf, "w").write(render(FULL, domain, upstream))
    reload_nginx(lf)

def remove_vhost(domain):
    try: os.remove(f"{CONF_DIR}/{domain}.conf")
    except FileNotFoundError: pass
    subprocess.run(["docker", "exec", "nginx", "nginx", "-s", "reload"], capture_output=True)


# ---------- deploy ----------
def parse_env(text):
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out

def clone_url_for(a):
    if a["method"] == "github_app":
        token = gh_installation_token(a["installation_id"])
        return f"https://x-access-token:{token}@github.com/{a['repo_full_name']}.git", [token]
    u = a["repo_url"]
    if a.get("token"):
        user = "oauth2" if a["provider"] == "gitlab" else "x-access-token"
        u = u.replace("https://", f"https://{user}:{quote(a['token'], safe='')}@", 1)
        return u, [a["token"]]
    return u, []

def deploy(name):
    lock = dlocks.setdefault(name, threading.Lock())
    if not lock.acquire(blocking=False):
        return
    try:
        a = get_apps().get(name)
        if not a: return
        update_app(name, status="deploying")
        with open(logpath(name), "w") as lf:
            try:
                path, container = f"{DATA}/repos/{name}", f"{APP_PREFIX}-{name}"
                url, hide = clone_url_for(a)
                if os.path.isdir(path + "/.git"):
                    sh(["git", "-C", path, "remote", "set-url", "origin", url], lf, hide)
                else:
                    sh(["git", "init", path], lf)
                    sh(["git", "-C", path, "remote", "add", "origin", url], lf, hide)
                sh(["git", "-C", path, "fetch", "--depth", "1", "origin", a["branch"]], lf, hide)
                sh(["git", "-C", path, "reset", "--hard", "FETCH_HEAD"], lf)
                sha = subprocess.check_output(["git", "-C", path, "rev-parse", "--short", "HEAD"]).decode().strip()

                image = f"{container}:{sha}"
                sh(["docker", "build", "-t", image, path], lf)
                sh(["docker", "rm", "-f", container], lf, check=False)
                cmd = ["docker", "run", "-d", "--name", container, "--restart", "unless-stopped",
                       "--network", NETWORK]
                for k, v in parse_env(a.get("env")).items():
                    cmd += ["-e", f"{k}={v}"]
                sh(cmd + [image], lf)
                sh(["docker", "image", "prune", "-f"], lf, check=False)

                status = "running"
                try:
                    provision(a["domain"], f"http://{container}:{a['port']}", lf)
                except Exception as e:
                    lf.write(f"\nWARNING: HTTPS setup failed ({e}). Check DNS points to this server.\n")
                    status = "no-tls"
                lf.write(f"\nDone: {sha} -> {a['domain']}\n")
                update_app(name, status=status, sha=sha, deployed_at=int(time.time()))
            except Exception as e:
                lf.write(f"\nFAILED: {e}\n")
                update_app(name, status="failed")
    finally:
        lock.release()

def start_deploy(name):
    threading.Thread(target=deploy, args=(name,), daemon=True).start()

def live_status(a):
    if a["status"] in ("deploying", "failed", "new"):
        return a["status"]
    r = subprocess.run(["docker", "inspect", "-f", "{{.State.Status}}", f"{APP_PREFIX}-{a['name']}"],
                       capture_output=True, text=True)
    st = r.stdout.strip() if r.returncode == 0 else "missing"
    return a["status"] if (st == "running" and a["status"] == "no-tls") else st


# ---------- auth ----------
def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("auth"):
            return redirect(url_for("login"))
        return f(*a, **k)
    return w

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD):
            session["auth"] = True
            return redirect(url_for("index"))
        time.sleep(1)
        flash("Wrong password")
    return render_template("login.html")

@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- GitHub App connect flow ----------
@app.get("/connect/github")
@login_required
def connect_github():
    if not GH_APP_ENABLED:
        abort(404)
    state = secrets.token_urlsafe(24)
    session["gh_state"] = state
    return redirect(f"https://github.com/apps/{GH_APP_SLUG}/installations/new?state={state}")

@app.get("/github/callback")
@login_required
def github_callback():
    if request.args.get("state") != session.pop("gh_state", None):
        flash("GitHub connection expired, please try again.")
        return redirect(url_for("new"))
    installation_id = request.args.get("installation_id")
    if not installation_id:
        return redirect(url_for("new"))
    info = gh_installation_info(installation_id)
    account = info.get("account", {}).get("login", "unknown")
    with slock:
        s = load()
        s.setdefault("installations", {})[installation_id] = {"account": account}
        save(s)
    flash(f"Connected GitHub account: {account}")
    return redirect(url_for("new", installation_id=installation_id))

@app.get("/api/github/repos")
@login_required
def api_github_repos():
    installation_id = request.args.get("installation_id")
    if not installation_id or installation_id not in load().get("installations", {}):
        abort(404)
    try:
        return jsonify(repos=gh_list_repos(installation_id))
    except requests.HTTPError as e:
        return jsonify(error=str(e)), 502

@app.get("/api/github/branches")
@login_required
def api_github_branches():
    installation_id = request.args.get("installation_id")
    repo = request.args.get("repo")
    if not installation_id or not repo or installation_id not in load().get("installations", {}):
        abort(404)
    try:
        return jsonify(branches=gh_list_branches(installation_id, repo))
    except requests.HTTPError as e:
        return jsonify(error=str(e)), 502


# ---------- UI ----------
def get_app_or_404(name):
    a = get_apps().get(name)
    if not a: abort(404)
    return a

def subdomain_taken(sub, existing=None):
    domain = f"{sub}.{APP_BASE_DOMAIN}"
    if PANEL_DOMAIN and domain == PANEL_DOMAIN:
        return True
    return any(o["domain"] == domain and o["name"] != existing for o in get_apps().values())

def validate(f, existing=None):
    errors = []
    name = (existing or f.get("name", "")).strip().lower()
    if not existing and (not NAME_RE.match(name) or name in get_apps()):
        errors.append("Name must be lowercase letters/digits/dashes and unique.")
    sub = f.get("subdomain", "").strip().lower()
    if not SUB_RE.match(sub) or sub in ("www",):
        errors.append("Subdomain must be lowercase letters, digits and dashes.")
    elif subdomain_taken(sub, existing):
        errors.append("That subdomain is already in use.")
    if not (f.get("port", "").isdigit() and 0 < int(f["port"]) < 65536):
        errors.append("Invalid port.")
    method = f.get("method", "manual")
    if method == "github_app":
        if not (f.get("installation_id") and f.get("repo_full_name")):
            errors.append("Choose a connected GitHub account and repository.")
    else:
        if not f.get("repo_url", "").startswith("https://"):
            errors.append("Repository URL must start with https://")
    return errors

@app.route("/")
@login_required
def index():
    apps = sorted(get_apps().values(), key=lambda a: a["name"])
    for a in apps: a["live"] = live_status(a)
    return render_template("index.html", apps=apps, base_domain=APP_BASE_DOMAIN)

@app.route("/apps/new", methods=["GET", "POST"])
@login_required
def new():
    installations = load().get("installations", {})
    if request.method == "POST":
        f = request.form
        errors = validate(f)
        if errors:
            for e in errors: flash(e)
            return render_template("form.html", app=f, editing=False, base_domain=APP_BASE_DOMAIN,
                                  gh_enabled=GH_APP_ENABLED, installations=installations)
        name = f["name"].strip().lower()
        method = f.get("method", "manual")
        a = dict(name=name, method=method, branch=f.get("branch", "main").strip() or "main",
                 domain=f"{f['subdomain'].strip().lower()}.{APP_BASE_DOMAIN}",
                 port=int(f["port"]), env=f.get("env", ""), secret=secrets.token_hex(16),
                 status="new", sha="", deployed_at=0)
        if method == "github_app":
            a.update(installation_id=f["installation_id"], repo_full_name=f["repo_full_name"], provider="github")
        else:
            a.update(provider=f.get("provider", "github"), repo_url=f["repo_url"].strip(),
                     token=f.get("token", "").strip())
        with slock:
            s = load(); s.setdefault("apps", {})[name] = a; save(s)
        start_deploy(name)
        return redirect(url_for("detail", name=name))
    prefill = {"branch": "main", "port": 80, "method": "github_app" if GH_APP_ENABLED else "manual",
              "provider": "github", "installation_id": request.args.get("installation_id", "")}
    return render_template("form.html", app=prefill, editing=False, base_domain=APP_BASE_DOMAIN,
                          gh_enabled=GH_APP_ENABLED, installations=installations)

@app.route("/apps/<name>/edit", methods=["GET", "POST"])
@login_required
def edit(name):
    a = get_app_or_404(name)
    installations = load().get("installations", {})
    if request.method == "POST":
        f = request.form
        errors = validate(f, existing=name)
        if errors:
            for e in errors: flash(e)
            return render_template("form.html", app={**a, **f}, editing=True, base_domain=APP_BASE_DOMAIN,
                                  gh_enabled=GH_APP_ENABLED, installations=installations)
        new_domain = f"{f['subdomain'].strip().lower()}.{APP_BASE_DOMAIN}"
        if new_domain != a["domain"]:
            remove_vhost(a["domain"])
        upd = dict(branch=f.get("branch", "main").strip() or "main", domain=new_domain,
                   port=int(f["port"]), env=f.get("env", ""), method=f.get("method", "manual"))
        if upd["method"] == "github_app":
            upd.update(installation_id=f["installation_id"], repo_full_name=f["repo_full_name"], provider="github")
        else:
            upd.update(provider=f.get("provider", "github"), repo_url=f["repo_url"].strip())
            if f.get("token", "").strip():
                upd["token"] = f["token"].strip()
        update_app(name, **upd)
        flash("Saved. Click Deploy to apply.")
        return redirect(url_for("detail", name=name))
    a = dict(a); a["subdomain"] = a["domain"][: -(len(APP_BASE_DOMAIN) + 1)]
    return render_template("form.html", app=a, editing=True, base_domain=APP_BASE_DOMAIN,
                          gh_enabled=GH_APP_ENABLED, installations=installations)

@app.route("/apps/<name>")
@login_required
def detail(name):
    a = get_app_or_404(name)
    base = f"https://{PANEL_DOMAIN}" if PANEL_DOMAIN else request.host_url.rstrip("/")
    webhook = f"{base}/webhook/github (shared)" if a["method"] == "github_app" else f"{base}/webhook/{name}"
    return render_template("detail.html", app=a, status=live_status(a), webhook=webhook)

@app.post("/apps/<name>/deploy")
@login_required
def redeploy(name):
    get_app_or_404(name)
    start_deploy(name)
    return redirect(url_for("detail", name=name))

@app.post("/apps/<name>/restart")
@login_required
def restart(name):
    get_app_or_404(name)
    r = subprocess.run(["docker", "restart", f"{APP_PREFIX}-{name}"], capture_output=True, text=True)
    if r.returncode == 0:
        flash("Container restarted.")
    else:
        flash(f"Restart failed: {r.stderr.strip() or 'container missing'}")
    return redirect(url_for("detail", name=name))

@app.post("/apps/<name>/stop")
@login_required
def stop(name):
    get_app_or_404(name)
    r = subprocess.run(["docker", "stop", f"{APP_PREFIX}-{name}"], capture_output=True, text=True)
    if r.returncode == 0:
        flash("Container stopped.")
    else:
        flash(f"Stop failed: {r.stderr.strip() or 'container missing'}")
    return redirect(url_for("detail", name=name))

@app.post("/apps/<name>/start")
@login_required
def start(name):
    get_app_or_404(name)
    r = subprocess.run(["docker", "start", f"{APP_PREFIX}-{name}"], capture_output=True, text=True)
    if r.returncode == 0:
        flash("Container started.")
    else:
        flash(f"Start failed: {r.stderr.strip() or 'container missing'}")
    return redirect(url_for("detail", name=name))

@app.post("/apps/<name>/delete")
@login_required
def delete(name):
    a = get_app_or_404(name)
    subprocess.run(["docker", "rm", "-f", f"{APP_PREFIX}-{name}"], capture_output=True)
    remove_vhost(a["domain"])
    shutil.rmtree(f"{DATA}/repos/{name}", ignore_errors=True)
    with slock:
        s = load(); s.get("apps", {}).pop(name, None); save(s)
    return redirect(url_for("index"))

@app.get("/apps/<name>/log.json")
@login_required
def log_json(name):
    a = get_app_or_404(name)
    return jsonify(status=live_status(a), log=read_log(name))


# ---------- webhooks ----------
@app.post("/webhook/github")
def webhook_github_app():
    """Single shared endpoint for every repo the GitHub App is installed on."""
    if not GH_APP_ENABLED: abort(404)
    sig = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(GH_APP_WEBHOOK_SECRET.encode(), request.get_data(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        abort(401)
    if request.headers.get("X-GitHub-Event") != "push":
        return {"status": "ignored"}, 200
    payload = request.get_json(silent=True) or {}
    full_name = payload.get("repository", {}).get("full_name", "")
    ref = payload.get("ref", "")
    triggered = []
    for a in get_apps().values():
        if a["method"] == "github_app" and a.get("repo_full_name") == full_name \
           and ref == f"refs/heads/{a['branch']}":
            start_deploy(a["name"])
            triggered.append(a["name"])
    return {"status": "deploying", "apps": triggered}, 202

@app.post("/webhook/<name>")
def webhook_manual(name):
    """Per-app endpoint for manually configured GitHub/GitLab repos."""
    a = get_apps().get(name) or abort(404)
    if a["method"] == "github_app": abort(404)
    sig, token = request.headers.get("X-Hub-Signature-256"), request.headers.get("X-Gitlab-Token")
    if sig:
        exp = "sha256=" + hmac.new(a["secret"].encode(), request.get_data(), hashlib.sha256).hexdigest()
        ok = hmac.compare_digest(sig, exp)
    elif token:
        ok = hmac.compare_digest(token, a["secret"])
    else:
        ok = False
    if not ok: abort(401)
    ref = (request.get_json(silent=True) or {}).get("ref", "")
    if ref != f"refs/heads/{a['branch']}":
        return {"status": "ignored", "ref": ref}, 200
    start_deploy(name)
    return {"status": "deploying"}, 202


# ---------- misc ----------
@app.template_filter("timestamp")
def _ts(v): return time.strftime("%Y-%m-%d %H:%M", time.localtime(v))

@app.context_processor
def inject_globals():
    return dict(gh_enabled=GH_APP_ENABLED)


# ---------- bootstrap: put the panel itself behind nginx + TLS ----------
def bootstrap():
    if not PANEL_DOMAIN: return
    for _ in range(10):
        try:
            provision(PANEL_DOMAIN, "http://deployer:8080", sys.stdout)
            print("panel ready at https://" + PANEL_DOMAIN, flush=True)
            return
        except Exception as e:
            print("bootstrap retry:", e, flush=True)
            time.sleep(5)

threading.Thread(target=bootstrap, daemon=True).start()
