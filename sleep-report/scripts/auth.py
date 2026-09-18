"""One-time OAuth consent for the Google Health API (loopback redirect + PKCE).

Opens the browser, waits for Google to redirect back to http://127.0.0.1:<port>/,
exchanges the code and stores the token in ~/.config/google-health/token.json.

Usage: python auth.py [--no-browser]
"""
import base64
import hashlib
import http.server
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser

from gh_common import AUTH_URL, SCOPES, TOKEN_PATH, TOKEN_URL, _post_form, die, load_client, save_token

TIMEOUT_SEC = 300


def main():
    client = load_client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" not in qs and "error" not in qs:
                self.send_response(404)
                self.end_headers()
                return
            result.update({k: v[0] for k, v in qs.items()})
            ok = "code" in qs and qs.get("state", [""])[0] == state
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Done. You can close this tab and return to Claude." if ok else f"Error: {result}"
            self.wfile.write(f"<html><body style='font:18px sans-serif;padding:40px'>{msg}</body></html>".encode())

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}/"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    print("Open this URL to grant access (opening browser automatically):\n" + url, flush=True)
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)

    t = threading.Thread(target=lambda: [server.handle_request() for _ in range(20) if not result])
    t.daemon = True
    t.start()
    t.join(TIMEOUT_SEC)
    server.server_close()

    if not result:
        die(f"No redirect received within {TIMEOUT_SEC}s.")
    if "error" in result:
        die(f"Consent denied/failed: {result}")
    if result.get("state") != state:
        die("State mismatch - aborting.")

    tok = _post_form(TOKEN_URL, {
        "client_id": client["client_id"],
        "client_secret": client.get("client_secret", ""),
        "code": result["code"],
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })
    if "access_token" not in tok:
        die(f"Token exchange failed: {tok}")
    save_token(tok)
    granted = tok.get("scope", "")
    missing = [s for s in SCOPES if s not in granted]
    print(f"OK. Token saved to {TOKEN_PATH}. refresh_token: {'yes' if tok.get('refresh_token') else 'NO'}")
    if missing:
        print("WARNING: scopes not granted (tick all checkboxes on the consent screen): " + ", ".join(missing))


if __name__ == "__main__":
    main()
