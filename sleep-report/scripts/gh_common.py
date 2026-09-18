"""Shared helpers for the Google Health API: config paths, OAuth token refresh, HTTP GET.

Stdlib only. Credentials live outside the skill folder, in ~/.config/google-health/.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "google-health")
CLIENT_SECRET_PATH = os.path.join(CONFIG_DIR, "client_secret.json")
TOKEN_PATH = os.path.join(CONFIG_DIR, "token.json")

API_BASE = "https://health.googleapis.com/v4/users/me/dataTypes"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
]


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def load_client():
    if not os.path.exists(CLIENT_SECRET_PATH):
        die(f"client_secret.json not found at {CLIENT_SECRET_PATH}. See SKILL.md -> 'Первичная настройка'.", 2)
    with open(CLIENT_SECRET_PATH, encoding="utf-8") as f:
        data = json.load(f)
    # Downloaded file is {"installed": {...}} for Desktop clients or {"web": {...}} for Web clients
    return data.get("installed") or data.get("web") or data


def _post_form(url, fields):
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        die(f"token endpoint HTTP {e.code}: {e.read().decode(errors='replace')}", 3)


def save_token(tok):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tok["obtained_at"] = int(time.time())
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        json.dump(tok, f, indent=2)


def get_access_token():
    if not os.path.exists(TOKEN_PATH):
        die(f"No token at {TOKEN_PATH}. Run auth.py first.", 4)
    with open(TOKEN_PATH, encoding="utf-8") as f:
        tok = json.load(f)
    if tok.get("access_token") and time.time() < tok.get("obtained_at", 0) + tok.get("expires_in", 0) - 120:
        return tok["access_token"]
    if not tok.get("refresh_token"):
        die("Token expired and no refresh_token. Run auth.py again.", 4)
    client = load_client()
    new = _post_form(TOKEN_URL, {
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "refresh_token": tok["refresh_token"],
        "grant_type": "refresh_token",
    })
    if "access_token" not in new:
        die(f"Refresh failed ({new}). Run auth.py again.", 4)
    new.setdefault("refresh_token", tok["refresh_token"])
    save_token(new)
    return new["access_token"]


def api_get(path, params, token):
    url = f"{API_BASE}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if e.code == 401:
            die(f"401 Unauthorized for {path}. Token revoked/expired - run auth.py again. {body}", 4)
        return {"_error": {"status": e.code, "body": body[:2000]}}
    except urllib.error.URLError as e:
        return {"_error": {"status": None, "body": str(e)}}


def list_all(data_type, filter_expr, token, page_size=25, max_pages=40):
    """Paginate users.dataTypes.dataPoints.list. Returns (points, error)."""
    points, page_token = [], None
    for _ in range(max_pages):
        params = {"pageSize": page_size, "filter": filter_expr}
        if page_token:
            params["pageToken"] = page_token
        resp = api_get(f"{data_type}/dataPoints", params, token)
        if "_error" in resp:
            return points, resp["_error"]
        points.extend(resp.get("dataPoints", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return points, None
