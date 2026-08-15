# Group Theater (MovieSite)

Synced watch-party rooms with browser-local media and direct-link playback. New local videos are persisted in the adding browser with OPFS; their bytes are not uploaded to Flask, PostgreSQL, Railway, or Mux. Existing Mux playback remains supported as the cloud/fallback source architecture evolves.

## Railway setup

1. In Mux dashboard go to **Settings → Access Tokens** (not Mux Data).
2. Create a token with **Mux Video — Read** and **Write**.
3. Add a Railway PostgreSQL service. In the app service, add `DATABASE_URL` as a reference to `${{Postgres.DATABASE_URL}}` (replace `Postgres` if the database service has another name).
4. On the Railway service Variables, set:
   - `MUX_TOKEN_ID` = Token ID (UUID-looking)
   - `MUX_TOKEN_SECRET` = Token Secret (long string)
   - `SECRET_KEY` = any long random string; keep it stable across deployments
   - `SOCKETIO_ALLOWED_ORIGINS` = optional comma-separated public origins; same-origin only by default
   - `RATELIMIT_STORAGE_URI` = optional shared rate-limit backend; the single-worker default uses memory
   - `DIRECT_URL_REQUIRE_HTTPS` = optional local override to force HTTPS; Railway requires HTTPS regardless
  - yt-dlp is installed from `requirements.txt` and extracts playable media from pasted site links
5. Redeploy. Railway runs `flask --app movie_theater.py db upgrade` before starting the app. Check `https://YOUR-APP.up.railway.app/api/mux/health` — it should return `{"ok": true}`.

Do **not** use the Mux Data “environment key” (from the player/analytics setup screen). That is not an API access token.

The Stage 2A expand/backfill migration should be deployed during a brief maintenance window so the previous process cannot create Stage 1 queue rows during the backfill. It retains the Stage 1 `room_videos` table for immediate validation rollback. The downgrade refuses to run after Stage 2-only media, queue, permission, request, or provider-state changes would make rollback lossy; use the pre-deployment database snapshot in that case. A later contract migration will remove the legacy table after validation.

## Local run

```bash
pip install -r requirements-dev.txt
set DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE
set SECRET_KEY=local-development-secret
set MUX_TOKEN_ID=...
set MUX_TOKEN_SECRET=...
flask --app movie_theater.py db upgrade
python movie_theater.py
```

The app falls back to SQLite when `DATABASE_URL` is omitted. PostgreSQL is required for deployment.

Run backend tests with `pytest` and browser-storage module tests with
`npm run test:js`. Set `TEST_POSTGRES_URL` to include the PostgreSQL integration
test.

## Behavior

- Register or sign in to create and join persistent rooms
- Owned and joined rooms remain on the saved-room dashboard
- Local videos are quota-checked, persisted in OPFS, registered as metadata-only
  `BrowserLocalSource` records, and restored after refresh on the owning browser
- Browser identity uses a durable random client token; only its SHA-256 digest is
  stored in PostgreSQL, and local storage keys are disclosed only to that browser
- Local files remain usable without Mux and are never sent through Railway
- Privileged members can paste a YouTube or other site link; yt-dlp extracts a
  playable clip, the room queues it, and the player starts it. Direct `.mp4`
  files that this browser can play are still used as-is without a server fetch
- Playback, queue, library, permissions, requests, and presence synchronize over
  authoritative Socket.IO room state
- The backend stores reusable media separately from stable-ID queue entries
- Room owners are implicitly fully privileged; other members are viewers until granted permissions
- Anonymous guests can watch active rooms and submit validated action requests;
  reviewers approve through the same permission-checked command services
- Finishing a video removes only its queue entry; the saved media and Mux asset remain
