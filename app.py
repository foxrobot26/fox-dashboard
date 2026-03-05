import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

CHAT_ID = "-1003840141521"
THREAD_ID = "22"
HANDLE_SCRIPT = "/Users/fox/.openclaw/workspace/scripts/recs/handle_recs_reply.py"
DATA_FILES = [
    Path("/tmp/openclaw/video-recs-message.json"),
    Path("/tmp/openclaw/video-recs-latest.json"),
]


def create_app() -> Flask:
    password = os.getenv("DASHBOARD_PASSWORD")
    if not password:
        raise RuntimeError("DASHBOARD_PASSWORD must be set before starting the app")

    app = Flask(__name__)
    app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-me-in-production")
    app.config["DASHBOARD_PASSWORD"] = password

    def _hash_item(item: dict[str, Any]) -> str:
        base = f"{item.get('video_id', '')}|{item.get('url', '')}|{item.get('title', '')}|{item.get('reply_text', '')}"
        return hashlib.sha256(base.encode("utf-8")).hexdigest()[:12]

    def _normalize_item(raw: dict[str, Any], source: str) -> dict[str, Any]:
        title = (
            raw.get("title")
            or raw.get("video_title")
            or raw.get("name")
            or raw.get("text")
            or "Untitled recommendation"
        )
        url = raw.get("url") or raw.get("video_url") or raw.get("link") or ""
        video_id = raw.get("video_id") or raw.get("id") or raw.get("youtube_id") or ""

        if not url and video_id:
            url = f"https://www.youtube.com/watch?v={video_id}"

        description = raw.get("description") or raw.get("summary") or raw.get("reason") or ""
        reply_text = raw.get("reply_text")
        if not reply_text:
            parts = [p for p in [title, url, f"video_id:{video_id}" if video_id else ""] if p]
            reply_text = " | ".join(parts)

        item = {
            "source": source,
            "title": str(title),
            "url": str(url),
            "video_id": str(video_id),
            "description": str(description),
            "reply_text": str(reply_text),
            "raw": raw,
        }
        item["id"] = _hash_item(item)
        return item

    def _extract_items(obj: Any, source: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []

        if isinstance(obj, list):
            for entry in obj:
                items.extend(_extract_items(entry, source))
            return items

        if not isinstance(obj, dict):
            return items

        candidate_keys = ["videos", "recommendations", "items", "results", "data"]
        found_nested = False
        for key in candidate_keys:
            if key in obj and isinstance(obj[key], (list, dict)):
                found_nested = True
                items.extend(_extract_items(obj[key], source))

        has_video_shape = any(k in obj for k in ["url", "video_id", "video_url", "title", "video_title"])
        if has_video_shape or not found_nested:
            items.append(_normalize_item(obj, source))

        return items

    def load_recommendations() -> list[dict[str, Any]]:
        all_items: list[dict[str, Any]] = []
        seen: set[str] = set()

        for path in DATA_FILES:
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text())
                items = _extract_items(payload, str(path))
                for item in items:
                    key = item["id"]
                    if key in seen:
                        continue
                    seen.add(key)
                    all_items.append(item)
            except Exception:
                continue
        return all_items

    def _require_login():
        return session.get("authenticated") is True

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
        recommendations = load_recommendations()
        return render_template("dashboard.html", recommendations=recommendations)

    @app.route("/api/recommendations", methods=["GET"])
    def api_recommendations():
        if not _require_login():
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        recommendations = load_recommendations()
        return jsonify({"ok": True, "count": len(recommendations), "items": recommendations})

    @app.route("/api/recommendations/<item_id>/<action>", methods=["POST"])
    def take_action(item_id: str, action: str):
        if not _require_login():
            return jsonify({"ok": False, "error": "Unauthorized"}), 401

        action = action.lower().strip()
        if action not in {"approve", "reject"}:
            return jsonify({"ok": False, "error": "Action must be approve or reject"}), 400

        recommendations = load_recommendations()
        item = next((r for r in recommendations if r["id"] == item_id), None)
        if not item:
            return jsonify({"ok": False, "error": "Recommendation not found"}), 404

        cmd = [
            "python3",
            HANDLE_SCRIPT,
            "--chat-id",
            CHAT_ID,
            "--thread-id",
            THREAD_ID,
            "--text",
            action,
            "--reply-text",
            item["reply_text"],
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        ok = result.returncode == 0

        payload = {
            "ok": ok,
            "action": action,
            "item_id": item_id,
            "title": item.get("title"),
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "returncode": result.returncode,
        }
        status = 200 if ok else 500
        return jsonify(payload), status

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
