# Group Theater (MovieSite)

Synced watch-party rooms. Uploads go to **Mux** (transcoded HLS) so everyone gets the same playable stream. The uploader previews instantly from a local blob while Mux processes.

## Railway setup

1. Create a free Mux account → Access Tokens → create token with **Mux Video** write permissions.
2. On the Railway service, set:
   - `MUX_TOKEN_ID`
   - `MUX_TOKEN_SECRET`
   - `SECRET_KEY` (any long random string)
3. Deploy from `main`. Start command is already in `railway.json` (gunicorn gthread).

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
