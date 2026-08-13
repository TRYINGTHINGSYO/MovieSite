# Group Theater (MovieSite)

Synced watch-party rooms. Uploads go to **Mux** (transcoded HLS) so everyone gets the same playable stream. The uploader previews instantly from a local blob while Mux processes.

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

Run tests with `pytest`. Set `TEST_POSTGRES_URL` to include the PostgreSQL integration test.

## Behavior

- Register or sign in to create and join persistent rooms
- Owned and joined rooms remain on the saved-room dashboard
- File uploads go directly to Mux (not through Railway body limits) and are saved
  to the room library before they are independently added to the queue
- Privileged members can save browser-probed direct media URLs without the Flask
  server fetching or proxying the submitted URL
- Playback, queue, library, permissions, requests, and presence synchronize over
  authoritative Socket.IO room state
- The backend stores reusable media separately from stable-ID queue entries
- Room owners are implicitly fully privileged; other members are viewers until granted permissions
- Anonymous guests can watch active rooms and submit validated action requests;
  reviewers approve through the same permission-checked command services
- Finishing a video removes only its queue entry; the saved media and Mux asset remain
