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
5. Redeploy. Railway runs `flask --app movie_theater.py db upgrade` before starting the app. Check `https://YOUR-APP.up.railway.app/api/mux/health` — it should return `{"ok": true}`.

Do **not** use the Mux Data “environment key” (from the player/analytics setup screen). That is not an API access token.

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
- Drop a video → host plays immediately from the original file
- File uploads directly to Mux (not through Railway body limits)
- When Mux is ready, the whole room switches to the shared HLS URL (synced via Socket.IO)
- Host can **Download original**
- Finished videos are removed from the queue and deleted from Mux
