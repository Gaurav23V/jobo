"""Minimal manual OAuth flow to debug the 400 consent error.

Strips everything down to: response_type, client_id, redirect_uri,
scope, access_type=offline. No include_granted_scopes, no PKCE.
"""

import json
import threading
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

with open("credentials/client_secret.json") as f:
    _secret = json.load(f)["installed"]

CLIENT_ID = _secret["client_id"]
CLIENT_SECRET = _secret["client_secret"]
# Must be gmail.modify, not gmail.readonly: the collector marks processed
# job-alert emails as read (commit 5a1b4c6), which readonly cannot do.
SCOPE = "https://www.googleapis.com/auth/gmail.modify"
PORT = 8642
REDIRECT_URI = f"http://localhost:{PORT}"
TOKEN_URI = "https://oauth2.googleapis.com/token"

result = {}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        result.update(query)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        msg = "Got it! You can close this tab." if "code" in query else "No code in redirect."
        self.wfile.write(f"<h1>{msg}</h1>".encode())

    def log_message(self, *args):
        pass


params = {
    "response_type": "code",
    "client_id": CLIENT_ID,
    "redirect_uri": REDIRECT_URI,
    "scope": SCOPE,
    "access_type": "offline",
    # Force the consent screen so Google always returns a refresh_token.
    # Without this, a re-auth on an already-granted account silently omits it.
    "prompt": "consent",
}
auth_url = "https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(params)

server = HTTPServer(("localhost", PORT), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()

print("Opening browser for authorization...")
print(f"If nothing opens, paste this URL manually:\n\n{auth_url}\n")
webbrowser.open(auth_url)

print(f"Waiting for redirect on {REDIRECT_URI} ...")
while "code" not in result and "error" not in result:
    server.handle_request()

if "error" in result:
    print(f"Authorization failed: {result['error']}")
    raise SystemExit(1)

code = result["code"][0]
print("Received authorization code.")

data = urllib.parse.urlencode(
    {
        "code": code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }
).encode()

req = urllib.request.Request(TOKEN_URI, data=data)
with urllib.request.urlopen(req) as resp:
    token_data = json.loads(resp.read())

print(f"Token exchange status keys: {sorted(token_data.keys())}")

if "refresh_token" not in token_data:
    print("WARNING: no refresh_token returned!")

creds = {
    "token": token_data["access_token"],
    "refresh_token": token_data.get("refresh_token"),
    "token_uri": TOKEN_URI,
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "scopes": [SCOPE],
}
with open("credentials/token.json", "w") as f:
    json.dump(creds, f)

print("Saved credentials/token.json")
