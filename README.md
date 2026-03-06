# fox-dashboard

Simple Flask dashboard to review daily video recommendations and send approve/reject actions to the existing Telegram recs workflow.

## Features
- Password login with Flask session auth
- Reads review buckets from KB backend via:
  - `scripts/kb/kb_cli.sh --base-dir /Users/fox/.openclaw/workspace/rag_kb_data review-buckets --limit 200`
- Tabs for Pending, Approved, Rejected
- Pending cards have Approve/Reject actions
- Approved/Rejected cards show status + who actioned
- Actions trigger KB backend directly:
  - `approve-video ... --by <actor>`
  - `reject-video ... --by <actor>`

## Requirements
- Python 3.10+
- `DASHBOARD_PASSWORD` environment variable must be set, app refuses startup otherwise

## Setup
```bash
cd /Users/fox/projects/fox-dashboard
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set env vars (or load from `.env` using your preferred method):
```bash
export DASHBOARD_PASSWORD='your-strong-password'
export PORT=8080
export FLASK_SECRET_KEY='random-long-secret'
```

## Run
```bash
cd /Users/fox/projects/fox-dashboard
source .venv/bin/activate
python3 app.py
```

App listens on `127.0.0.1:${PORT:-8080}` behind Nginx.

## Tailscale access note
Because the server binds to `0.0.0.0`, it can be reached over your Tailscale IP when running on a Tailscale-connected host. Example:
- `http://<tailscale-ip>:8080/login`

Use Tailscale ACLs and a strong `DASHBOARD_PASSWORD`.

## Endpoints
- `GET /login`
- `POST /login`
- `GET /dashboard`
- `POST /logout`
- `GET /api/review-buckets`
- `POST /api/recommendations/<item_id>/approve`
- `POST /api/recommendations/<item_id>/reject`
