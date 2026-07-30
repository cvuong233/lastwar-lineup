# ACMB Position API

Small Flask backend that lets an R4 log in and save each player's battle
"position" (free text) from the public lineup page, without ever exposing a
GitHub token in that page's source.

## Why this exists

`lastwar/lineup.html` is a static page on GitHub Pages. It has no server of
its own, so it can't hold a secret. This API is the server: it holds the
GitHub token, checks the R4 password itself, and is the only thing that
actually writes to `players.json`.

## Endpoints

- `GET /health` — health check for Railway.
- `GET /api/players` — returns the full current `players.json` content, no
  auth required (same data that was previously public via GitHub Pages).
  Cached in-memory for 5 seconds to avoid hammering the GitHub API if many
  people load the page at once. The frontend now reads from here instead of
  fetching `players.json` as a static file, so data updates show up
  immediately without needing a redeploy of the frontend.
- `POST /api/login` — body `{"username": "...", "password": "..."}`.
  Returns `{"token": "<jwt>", "expires_in": <seconds>}` on success (401 on
  failure, 429 if a client IP has failed 5 times in the last 5 minutes).
- `POST /api/positions` — header `Authorization: Bearer <jwt>`, body
  `{"edits": [{"mode": "sm", "slot": "A", "section": "main", "name": "...", "pos": "..."}]}`.
  `pos: ""` clears the position. Fetches the latest `players.json` from
  GitHub, applies the edits, and commits — with up to 3 retries if another
  save landed in between (409 conflict).

## Environment variables

See `.env.example`. All are required except `JWT_TTL_SECONDS` (defaults to
30 days). A real, already-filled-in `.env` is sitting next to this file
(gitignored, so it never gets committed) — use it for local runs and as the
source of truth when copying values into Railway.

- `EDITOR_USERNAME` / `EDITOR_PASSWORD` — the R4 login.
- `JWT_SECRET` — long random string signing session tokens. Generate with
  `python3 -c "import secrets; print(secrets.token_hex(32))"`.
- `GITHUB_TOKEN` — fine-grained PAT scoped to **only** the target repo,
  with **Contents: Read and write** and nothing else.
- `GITHUB_OWNER`, `GITHUB_REPO`, `GITHUB_PATH`, `GITHUB_BRANCH` — where
  `players.json` lives.

## Local dev

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)
python3 app.py
```

## Deploy (Railway)

1. Push this folder to its own GitHub repo (or a subfolder of one).
2. Railway → New Project → Deploy from GitHub repo → pick it.
   If it's a subfolder, set **Settings → Source → Root Directory**.
3. **Variables** tab → add all vars from `.env` (real values) or
   `.env.example` (template) — see table in the setup instructions.
4. **Settings → Networking → Generate Domain** (this is an HTTP API, so —
   unlike a Telegram bot — it needs a public URL).
5. Check **Deployments → Logs** for the gunicorn boot line, then:
   ```bash
   curl https://<your-domain>.up.railway.app/health
   ```
6. Put that domain into `lastwar/lineup.html`'s `API_BASE` constant.

## Tests

```bash
pip install pytest
python3 -m pytest test_app.py -v
```

All 13 tests mock the GitHub calls — no network needed. There's also been a
manual live round-trip against the real `players.json` (write, verify,
revert) confirming the whole flow end-to-end.
