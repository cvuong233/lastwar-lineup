import os
import time
import base64
import json
import threading
from functools import wraps

import jwt
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ---- Config (env vars) ----
EDITOR_USERNAME = os.environ["EDITOR_USERNAME"]
EDITOR_PASSWORD = os.environ["EDITOR_PASSWORD"]
JWT_SECRET = os.environ["JWT_SECRET"]
JWT_TTL_SECONDS = int(os.environ.get("JWT_TTL_SECONDS", str(30 * 24 * 3600)))  # 30 days default

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "cvuong233")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "agent-presentation")
GITHUB_PATH = os.environ.get("GITHUB_PATH", "lastwar/players.json")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "master")

GITHUB_API = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{GITHUB_PATH}"


def github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }


# ---- naive in-memory rate limiter for login (fine for a single Railway instance) ----
_login_attempts = {}
_lock = threading.Lock()
MAX_FAILS = 5
WINDOW_SECONDS = 5 * 60


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _is_rate_limited(ip):
    now = time.time()
    with _lock:
        attempts = [t for t in _login_attempts.get(ip, []) if now - t < WINDOW_SECONDS]
        _login_attempts[ip] = attempts
        return len(attempts) >= MAX_FAILS


def _record_failure(ip):
    with _lock:
        _login_attempts.setdefault(ip, []).append(time.time())


def _clear_failures(ip):
    with _lock:
        _login_attempts.pop(ip, None)


# ---- CORS ----
# Open origin on purpose: the real gate is the password + JWT below, not the
# calling origin, and there's nothing origin-sensitive (like cookies) here.
@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.route("/api/<path:_any>", methods=["OPTIONS"])
def cors_preflight(_any):
    return ("", 204)


# ---- auth ----
def make_token():
    now = int(time.time())
    payload = {"sub": "editor", "iat": now, "exp": now + JWT_TTL_SECONDS}
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "unauthorized"}), 401
        token = auth[len("Bearer "):]
        try:
            jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "token_expired"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "invalid_token"}), 401
        return fn(*args, **kwargs)

    return wrapper


# ---- routes ----
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/api/login", methods=["POST"])
def login():
    ip = _client_ip()
    if _is_rate_limited(ip):
        return jsonify({"error": "rate_limited", "message": "Too many failed attempts. Try again later."}), 429

    body = request.get_json(silent=True) or {}
    username = str(body.get("username", ""))
    password = str(body.get("password", ""))

    if username == EDITOR_USERNAME and password == EDITOR_PASSWORD:
        _clear_failures(ip)
        return jsonify({"token": make_token(), "expires_in": JWT_TTL_SECONDS})

    _record_failure(ip)
    return jsonify({"error": "invalid_credentials"}), 401


def _github_get():
    resp = requests.get(GITHUB_API, headers=github_headers(), params={"ref": GITHUB_BRANCH}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return json.loads(content), data["sha"]


def _github_put(new_data, sha, message):
    content = json.dumps(new_data, indent=2, ensure_ascii=False) + "\n"
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return requests.put(
        GITHUB_API,
        headers=github_headers(),
        json={"message": message, "content": b64, "sha": sha, "branch": GITHUB_BRANCH},
        timeout=15,
    )


def _apply_edits(data, edits):
    applied, skipped = [], []
    for e in edits:
        mode = e.get("mode")
        slot = e.get("slot")
        section = e.get("section")
        name = e.get("name")
        pos = (e.get("pos") or "").strip()
        try:
            arr = data["next_lineup"][mode][slot][section]
        except (KeyError, TypeError):
            skipped.append(name)
            continue
        entry = next((p for p in arr if p.get("name") == name), None)
        if entry is None:
            skipped.append(name)
            continue
        if pos:
            entry["pos"] = pos
        else:
            entry.pop("pos", None)
        applied.append(name)
    return applied, skipped


@app.route("/api/positions", methods=["POST"])
@require_auth
def save_positions():
    body = request.get_json(silent=True) or {}
    edits = body.get("edits")
    if not isinstance(edits, list) or not edits:
        return jsonify({"error": "no_edits"}), 400

    last_err = None
    for _ in range(3):
        try:
            data, sha = _github_get()
        except requests.RequestException as e:
            return jsonify({"error": "github_get_failed", "detail": str(e)}), 502

        applied, skipped = _apply_edits(data, edits)
        if not applied:
            return jsonify({"error": "nothing_applied", "skipped": skipped}), 400

        resp = _github_put(data, sha, f"Update player positions ({len(applied)} player(s))")
        if resp.status_code in (200, 201):
            return jsonify({"ok": True, "applied": applied, "skipped": skipped})
        if resp.status_code == 409:
            last_err = "sha conflict, retrying"
            continue
        return jsonify({"error": "github_put_failed", "status": resp.status_code, "detail": resp.text[:500]}), 502

    return jsonify({"error": "conflict_retries_exhausted", "detail": last_err}), 409


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
