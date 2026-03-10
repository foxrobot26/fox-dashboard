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
- New **Multimodal RAG Lab** page (`/dashboard/multimodal`) with:
  - text query input
  - optional image upload query
  - top-k selector
  - result cards with score, type, preview, and source metadata
  - persistent LanceDB-backed index (no per-request in-memory rebuild)
  - status badges for API key mode, backend mode, table rows, and last sync

## Requirements
- Python 3.10+
- `DASHBOARD_PASSWORD` environment variable must be set, app refuses startup otherwise
- Python deps in `requirements.txt` (includes `numpy`, optional live-mode Gemini dependency `google-genai`, and `lancedb` for persistent vector storage)

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

# Optional: Multimodal RAG Lab live mode (image query + Gemini embeddings)
export GEMINI_API_KEY='your-gemini-key'
export GEMINI_EMBEDDING_MODEL='gemini-embedding-2-preview'

# Optional: point the lab to a specific dataset/index root
export MULTIMODAL_RAG_BASE_DIR='/Users/fox/.openclaw/workspace/gemini-multimodal-rag-lab'
```

## Run
```bash
cd /Users/fox/projects/fox-dashboard
source .venv/bin/activate
python3 app.py
```

App listens on `127.0.0.1:${PORT:-8080}` behind Nginx.

## Multimodal RAG Lab data setup
The dashboard expects data in:
- `${MULTIMODAL_RAG_BASE_DIR}/data/text/*.txt`
- `${MULTIMODAL_RAG_BASE_DIR}/data/images/*` (png/jpg/jpeg)

Default base dir:
- `/Users/fox/.openclaw/workspace/gemini-multimodal-rag-lab`

If no data files are present, the page stays in a friendly "index not ready" state and retrieval returns a clear setup hint.

## Multimodal index operations (LanceDB)
One-time build or sync:
```bash
cd /Users/fox/projects/fox-dashboard
source .venv/bin/activate
python3 multimodal_rag.py sync --base-dir "$MULTIMODAL_RAG_BASE_DIR"
```

Force full rebuild:
```bash
python3 multimodal_rag.py sync --base-dir "$MULTIMODAL_RAG_BASE_DIR" --force
```

Inspect backend status and table info:
```bash
python3 multimodal_rag.py status --base-dir "$MULTIMODAL_RAG_BASE_DIR"
```

On-disk LanceDB location and metadata:
- `${MULTIMODAL_RAG_BASE_DIR}/output/lancedb/`
- table name: `multimodal_embeddings`
- sync metadata: `index_meta.json`

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
