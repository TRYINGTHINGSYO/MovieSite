# Group Theater (MovieSite)

Synced watch-party rooms. Uploads go to **Mux** (transcoded HLS) so everyone gets the same playable stream. The uploader previews instantly from a local blob while Mux processes.

## Railway setup

1. In Mux dashboard go to **Settings → Access Tokens** (not Mux Data).
2. Create a token with **Mux Video — Read** and **Write**.
3. On the Railway service Variables, set:
   - `MUX_TOKEN_ID` = Token ID (UUID-looking)
   - `MUX_TOKEN_SECRET` = Token Secret (long string)
   - `SECRET_KEY` = any long random string
4. Redeploy. Check `https://YOUR-APP.up.railway.app/api/mux/health` — should return `{"ok": true}`.

Do **not** use the Mux Data “environment key” (from the player/analytics setup screen). That is not an API access token.

## Local run

```bash
pip install -r requirements.txt
set MUX_TOKEN_ID=...
set MUX_TOKEN_SECRET=...
python movie_theater.py
```

## Behavior

- Drop a video → host plays immediately from the original file
- File uploads directly to Mux (not through Railway body limits)
- When Mux is ready, the whole room switches to the shared HLS URL (synced via Socket.IO)
- Host can **Download original**
- Finished videos are removed from the queue and deleted from Mux
