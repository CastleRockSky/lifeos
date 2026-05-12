"""
bootstrap_google_calendar.py — One-time Google Calendar OAuth setup.

Prerequisites:
  1. In Google Cloud Console, enable the Google Calendar API for your project.
  2. Create an OAuth 2.0 client (type: "Desktop app"), download the JSON, and
     copy it to /data/auth/google-oauth-client.json inside the api container.
  3. Run this script. It will print a URL — open it, authorise, paste the
     resulting code back into the terminal.

The script writes refresh tokens to /data/auth/google-calendar-tokens.json.
After that, set GOOGLE_CALENDAR_ENABLED=true in .env and restart the api.

Usage:
    docker exec -it lifeos-api python scripts/bootstrap_google_calendar.py
"""

import json
import os
import sys

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def main():
    from google_auth_oauthlib.flow import InstalledAppFlow

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

    flow = InstalledAppFlow.from_client_secrets_file(client_path, SCOPES)
    # Console flow: prints URL, asks for the code on stdin.
    creds = flow.run_console()

    with open(tokens_path, "w") as f:
        f.write(creds.to_json())

    # Lock down permissions
    try:
        os.chmod(tokens_path, 0o600)
    except OSError:
        pass

    print()
    print(f"Tokens written to {tokens_path}")
    print()
    print("Next steps:")
    print("  1. In .env set GOOGLE_CALENDAR_ENABLED=true")
    print("  2. Optionally set GOOGLE_CALENDAR_ID to a dedicated 'LifeOS' calendar id")
    print("  3. docker compose restart api")


if __name__ == "__main__":
    main()
