"""
bootstrap_google_calendar.py — One-time Google Calendar OAuth setup.

Uses the manual code-paste flow because Google deprecated the OOB/console
flow in October 2022 and `flow.run_console()` was removed from
google-auth-oauthlib 1.0+. The flow we use here works inside `docker exec`
(no browser in the container) by relying on the loopback redirect: Google
redirects to http://localhost/?code=... in the user's browser, the browser
shows a "connection refused" page (no server is listening there), and the
user copies the code out of the URL bar back into this script.

Prerequisites:
  1. In Google Cloud Console (project iconic-monitor-489123-s4 by spec
     default), enable the Google Calendar API.
  2. Credentials → Create OAuth 2.0 client ID → type "Desktop app" →
     name it whatever you like.
  3. Download the JSON and place it at /srv/lifeos/auth/google-oauth-client.json
     on the host (mounted as /data/auth/google-oauth-client.json inside
     the api container).
  4. Run this script.

Usage:
    docker exec -it lifeos-api python scripts/bootstrap_google_calendar.py
"""

import os
import sys
import urllib.parse

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
# Google's "Loopback IP address flow" allows any port on localhost without
# pre-registering it as a redirect URI for Desktop-app OAuth clients.
# We never actually listen here — the user just copies the code out of
# the browser's address bar after the redirect fails.
REDIRECT_URI = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "http://localhost")


def main():
    from google_auth_oauthlib.flow import Flow

    client_path = os.environ.get(
        "GOOGLE_OAUTH_CLIENT_PATH",
        "/data/auth/google-oauth-client.json",
    )
    tokens_path = os.environ.get(
        "GOOGLE_CREDENTIALS_PATH",
        "/data/auth/google-calendar-tokens.json",
    )

    if not os.path.exists(client_path):
        print(f"OAuth client file not found: {client_path}", file=sys.stderr)
        print("Download an OAuth client (Desktop app) JSON from Google Cloud", file=sys.stderr)
        print("Console and copy it to that path before running this script.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(os.path.dirname(tokens_path), exist_ok=True)

    flow = Flow.from_client_secrets_file(
        client_path,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )

    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",  # forces issuance of a refresh_token
    )

    print()
    print("=" * 72)
    print(" Step 1: Open this URL in any browser and authorize:")
    print()
    print(f"   {auth_url}")
    print()
    print(" Step 2: After authorizing, Google will redirect your browser to:")
    print(f"   {REDIRECT_URI}/?state=...&code=<long-string>&scope=...")
    print()
    print(" Your browser will show a 'site can't be reached' or 'connection")
    print(" refused' page — that's expected. The URL bar still contains the")
    print(" code we need.")
    print()
    print(" Step 3: Copy the FULL redirect URL from your browser's address")
    print(" bar (or just the 'code=...' value) and paste it below:")
    print("=" * 72)
    print()

    raw = input(" Paste redirect URL or code: ").strip()
    if not raw:
        print("Empty input.", file=sys.stderr)
        sys.exit(1)

    # Accept either the full URL or just the code value
    if raw.startswith("http"):
        parsed = urllib.parse.urlparse(raw)
        params = urllib.parse.parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        if not code:
            print("Could not find 'code=' in that URL.", file=sys.stderr)
            sys.exit(1)
    else:
        code = raw

    flow.fetch_token(code=code)
    creds = flow.credentials

    with open(tokens_path, "w") as f:
        f.write(creds.to_json())

    try:
        os.chmod(tokens_path, 0o600)
    except OSError:
        pass

    print()
    print(f"Tokens written to {tokens_path}")
    print()
    print("Next steps:")
    print("  1. In .env set GOOGLE_CALENDAR_ENABLED=true")
    print("  2. Optionally set GOOGLE_CALENDAR_ID to a dedicated 'LifeOS'")
    print("     calendar id (e.g. abc123@group.calendar.google.com).")
    print("     Default is 'primary' which uses your main calendar.")
    print("  3. docker compose up -d --force-recreate api")


if __name__ == "__main__":
    main()
