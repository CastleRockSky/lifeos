# Google Calendar sync setup

Phase 10's calendar_sync mirrors every action_item with a due_date
into a Google Calendar. Recurring action items get RRULE entries.
Status changes propagate: marking an action completed deletes the
calendar event; rescheduling updates it.

This is a one-time setup against your Google account. After it's
running, your downstream calendar bot just reads from Google Calendar
like it does any other event.

---

## 1. Google Cloud Console

You need an OAuth client to authenticate as your Google user. If you
already have a Cloud project, use it; otherwise create one
(`Console → New Project`).

1. **Enable the Calendar API.**
   - APIs & Services → Library → search "Google Calendar API" → Enable.

2. **Create an OAuth consent screen** (if you haven't already for this
   project).
   - APIs & Services → OAuth consent screen.
   - User Type: **External** (works for personal `@gmail.com` accounts too).
   - App name: "LifeOS" (or whatever).
   - Support email + developer email: your address.
   - Scopes: skip (we request scopes per-flow).
   - Test users: add `mr@davidcol.es` so Google won't block the flow
     while the app is in "Testing" status (you don't need to publish).

3. **Create the OAuth client.**
   - APIs & Services → Credentials → Create Credentials → OAuth client ID.
   - Application type: **Desktop app** (this is critical — it enables the
     loopback-redirect flow our bootstrap script uses).
   - Name: "LifeOS Calendar".
   - Download the JSON file when prompted.

## 2. Place the client JSON on the host

```bash
# Make sure the auth dir exists (created by the backup-share setup too)
sudo mkdir -p /srv/lifeos/auth

# Move + lock down
sudo mv ~/Downloads/client_secret_*.json /srv/lifeos/auth/google-oauth-client.json
sudo chown root:root /srv/lifeos/auth/google-oauth-client.json
sudo chmod 600 /srv/lifeos/auth/google-oauth-client.json
```

The `/srv/lifeos/auth` directory is mounted into the api container as
`/data/auth`, so the bootstrap script will see it.

## 3. (Optional but recommended) Create a dedicated "LifeOS" calendar

Keeps LifeOS-pushed events visually separate from your personal/work
calendar.

1. Open Google Calendar → left sidebar → click `+` next to "Other
   calendars" → **Create new calendar**.
2. Name it `LifeOS`. Save.
3. Click the new calendar in the sidebar → **Settings**.
4. Scroll to **Integrate calendar** → copy the **Calendar ID**. It
   looks like `<long-hash>@group.calendar.google.com`.

You'll paste this into `.env` in step 5.

## 4. Run the OAuth bootstrap

```bash
docker exec -it lifeos-api python scripts/bootstrap_google_calendar.py
```

The script will:

1. Print an authorization URL.
2. Open the URL in any browser, sign in as `mr@davidcol.es`, click
   "Allow". The first time you'll see an "unverified app" warning
   because the OAuth consent screen is in Testing mode — click
   **Advanced → Go to LifeOS (unsafe)**. Safe because *you* are the app.
3. Google redirects your browser to `http://localhost/?code=...&...`.
   The page won't load (nothing's listening). **That's expected.**
   The URL bar contains the code we need.
4. Copy the entire URL out of the address bar (or just the `code=...`
   value) and paste it back into the docker exec terminal.
5. The script writes `/data/auth/google-calendar-tokens.json` and prints
   "Tokens written to …".

## 5. Wire up `.env`

Edit `~/lifeos/.env`:

```
GOOGLE_CALENDAR_ENABLED=true

# Use 'primary' to use your default Google Calendar (mr@davidcol.es)
# Or paste the dedicated calendar id from step 3
GOOGLE_CALENDAR_ID=<long-hash>@group.calendar.google.com

# Optional: only sync action items from certain domains (blank = all)
# GOOGLE_CALENDAR_DOMAINS=medical,vet,auto

# Optional: prefix that goes into the event title — defaults to "[LifeOS] "
# GOOGLE_CALENDAR_EVENT_PREFIX=[LifeOS]

# Optional: base URL inserted into event descriptions so you can click
# from Google Calendar back to the LifeOS action item.
# GOOGLE_CALENDAR_LINK_BASE=https://lifeos.davidcol.es
```

## 6. Restart the api container

`docker compose restart api` won't reload env from `.env`. Use:

```bash
docker compose up -d --force-recreate api
```

## 7. Verify

```bash
# Server-side status
curl -s http://localhost:8000/api/calendar/status | python3 -m json.tool
# Expect: enabled: true, configured: true, calendar_id: <yours>

# Or check the log for the watcher confirmation
docker logs lifeos-api 2>&1 | grep -i calendar | tail -5
```

Now create a test action item with a due_date — easiest is via the web
UI under Actions, or just wait for the next email to come in with an
action-bearing document. Within a few seconds, an event titled
`[LifeOS] <action title>` should appear on the chosen Google Calendar.

## What gets synced

| Trigger | Calendar effect |
|---|---|
| Action item created with `due_date` | New event (all-day on due_date) |
| Action item with `recurrence_rule="monthly"` | Event with `RRULE:FREQ=MONTHLY;COUNT=12` |
| Action item updated (title, date, etc.) | Existing event updated in place |
| Action item completed or dismissed | Event deleted |
| Action item updated with no `due_date` | Event deleted (date removed) |

If `GOOGLE_CALENDAR_DOMAINS` is set, only items in those domains sync.

Failures in any of the above are non-fatal — they log a warning in the
api container but don't roll back the LifeOS action change.

## Rotating credentials

If the token ever stops working (e.g. you change the OAuth consent
screen status from Testing to In Production, or you revoke access from
your Google account dashboard), re-run the bootstrap. It overwrites
the existing `google-calendar-tokens.json`.

## Troubleshooting

**`AccessDeniedException` / "unverified app" warning blocks you.**
- OAuth consent screen → Testing → add `mr@davidcol.es` to Test users.

**Bootstrap says "Could not find 'code=' in that URL".**
- You probably pasted before the redirect finished, or pasted the
  authorization URL instead of the redirect URL. Re-run; this time wait
  for the URL bar to update after clicking "Allow".

**`/api/calendar/status` shows `enabled: true, configured: false`.**
- `google-calendar-tokens.json` isn't where the api expects. The file
  must live at the path in `GOOGLE_CREDENTIALS_PATH` (default
  `/data/auth/google-calendar-tokens.json` inside the container =
  `/srv/lifeos/auth/google-calendar-tokens.json` on the host).

**Events aren't appearing.**
- `docker logs -f lifeos-api 2>&1 | grep -i calendar` — look for any
  `Calendar … failed:` warnings. Usually missing scope, expired token,
  or the calendar ID is wrong (a deleted calendar will silently 404).
