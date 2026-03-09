import base64
import datetime as dt
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from urllib import error as urlerror, parse as urlparse, request as urlrequest

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for

WORKSPACE = Path("/Users/fox/.openclaw/workspace")
FILE_APPROVAL_FLOW = WORKSPACE / "scripts" / "recs" / "file_approval_flow.py"
PENDING_DIR = WORKSPACE / "content" / "staging" / "pending"
APPROVED_DIR = WORKSPACE / "content" / "approved" / "transcripts"
REJECTED_DIR = WORKSPACE / "content" / "staging" / "rejected"
DECISIONS_FILE = WORKSPACE / "content" / "staging" / "decisions" / "video-decisions.jsonl"
VIDEO_NOTES_FILE = WORKSPACE / "content" / "staging" / "decisions" / "video-notes.jsonl"
ACTION_ACTOR = os.getenv("DASHBOARD_ACTOR", "dashboard")
NEO4J_BROWSER_BASE = os.getenv("NEO4J_BROWSER_BASE", "http://127.0.0.1:7474")
NEO4J_HTTP_ENDPOINT = os.getenv("NEO4J_HTTP_ENDPOINT", "http://127.0.0.1:7474/db/neo4j/tx/commit")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")


def _clamp_int(value: Any, default: int, min_value: int, max_value: int) -> int:
    try:
        out = int(str(value).strip())
    except Exception:
        return default
    return max(min_value, min(max_value, out))


def _neo4j_query(cypher: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    payload = {
        "statements": [
            {
                "statement": cypher,
                "parameters": params or {},
                "resultDataContents": ["row"],
            }
        ]
    }
    req = urlrequest.Request(
        NEO4J_HTTP_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")

    token = base64.b64encode(f"{NEO4J_USER}:{NEO4J_PASSWORD}".encode("utf-8")).decode("ascii")
    req.add_header("Authorization", f"Basic {token}")

    with urlrequest.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))

    errors = body.get("errors") or []
    if errors:
        first = errors[0] if isinstance(errors[0], dict) else {"message": str(errors[0])}
        raise RuntimeError(str(first.get("message") or "Neo4j query failed"))

    results = body.get("results") or []
    if not results:
        return []
    data = results[0].get("data") or []
    columns = results[0].get("columns") or []
    rows: list[dict[str, Any]] = []
    for entry in data:
        row = entry.get("row") or []
        mapped = {str(columns[i]): row[i] for i in range(min(len(columns), len(row)))}
        rows.append(mapped)
    return rows


def create_app() -> Flask:
    password = os.getenv("DASHBOARD_PASSWORD")
    if not password:
        raise RuntimeError("DASHBOARD_PASSWORD must be set before starting the app")

    app = Flask(__name__)
    app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-me-in-production")
    app.config["DASHBOARD_PASSWORD"] = password

    def _require_login() -> bool:
        return session.get("authenticated") is True

    def _parse_json(text: str) -> dict[str, Any]:
        try:
            out = json.loads(text or "{}")
            return out if isinstance(out, dict) else {}
        except Exception:
            return {}

    def _item_id(item: dict[str, Any]) -> str:
        base = f"{item.get('video_id','')}|{item.get('url','')}|{item.get('created_at','')}"
        return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]

    def _extract_title(item: dict[str, Any]) -> str:
        meta = _parse_json(str(item.get("metadata_json") or "{}"))
        return str(meta.get("title") or item.get("video_id") or "Untitled")

    def _extract_uploader(item: dict[str, Any]) -> str:
        meta = _parse_json(str(item.get("metadata_json") or "{}"))
        return str(meta.get("uploader") or "")

    def _extract_actor(item: dict[str, Any]) -> str:
        decision = item.get("decision_event") or {}
        if not isinstance(decision, dict):
            return ""
        meta = _parse_json(str(decision.get("metadata_json") or "{}"))
        return str(meta.get("actor") or "")

    def _normalize_bucket_item(item: dict[str, Any], bucket: str) -> dict[str, Any]:
        out = dict(item)
        out["bucket"] = bucket
        out["item_id"] = _item_id(out)
        out["title"] = _extract_title(out)
        out["uploader"] = _extract_uploader(out)
        out["acted_by"] = _extract_actor(out)
        out["status"] = bucket
        out["can_act"] = bucket == "pending"
        decision = out.get("decision_event") or {}
        if isinstance(decision, dict):
            out["actioned_at"] = str(decision.get("created_at") or "")
            out["decision_note"] = str(decision.get("reason") or "").strip()
        else:
            out["actioned_at"] = ""
            out["decision_note"] = ""
        return out

    def _run_json_command(cmd: list[str], failure_message: str) -> tuple[bool, dict[str, Any], str, int, list[str]]:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()

        payload = {}
        if stdout:
            try:
                payload = json.loads(stdout)
                if not isinstance(payload, dict):
                    payload = {"raw": stdout}
            except Exception:
                payload = {"raw": stdout}

        ok = proc.returncode == 0
        if not ok and not payload:
            payload = {"error": stderr or failure_message}
        return ok, payload, stderr, proc.returncode, cmd

    def _run_file_approval_flow(args: list[str]) -> tuple[bool, dict[str, Any], str, int, list[str]]:
        cmd = ["python3", str(FILE_APPROVAL_FLOW), *args]
        return _run_json_command(cmd, "file_approval_flow command failed")

    def _coerce_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            return json.dumps(value)
        return str(value).strip()

    def _extract_error_detail(payload: dict[str, Any], stderr: str, returncode: int) -> str:
        for key in ("error", "message", "detail"):
            detail = _coerce_text(payload.get(key))
            if detail:
                return detail

        raw = _coerce_text(payload.get("raw"))
        if raw:
            return raw

        if isinstance(payload.get("approval"), dict):
            approval_error = _coerce_text(payload["approval"].get("error"))
            if approval_error:
                return approval_error

        err = _coerce_text(stderr)
        if err:
            lines = [line.strip() for line in err.splitlines() if line.strip()]
            if lines:
                return lines[-1]
            return err

        return f"kb_cli command failed with exit code {returncode}"

    def _build_success_message(action: str, payload: dict[str, Any]) -> str:
        if action == "approve":
            source_id = payload.get("source_id")
            if isinstance(source_id, int):
                return f"Approved and ingested source #{source_id}."
            return "Approved successfully."
        return "Rejected successfully."

    def _parse_frontmatter(path: Path) -> dict[str, Any]:
        text = path.read_text(errors="ignore")
        if not text.startswith("---\n"):
            return {}
        end = text.find("\n---\n", 4)
        if end < 0:
            return {}
        block = text[4:end].splitlines()
        out: dict[str, Any] = {}
        for line in block:
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k = k.strip()
            v = v.strip()
            try:
                out[k] = json.loads(v)
            except Exception:
                out[k] = v.strip('"')
        return out

    def _load_decision_events() -> dict[str, dict[str, Any]]:
        events: dict[str, dict[str, Any]] = {}
        if not DECISIONS_FILE.exists():
            return events
        for line in DECISIONS_FILE.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            vid = str(row.get("video_id") or "").strip()
            if vid:
                events[vid] = row
        return events

    def _load_video_notes() -> dict[str, list[dict[str, Any]]]:
        notes: dict[str, list[dict[str, Any]]] = {}
        if not VIDEO_NOTES_FILE.exists():
            return notes
        for line in VIDEO_NOTES_FILE.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            vid = str(row.get("video_id") or "").strip()
            note = str(row.get("note") or "").strip()
            if not vid or not note:
                continue
            entry = {
                "at": str(row.get("at") or ""),
                "by": str(row.get("by") or ""),
                "note": note,
            }
            notes.setdefault(vid, []).append(entry)

        for vid in notes:
            notes[vid].sort(key=lambda x: x.get("at") or "")
        return notes

    def _append_video_note(video_id: str, note: str, by: str) -> dict[str, Any]:
        VIDEO_NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "video_id": video_id,
            "note": note,
            "by": by,
        }
        with VIDEO_NOTES_FILE.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def _items_from_dir(path: Path, bucket: str, decision_events: dict[str, dict[str, Any]], notes_by_video: dict[str, list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        items: list[dict[str, Any]] = []
        files = sorted(path.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[:limit]:
            fm = _parse_frontmatter(p)
            video_id = str(fm.get("video_id") or "").strip()
            title = str(fm.get("title") or video_id or p.stem)
            uploader = str(fm.get("uploader") or "")
            url = str(fm.get("url") or "")
            decision_event = decision_events.get(video_id, {}) if video_id else {}
            metadata = {"title": title, "uploader": uploader}
            note_history = list(notes_by_video.get(video_id, [])) if video_id else []
            row = {
                "video_id": video_id,
                "url": url,
                "source_id": None,
                "created_at": str(fm.get("decided_at") or fm.get("run_date") or ""),
                "metadata_json": json.dumps(metadata),
                "decision_event": {
                    "created_at": decision_event.get("at") or "",
                    "reason": decision_event.get("reason") or "",
                    "metadata_json": json.dumps({"actor": decision_event.get("by") or ""}),
                }
                if decision_event
                else {},
            }
            normalized = _normalize_bucket_item(row, bucket)
            normalized["notes"] = note_history
            normalized["latest_note"] = note_history[-1]["note"] if note_history else ""
            items.append(normalized)
        return items

    def load_review_buckets(limit: int = 200) -> dict[str, Any]:
        try:
            decision_events = _load_decision_events()
            notes_by_video = _load_video_notes()
            pending = _items_from_dir(PENDING_DIR, "pending", decision_events, notes_by_video, limit)
            approved = _items_from_dir(APPROVED_DIR, "approved", decision_events, notes_by_video, limit)
            rejected = _items_from_dir(REJECTED_DIR, "rejected", decision_events, notes_by_video, limit)
        except Exception as exc:
            return {
                "ok": False,
                "error": f"Failed to load review buckets: {exc}",
                "run_date": "",
                "counts": {"pending": 0, "approved": 0, "rejected": 0, "total": 0},
                "pending": [],
                "approved": [],
                "rejected": [],
            }

        return {
            "ok": True,
            "run_date": "",
            "counts": {
                "pending": len(pending),
                "approved": len(approved),
                "rejected": len(rejected),
                "total": len(pending) + len(approved) + len(rejected),
            },
            "pending": pending,
            "approved": approved,
            "rejected": rejected,
        }

    @app.route("/")
    def home():
        if _require_login():
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            entered = request.form.get("password", "")
            if entered == app.config["DASHBOARD_PASSWORD"]:
                session["authenticated"] = True
                return redirect(url_for("dashboard"))
            error = "Invalid password"
        return render_template("login.html", error=error)

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/dashboard")
    def dashboard():
        if not _require_login():
            return redirect(url_for("login"))
        data = load_review_buckets(limit=200)
        return render_template("dashboard.html", data=data)

    @app.route("/dashboard/graph")
    def dashboard_graph():
        if not _require_login():
            return redirect(url_for("login"))
        return render_template("graph.html")

    @app.route("/dashboard/neo4j/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    @app.route("/dashboard/neo4j/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    def neo4j_proxy(path: str):
        if not _require_login():
            return jsonify({"ok": False, "error": "Unauthorized"}), 401

        upstream = f"{NEO4J_BROWSER_BASE.rstrip('/')}/{path}"
        if request.query_string:
            upstream = f"{upstream}?{request.query_string.decode('utf-8', errors='ignore')}"

        body = request.get_data() if request.method in {"POST", "PUT", "PATCH", "DELETE"} else None
        req = urlrequest.Request(upstream, data=body, method=request.method)

        content_type = request.headers.get("Content-Type")
        if content_type:
            req.add_header("Content-Type", content_type)

        auth_header = request.headers.get("Authorization")
        if auth_header:
            req.add_header("Authorization", auth_header)

        try:
            with urlrequest.urlopen(req, timeout=60) as resp:
                data = resp.read()
                excluded = {"connection", "transfer-encoding", "content-encoding", "content-length", "x-frame-options"}
                headers = [(k, v) for k, v in resp.getheaders() if k.lower() not in excluded]
                headers.append(("X-Frame-Options", "SAMEORIGIN"))
                return Response(data, status=resp.status, headers=headers)
        except urlerror.HTTPError as e:
            data = e.read() if e.fp else b""
            headers = []
            if hasattr(e, "headers") and e.headers:
                excluded = {"connection", "transfer-encoding", "content-encoding", "content-length", "x-frame-options"}
                headers = [(k, v) for k, v in e.headers.items() if k.lower() not in excluded]
            headers.append(("X-Frame-Options", "SAMEORIGIN"))
            return Response(data, status=e.code, headers=headers)
        except Exception as e:
            return jsonify({"ok": False, "error": f"Neo4j proxy error: {e}"}), 502


    @app.route("/api/graph/search", methods=["GET"])
    def api_graph_search():
        if not _require_login():
            return jsonify({"ok": False, "error": "Unauthorized"}), 401

        query = str(request.args.get("q") or "").strip()
        if len(query) < 1:
            return jsonify({"ok": True, "nodes": []})

        limit = _clamp_int(request.args.get("limit"), default=10, min_value=1, max_value=50)
        cypher = """
        MATCH (n)
        WHERE toLower(coalesce(n.name, "")) CONTAINS toLower($q)
        WITH n, COUNT { (n)--() } AS degree
        RETURN coalesce(n.name, toString(id(n))) AS id,
               coalesce(n.name, toString(id(n))) AS label,
               coalesce(head(labels(n)), "Entity") AS `group`,
               degree
        ORDER BY degree DESC, label ASC
        LIMIT $limit
        """
        try:
            rows = _neo4j_query(cypher, {"q": query, "limit": limit})
            return jsonify({"ok": True, "nodes": rows})
        except Exception as exc:
            return jsonify({"ok": False, "error": f"Graph search failed: {exc}"}), 502

    @app.route("/api/graph/top", methods=["GET"])
    def api_graph_top():
        if not _require_login():
            return jsonify({"ok": False, "error": "Unauthorized"}), 401

        limit = _clamp_int(request.args.get("limit"), default=12, min_value=1, max_value=50)
        cypher = """
        MATCH (n)
        WITH n, COUNT { (n)--() } AS degree
        WHERE degree > 0
        RETURN coalesce(n.name, toString(id(n))) AS id,
               coalesce(n.name, toString(id(n))) AS label,
               coalesce(head(labels(n)), "Entity") AS `group`,
               degree
        ORDER BY degree DESC, label ASC
        LIMIT $limit
        """
        try:
            rows = _neo4j_query(cypher, {"limit": limit})
            return jsonify({"ok": True, "nodes": rows})
        except Exception as exc:
            return jsonify({"ok": False, "error": f"Top entities query failed: {exc}"}), 502

    @app.route("/api/graph/neighbors", methods=["GET"])
    def api_graph_neighbors():
        if not _require_login():
            return jsonify({"ok": False, "error": "Unauthorized"}), 401

        entity = str(request.args.get("entity") or "").strip()
        if not entity:
            return jsonify({"ok": False, "error": "Missing entity"}), 400

        hops = _clamp_int(request.args.get("hops"), default=1, min_value=1, max_value=2)
        limit = _clamp_int(request.args.get("limit"), default=80, min_value=1, max_value=200)
        edge_limit = min(400, max(20, limit * 4))

        node_query = f"""
        MATCH (c {{name: $entity}})
        OPTIONAL MATCH (c)-[*1..{hops}]-(n)
        WITH c, collect(DISTINCT n)[0..$limit] AS neighbors
        WITH [c] + neighbors AS nodes
        UNWIND nodes AS n
        WITH DISTINCT n
        RETURN coalesce(n.name, toString(id(n))) AS id,
               coalesce(n.name, toString(id(n))) AS label,
               coalesce(head(labels(n)), "Entity") AS `group`,
               COUNT {{ (n)--() }} AS degree
        """

        edge_query = f"""
        MATCH (c {{name: $entity}})
        MATCH p=(c)-[*1..{hops}]-(n)
        UNWIND relationships(p) AS rel
        WITH DISTINCT rel
        LIMIT $edge_limit
        RETURN coalesce(startNode(rel).name, toString(id(startNode(rel)))) AS `from`,
               coalesce(endNode(rel).name, toString(id(endNode(rel)))) AS `to`,
               type(rel) AS label,
               coalesce(rel.fact, rel.evidence, rel.description, "") AS fact
        """

        try:
            nodes = _neo4j_query(node_query, {"entity": entity, "limit": limit})
            if not nodes:
                return jsonify({"ok": False, "error": "Entity not found"}), 404

            edges = _neo4j_query(edge_query, {"entity": entity, "edge_limit": edge_limit})
            node_ids = {str(n.get("id")) for n in nodes}
            safe_edges = [e for e in edges if str(e.get("from")) in node_ids and str(e.get("to")) in node_ids]
            return jsonify({"ok": True, "nodes": nodes, "edges": safe_edges})
        except Exception as exc:
            return jsonify({"ok": False, "error": f"Neighbor query failed: {exc}"}), 502

    @app.route("/api/review-buckets", methods=["GET"])
    def api_review_buckets():
        if not _require_login():
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return jsonify(load_review_buckets(limit=200))

    @app.route("/api/recommendations/<item_id>/<action>", methods=["POST"])
    def take_action(item_id: str, action: str):
        if not _require_login():
            return jsonify({"ok": False, "error": "Unauthorized"}), 401

        action = action.lower().strip()
        if action not in {"approve", "reject"}:
            return jsonify({"ok": False, "error": "Action must be approve or reject"}), 400

        data = load_review_buckets(limit=500)
        pending = data.get("pending") or []
        item = next((r for r in pending if r.get("item_id") == item_id), None)
        if not item:
            return jsonify({"ok": False, "error": "Pending recommendation not found"}), 404

        body = request.get_json(silent=True) or {}
        note = str((body.get("note") if isinstance(body, dict) else "") or "").strip()
        cmd = ["--decision", action, "--video-id", str(item.get("video_id") or ""), "--by", ACTION_ACTOR]
        source_id = item.get("source_id")
        if source_id is not None and str(source_id).strip() not in {"", "None"}:
            cmd += ["--source-id", str(source_id)]
        else:
            url = str(item.get("url") or "").strip()
            if url:
                cmd += ["--url", url]

        title = str(item.get("title") or "").strip()
        if title:
            cmd += ["--title", title]

        if note:
            cmd += ["--reason", note]

        ok, payload, stderr, returncode, full_cmd = _run_file_approval_flow(cmd)
        status = 200 if ok else 500
        message = _build_success_message(action, payload) if ok else ""
        error_detail = "" if ok else _extract_error_detail(payload, stderr, returncode)

        if not ok:
            app.logger.error(
                "dashboard action failed: action=%s item_id=%s video_id=%s returncode=%s cmd=%s stderr=%s payload=%s",
                action,
                item_id,
                item.get("video_id"),
                returncode,
                " ".join(full_cmd),
                stderr,
                payload,
            )

        return (
            jsonify(
                {
                    "ok": ok,
                    "action": action,
                    "item_id": item_id,
                    "video_id": item.get("video_id"),
                    "title": item.get("title"),
                    "note": note,
                    "result": payload,
                    "stderr": stderr,
                    "returncode": returncode,
                    "message": message,
                    "error": error_detail,
                }
            ),
            status,
        )

    @app.route("/api/videos/<video_id>/notes", methods=["POST"])
    def add_video_note(video_id: str):
        if not _require_login():
            return jsonify({"ok": False, "error": "Unauthorized"}), 401

        resolved_video_id = str(video_id or "").strip()
        if not resolved_video_id:
            return jsonify({"ok": False, "error": "Missing video_id"}), 400

        body = request.get_json(silent=True) or {}
        note = str((body.get("note") if isinstance(body, dict) else "") or "").strip()
        if not note:
            return jsonify({"ok": False, "error": "Note cannot be empty"}), 400

        item = None
        data = load_review_buckets(limit=1000)
        for bucket in ("approved", "pending", "rejected"):
            candidate = next((r for r in (data.get(bucket) or []) if str(r.get("video_id") or "").strip() == resolved_video_id), None)
            if candidate:
                item = candidate
                break

        if not item:
            return jsonify({"ok": False, "error": "Video not found in dashboard buckets"}), 404

        row = _append_video_note(video_id=resolved_video_id, note=note, by=ACTION_ACTOR)
        notes = _load_video_notes().get(resolved_video_id, [])
        return jsonify({"ok": True, "video_id": resolved_video_id, "note": row, "notes": notes, "message": "Note saved."})

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.getenv("PORT", "8080"))
    app.run(host="127.0.0.1", port=port)
