"""Deployer: push-to-deploy panel (GitHub/GitLab -> docker build -> nginx + Let's Encrypt)."""
import hashlib, hmac, json, os, re, secrets, shutil, subprocess, sys, threading, time
from functools import wraps
from urllib.parse import quote

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)

DATA = os.environ.get("DATA_DIR", "/data")
CONF_DIR, LE_DIR = "/etc/nginx/conf.d", "/etc/letsencrypt"
NETWORK = os.environ.get("DOCKER_NETWORK", "web")
PANEL_DOMAIN = os.environ.get("PANEL_DOMAIN", "")
BASE_DOMAIN = os.environ.get("BASE_DOMAIN", "").strip().lower()
if not BASE_DOMAIN and PANEL_DOMAIN and "." in PANEL_DOMAIN:
    parts = PANEL_DOMAIN.split(".")
    if len(parts) > 2:
        BASE_DOMAIN = ".".join(parts[1:])
ACME_EMAIL = os.environ["ACME_EMAIL"]
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
LE_VOL = os.environ.get("LE_VOLUME", "deployer_letsencrypt")
WWW_VOL = os.environ.get("WWW_VOLUME", "deployer_certbot-www")

GITHUB_APP_ID = os.environ.get("GITHUB_APP_ID", "").strip()
GITHUB_APP_SLUG = os.environ.get("GITHUB_APP_SLUG", "").strip()
GITHUB_PRIVATE_KEY_PATH = os.environ.get("GITHUB_PRIVATE_KEY_PATH", f"{DATA}/github-app.pem")
GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "").strip()

_token_cache = {}

def get_github_private_key():
    if os.environ.get("GITHUB_PRIVATE_KEY"):
        return os.environ["GITHUB_PRIVATE_KEY"].strip()
    if GITHUB_PRIVATE_KEY_PATH and os.path.exists(GITHUB_PRIVATE_KEY_PATH):
        try:
            with open(GITHUB_PRIVATE_KEY_PATH, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return None
    return None

def get_github_jwt():
    key = get_github_private_key()
    if not key or not GITHUB_APP_ID:
        return None
    try:
        import jwt
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + 600,
            "iss": int(GITHUB_APP_ID) if GITHUB_APP_ID.isdigit() else GITHUB_APP_ID,
        }
        return jwt.encode(payload, key, algorithm="RS256")
    except Exception as e:
        print(f"Failed to generate GitHub JWT: {e}", flush=True)
        return None

def get_installation_token(installation_id):
    if not installation_id:
        return None
    now = int(time.time())
    cached = _token_cache.get(str(installation_id))
    if cached and cached[1] > now + 60:
        return cached[0]
    jwt_tok = get_github_jwt()
    if not jwt_tok:
        return None
    import urllib.request
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers={
            "Authorization": f"Bearer {jwt_tok}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "Deployer",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            tok = data.get("token")
            if tok:
                _token_cache[str(installation_id)] = (tok, now + 3500)
                return tok
    except Exception as e:
        print(f"Failed to get installation token for {installation_id}: {e}", flush=True)
    return None

def list_installation_repos(installation_id):
    tok = get_installation_token(installation_id)
    if not tok:
        return []
    import urllib.request
    url = "https://api.github.com/installation/repositories?per_page=100"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {tok}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "Deployer",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            repos = []
            for r in data.get("repositories", []):
                repos.append({
                    "name": r.get("name"),
                    "full_name": r.get("full_name"),
                    "clone_url": r.get("clone_url"),
                    "default_branch": r.get("default_branch", "main"),
                    "private": r.get("private", False),
                    "installation_id": str(installation_id),
                })
            return repos
    except Exception as e:
        print(f"Failed to list installation repos for {installation_id}: {e}", flush=True)
        return []

def get_all_github_repos():
    s = load()
    insts = s.get("_github_installations", {})
    all_repos = []
    for inst_id in insts.keys():
        all_repos.extend(list_installation_repos(inst_id))
    return all_repos

def normalize_domain(d):
    d = (d or "").strip().lower()
    if d and "." not in d and BASE_DOMAIN:
        return f"{d}.{BASE_DOMAIN}"
    return d

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or hashlib.sha256(("k" + ADMIN_PASSWORD).encode()).hexdigest()
app.config.update(SESSION_COOKIE_SECURE=bool(PANEL_DOMAIN), SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax")

os.makedirs(f"{DATA}/repos", exist_ok=True)
os.makedirs(f"{DATA}/logs", exist_ok=True)
STATE = f"{DATA}/state.json"
slock, dlocks = threading.RLock(), {}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$")


# ---------- state ----------
def load():
    with slock:
        try:
            return json.load(open(STATE))
        except FileNotFoundError:
            return {}

def save(s):
    with slock:
        json.dump(s, open(STATE + ".tmp", "w"), indent=2)
        os.replace(STATE + ".tmp", STATE)

def update(name, **kw):
    with slock:
        s = load()
        if name in s:
            s[name].update(kw)
            save(s)

def logpath(name): return f"{DATA}/logs/{name}.log"

def read_log(name):
    try:
        with open(logpath(name), "rb") as f:
            f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 30000))
            return f.read().decode(errors="replace")
    except FileNotFoundError:
        return "(no deploys yet)"


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
    """Write nginx vhost; get a Let's Encrypt cert if missing; switch to HTTPS."""
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

def auth_url(a):
    u = a["repo_url"]
    inst_id = a.get("installation_id")
    if inst_id:
        tok = get_installation_token(inst_id)
        if tok:
            return u.replace("https://", f"https://x-access-token:{tok}@", 1)
    if a.get("token"):
        user = "oauth2" if a["provider"] == "gitlab" else "x-access-token"
        u = u.replace("https://", f"https://{user}:{quote(a['token'], safe='')}@", 1)
    return u

def deploy(name):
    lock = dlocks.setdefault(name, threading.Lock())
    if not lock.acquire(blocking=False):
        return
    try:
        a = load().get(name)
        if not a: return
        update(name, status="deploying")
        with open(logpath(name), "w") as lf:
            try:
                path, container = f"{DATA}/repos/{name}", f"deployer-{name}"
                hide = [a["token"]] if a.get("token") else []
                url = auth_url(a)
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
                sh(["docker", "rm", "-f", f"minipaas-{name}"], lf, check=False)
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
                update(name, status=status, sha=sha, deployed_at=int(time.time()))
            except Exception as e:
                lf.write(f"\nFAILED: {e}\n")
                update(name, status="failed")
    finally:
        lock.release()

def start_deploy(name):
    threading.Thread(target=deploy, args=(name,), daemon=True).start()

def live_status(a):
    if a["status"] in ("deploying", "failed", "new"):
        return a["status"]
    cname = f"deployer-{a['name']}"
    r = subprocess.run(["docker", "inspect", "-f", "{{.State.Status}}", cname],
                       capture_output=True, text=True)
    if r.returncode != 0:
        cname = f"minipaas-{a['name']}"
        r = subprocess.run(["docker", "inspect", "-f", "{{.State.Status}}", cname],
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


# ---------- UI ----------
@app.template_filter("timestamp")
def _ts(v): return time.strftime("%Y-%m-%d %H:%M", time.localtime(v))

def get_app(name):
    a = load().get(name)
    if not a: abort(404)
    return a

def validate(f, existing=None):
    name = (existing or f.get("name", "")).strip().lower()
    apps = load()
    if not existing and (not NAME_RE.match(name) or name in apps):
        return "Name must be lowercase letters/digits/dashes and unique."
    domain = normalize_domain(f.get("domain", ""))
    if not DOMAIN_RE.match(domain):
        return "Invalid domain."
    if domain == PANEL_DOMAIN or any(o["domain"] == domain and o["name"] != name for o in apps.values()):
        return "Domain already in use."
    if not f.get("repo_url", "").startswith("https://"):
        return "Repository URL must start with https://"
    if not (str(f.get("port", "")).isdigit() and 0 < int(f["port"]) < 65536):
        return "Invalid port."
    return None

@app.route("/")
@login_required
def index():
    apps = sorted([a for a in load().values() if isinstance(a, dict) and "name" in a and not a["name"].startswith("_")], key=lambda a: a["name"])
    for a in apps: a["live"] = live_status(a)
    return render_template("index.html", apps=apps)

@app.route("/apps/new", methods=["GET", "POST"])
@login_required
def new():
    github_repos = get_all_github_repos() if (GITHUB_APP_ID or GITHUB_APP_SLUG) else []
    if request.method == "POST":
        f = dict(request.form)
        f["domain"] = normalize_domain(f.get("domain", ""))
        err = validate(f)
        if err:
            flash(err)
            return render_template("form.html", app=f, editing=False, base_domain=BASE_DOMAIN,
                                   github_app_slug=GITHUB_APP_SLUG, has_github_app=bool(GITHUB_APP_ID or GITHUB_APP_SLUG),
                                   github_repos=github_repos)
        name = f["name"].strip().lower()
        a = dict(name=name, provider=f["provider"], repo_url=f["repo_url"].strip(),
                 token=f.get("token", "").strip(),
                 installation_id=f.get("installation_id", "").strip(),
                 branch=f.get("branch", "main").strip() or "main", domain=f["domain"],
                 port=int(f["port"]), env=f.get("env", ""), secret=secrets.token_hex(16),
                 status="new", sha="", deployed_at=0)
        with slock:
            s = load(); s[name] = a; save(s)
        start_deploy(name)
        return redirect(url_for("detail", name=name))
    return render_template("form.html", app={"branch": "main", "port": 80, "provider": "github"}, editing=False, base_domain=BASE_DOMAIN,
                           github_app_slug=GITHUB_APP_SLUG, has_github_app=bool(GITHUB_APP_ID or GITHUB_APP_SLUG),
                           github_repos=github_repos)

@app.route("/apps/<name>/edit", methods=["GET", "POST"])
@login_required
def edit(name):
    a = get_app(name)
    github_repos = get_all_github_repos() if (GITHUB_APP_ID or GITHUB_APP_SLUG) else []
    if request.method == "POST":
        f = dict(request.form)
        f["domain"] = normalize_domain(f.get("domain", ""))
        err = validate(f, existing=name)
        if err:
            flash(err)
            return render_template("form.html", app={**a, **f}, editing=True, base_domain=BASE_DOMAIN,
                                   github_app_slug=GITHUB_APP_SLUG, has_github_app=bool(GITHUB_APP_ID or GITHUB_APP_SLUG),
                                   github_repos=github_repos)
        new_domain = f["domain"]
        if new_domain != a["domain"]:
            remove_vhost(a["domain"])
        upd = dict(provider=f["provider"], repo_url=f["repo_url"].strip(), branch=f.get("branch", "main").strip() or "main",
                   domain=new_domain, port=int(f["port"]), env=f.get("env", ""))
        if f.get("token", "").strip():
            upd["token"] = f["token"].strip()
        if "installation_id" in f:
            upd["installation_id"] = f["installation_id"].strip()
        update(name, **upd)
        flash("Saved. Click Deploy to apply.")
        return redirect(url_for("detail", name=name))
    return render_template("form.html", app=a, editing=True, base_domain=BASE_DOMAIN,
                           github_app_slug=GITHUB_APP_SLUG, has_github_app=bool(GITHUB_APP_ID or GITHUB_APP_SLUG),
                           github_repos=github_repos)

@app.route("/github/setup")
@login_required
def github_setup():
    inst_id = request.args.get("installation_id")
    if inst_id:
        with slock:
            s = load()
            insts = s.setdefault("_github_installations", {})
            insts[str(inst_id)] = {"installed_at": int(time.time())}
            save(s)
        flash("GitHub App connected successfully!")
    return redirect(url_for("new"))

@app.route("/github/repos")
@login_required
def github_repos():
    return jsonify(repos=get_all_github_repos())

@app.route("/apps/<name>")
@login_required
def detail(name):
    a = get_app(name)
    base = f"https://{PANEL_DOMAIN}" if PANEL_DOMAIN else request.host_url.rstrip("/")
    return render_template("detail.html", app=a, status=live_status(a), webhook=f"{base}/webhook/{name}")

@app.post("/apps/<name>/deploy")
@login_required
def redeploy(name):
    get_app(name)
    start_deploy(name)
    return redirect(url_for("detail", name=name))

@app.post("/apps/<name>/delete")
@login_required
def delete(name):
    a = get_app(name)
    subprocess.run(["docker", "rm", "-f", f"deployer-{name}"], capture_output=True)
    subprocess.run(["docker", "rm", "-f", f"minipaas-{name}"], capture_output=True)
    remove_vhost(a["domain"])
    shutil.rmtree(f"{DATA}/repos/{name}", ignore_errors=True)
    with slock:
        s = load(); s.pop(name, None); save(s)
    return redirect(url_for("index"))

@app.get("/apps/<name>/log.json")
@login_required
def log_json(name):
    a = get_app(name)
    return jsonify(status=live_status(a), log=read_log(name))


# ---------- webhook (GitHub + GitLab) ----------
@app.post("/webhook/<name>")
def webhook(name):
    a = load().get(name) or abort(404)
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

@app.post("/github/webhook")
def github_webhook():
    sig = request.headers.get("X-Hub-Signature-256")
    event = request.headers.get("X-GitHub-Event", "")
    secret = GITHUB_WEBHOOK_SECRET
    if secret:
        if not sig:
            abort(401)
        exp = "sha256=" + hmac.new(secret.encode(), request.get_data(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, exp):
            abort(401)

    if event == "ping":
        return {"status": "pong"}, 200

    if event == "installation":
        data = request.get_json(silent=True) or {}
        action = data.get("action")
        inst_id = str((data.get("installation") or {}).get("id") or "")
        if inst_id:
            with slock:
                s = load()
                insts = s.setdefault("_github_installations", {})
                if action == "deleted":
                    insts.pop(inst_id, None)
                else:
                    insts[inst_id] = {"installed_at": int(time.time())}
                save(s)
        return {"status": "ok"}, 200

    if event == "push":
        data = request.get_json(silent=True) or {}
        ref = data.get("ref", "")
        repo = data.get("repository") or {}
        clone_url = repo.get("clone_url", "").lower().rstrip(".git")
        full_name = repo.get("full_name", "").lower()
        matched = []
        with slock:
            apps = load()
        for aname, a in apps.items():
            if aname.startswith("_"):
                continue
            if ref != f"refs/heads/{a.get('branch', 'main')}":
                continue
            app_repo = a.get("repo_url", "").lower().rstrip(".git")
            if app_repo == clone_url or (full_name and full_name in app_repo):
                matched.append(aname)
                start_deploy(aname)
        return {"status": "deploying" if matched else "ignored", "matched": matched}, 200

    return {"status": "ignored", "event": event}, 200


# ---------- bootstrap: put the panel itself behind nginx + TLS ----------
def bootstrap():
    if not PANEL_DOMAIN: return
    import socket
    wait = 10
    for attempt in range(1, 20):
        try:
            try:
                socket.gethostbyname(PANEL_DOMAIN)
            except socket.gaierror:
                print(f"bootstrap: waiting for DNS to resolve {PANEL_DOMAIN}...", flush=True)
                time.sleep(15)
                continue
            provision(PANEL_DOMAIN, "http://deployer:8080", sys.stdout)
            print("panel ready at https://" + PANEL_DOMAIN, flush=True)
            return
        except Exception as e:
            print(f"bootstrap retry ({attempt}):", e, flush=True)
            time.sleep(wait)
            wait = min(wait * 2, 60)

threading.Thread(target=bootstrap, daemon=True).start()
