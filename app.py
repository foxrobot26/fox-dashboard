import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

WORKSPACE = Path("/Users/fox/.openclaw/workspace")
KB_CLI = WORKSPACE / "scripts" / "kb" / "kb_cli.sh"
KB_BASE_DIR = WORKSPACE / "rag_kb_data"
ACTION_ACTOR = os.getenv("DASHBOARD_ACTOR", "dashboard")


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
        out["actioned_at"] = str(decision.get("created_at") or "") if isinstance(decision, dict) else ""
        return out

    def _run_kb_json(args: list[str]) -> tuple[bool, dict[str, Any], str]:
        cmd = [str(KB_CLI), "--base-dir", str(KB_BASE_DIR), *args]
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
            payload = {"error": stderr or "kb_cli command failed"}
        return ok, payload, stderr

    def load_review_buckets(limit: int = 200) -> dict[str, Any]:
        ok, payload, stderr = _run_kb_json(["review-buckets", "--limit", str(limit)])
        if not ok:
            return {
                "ok": False,
                "error": payload.get("error") or payload.get("raw") or stderr or "Failed to load review buckets",
                "run_date": "",
                "counts": {"pending": 0, "approved": 0, "rejected": 0, "total": 0},
                "pending": [],
                "approved": [],
                "rejected": [],
            }

        pending = [_normalize_bucket_item(i, "pending") for i in list(payload.get("pending") or []) if isinstance(i, dict)]
        approved = [_normalize_bucket_item(i, "approved") for i in list(payload.get("approved") or []) if isinstance(i, dict)]
        rejected = [_normalize_bucket_item(i, "rejected") for i in list(payload.get("rejected") or []) if isinstance(i, dict)]

        counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
        return {
            "ok": True,
            "run_date": str(payload.get("run_date") or ""),
            "counts": {
                "pending": int(counts.get("pending", len(pending)) or 0),
                "approved": int(counts.get("approved", len(approved)) or 0),
                "rejected": int(counts.get("rejected", len(rejected)) or 0),
                "total": int(counts.get("total", len(pending) + len(approved) + len(rejected)) or 0),
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

        cmd = [f"{action}-video", "--video-id", str(item.get("video_id") or ""), "--by", ACTION_ACTOR]
        source_id = item.get("source_id")
        if source_id is not None and str(source_id).strip() not in {"", "None"}:
            cmd += ["--source-id", str(source_id)]
        else:
            url = str(item.get("url") or "").strip()
            if url:
                cmd += ["--url", url]

        ok, payload, stderr = _run_kb_json(cmd)
        status = 200 if ok else 500
        return (
            jsonify(
                {
                    "ok": ok,
                    "action": action,
                    "item_id": item_id,
                    "video_id": item.get("video_id"),
                    "title": item.get("title"),
                    "result": payload,
                    "stderr": stderr,
                }
            ),
            status,
        )

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.getenv("PORT", "8080"))
    app.run(host="127.0.0.1", port=port)
