import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np


def resolve_gemini_api_key() -> str:
    key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if key:
        return key
    key_file = (os.getenv("GEMINI_API_KEY_FILE") or "").strip()
    if key_file:
        p = Path(key_file).expanduser()
        if p.exists():
            return p.read_text(errors="ignore").strip()
    return ""


MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


@dataclass
class Record:
    record_id: str
    kind: str
    content: str
    path: str
    raw_bytes: Optional[bytes] = field(default=None, repr=False)
    mime_type: Optional[str] = None


class Embedder:
    def __init__(self, offline: bool = False):
        self.offline = offline
        self.embedding_model = os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-2-preview")
        self.client = None

        api_key = resolve_gemini_api_key()
        if not self.offline and api_key:
            from google import genai

            self.client = genai.Client(api_key=api_key)
        elif not self.offline and not api_key:
            raise RuntimeError("GEMINI_API_KEY is required unless offline mode is used")

    @staticmethod
    def _offline_vector(text: str, dim: int = 256) -> np.ndarray:
        vec = np.zeros(dim, dtype=np.float32)
        tokens = [t.strip(".,?!:;()[]{}\"'").lower() for t in text.split()]
        for tok in tokens:
            if not tok:
                continue
            h = int(hashlib.sha256(tok.encode("utf-8")).hexdigest()[:8], 16)
            vec[h % dim] += 1.0
        norm = np.linalg.norm(vec)
        return vec / norm if norm else vec

    def _embed_online(self, contents: Any, task_type: str) -> np.ndarray:
        from google.genai import types

        response = self.client.models.embed_content(
            model=self.embedding_model,
            contents=contents,
            config=types.EmbedContentConfig(task_type=task_type),
        )
        vec = response.embeddings[0].values
        arr = np.array(vec, dtype=np.float32)
        norm = np.linalg.norm(arr)
        return arr / norm if norm else arr

    def embed(self, record: "Record", task_type: str = "RETRIEVAL_DOCUMENT") -> np.ndarray:
        if self.offline:
            return self._offline_vector(record.content)

        from google.genai import types

        if record.kind == "text":
            contents = record.content
        else:
            contents = types.Part.from_bytes(data=record.raw_bytes, mime_type=record.mime_type)

        return self._embed_online(contents=contents, task_type=task_type)

    def embed_query(self, query: str) -> np.ndarray:
        if self.offline:
            return self._offline_vector(query)
        return self._embed_online(contents=query, task_type="RETRIEVAL_QUERY")

    def embed_query_image(self, image_bytes: bytes, mime_type: str = "image/png") -> np.ndarray:
        if self.offline:
            raise RuntimeError("Image query embedding is unavailable in offline mode")

        from google.genai import types

        contents = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
        return self._embed_online(contents=contents, task_type="RETRIEVAL_QUERY")


def ingest_records(base_dir: Path) -> list[Record]:
    records: list[Record] = []

    text_dir = base_dir / "data" / "text"
    for path in sorted(text_dir.glob("*.txt")):
        text = path.read_text(errors="ignore").strip()
        records.append(Record(record_id=f"txt:{path.name}", kind="text", content=text, path=str(path)))

    image_dir = base_dir / "data" / "images"
    for path in sorted(image_dir.glob("*")):
        suffix = path.suffix.lower()
        mime = MIME_MAP.get(suffix)
        if not mime:
            continue
        raw = path.read_bytes()
        label = path.stem.replace("_", " ")
        records.append(
            Record(
                record_id=f"img:{path.name}",
                kind="image",
                content=f"[image: {label}]",
                path=str(path),
                raw_bytes=raw,
                mime_type=mime,
            )
        )

    return records


class LanceBackend:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.db_dir = base_dir / "output" / "lancedb"
        self.table_name = "multimodal_embeddings"
        self.meta_file = self.db_dir / "index_meta.json"
        self.available, self.error = self._check_available()

    @staticmethod
    def _check_available() -> tuple[bool, str]:
        try:
            import lancedb  # noqa: F401

            return True, ""
        except Exception as exc:
            return False, str(exc)

    def _source_state(self, records: list[Record]) -> dict[str, Any]:
        key = []
        for r in records:
            p = Path(r.path)
            try:
                stat = p.stat()
                key.append(f"{r.record_id}|{stat.st_mtime_ns}|{stat.st_size}")
            except FileNotFoundError:
                key.append(f"{r.record_id}|missing")
        joined = "\n".join(sorted(key))
        fingerprint = hashlib.sha256(joined.encode("utf-8")).hexdigest()
        return {
            "fingerprint": fingerprint,
            "record_count": len(records),
        }

    def _connect(self):
        import lancedb

        self.db_dir.mkdir(parents=True, exist_ok=True)
        return lancedb.connect(str(self.db_dir))

    def _table(self):
        db = self._connect()
        return db.open_table(self.table_name)

    def sync(self, records: list[Record], embedder: Embedder) -> dict[str, Any]:
        if not self.available:
            raise RuntimeError(f"LanceDB unavailable: {self.error}")

        state = self._source_state(records)
        previous = self.read_meta()
        if previous.get("fingerprint") == state["fingerprint"] and previous.get("record_count") == state["record_count"]:
            return {
                "changed": False,
                "synced": previous.get("last_sync_utc", ""),
                "record_count": state["record_count"],
            }

        rows = []
        dim = 0
        for r in records:
            vec = embedder.embed(r)
            dim = int(vec.shape[0])
            rows.append(
                {
                    "record_id": r.record_id,
                    "kind": r.kind,
                    "content": r.content,
                    "path": r.path,
                    "mime_type": r.mime_type or "",
                    "vector": vec.astype(np.float32).tolist(),
                }
            )

        db = self._connect()
        try:
            db.drop_table(self.table_name)
        except Exception:
            pass
        db.create_table(self.table_name, data=rows)

        now_utc = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        meta = {
            "backend": "lancedb",
            "table": self.table_name,
            "db_dir": str(self.db_dir),
            "last_sync_utc": now_utc,
            "vector_dim": dim,
            "fingerprint": state["fingerprint"],
            "record_count": state["record_count"],
        }
        self.meta_file.write_text(json.dumps(meta, indent=2))

        return {
            "changed": True,
            "synced": now_utc,
            "record_count": state["record_count"],
            "vector_dim": dim,
        }

    def read_meta(self) -> dict[str, Any]:
        if not self.meta_file.exists():
            return {}
        try:
            data = json.loads(self.meta_file.read_text(errors="ignore") or "{}")
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def status(self) -> dict[str, Any]:
        if not self.available:
            return {
                "backend": "fallback",
                "available": False,
                "error": self.error,
                "db_dir": str(self.db_dir),
                "table": self.table_name,
                "table_count": 0,
                "last_sync_utc": "",
                "vector_dim": 0,
            }

        meta = self.read_meta()
        count = 0
        try:
            table = self._table()
            count = table.count_rows()
        except Exception:
            count = 0

        return {
            "backend": "lancedb",
            "available": True,
            "error": "",
            "db_dir": str(self.db_dir),
            "table": self.table_name,
            "table_count": int(count),
            "last_sync_utc": str(meta.get("last_sync_utc") or ""),
            "vector_dim": int(meta.get("vector_dim") or 0),
        }

    def retrieve(self, query_vec: np.ndarray, k: int = 5) -> list[tuple[Record, float]]:
        if not self.available:
            raise RuntimeError("LanceDB backend not available")
        table = self._table()
        rows = table.search(query_vec.astype(np.float32).tolist()).limit(k).to_list()
        out: list[tuple[Record, float]] = []
        for row in rows:
            rec = Record(
                record_id=str(row.get("record_id") or ""),
                kind=str(row.get("kind") or "text"),
                content=str(row.get("content") or ""),
                path=str(row.get("path") or ""),
                mime_type=str(row.get("mime_type") or "") or None,
            )
            distance = float(row.get("_distance") or 0.0)
            score = 1.0 / (1.0 + max(0.0, distance))
            out.append((rec, score))
        return out

    def get_record(self, record_id: str) -> Optional[Record]:
        if not self.available:
            return None
        table = self._table()
        rows = table.search().where(f"record_id = '{record_id}'").limit(1).to_list()
        if not rows:
            return None
        row = rows[0]
        return Record(
            record_id=str(row.get("record_id") or ""),
            kind=str(row.get("kind") or "text"),
            content=str(row.get("content") or ""),
            path=str(row.get("path") or ""),
            mime_type=str(row.get("mime_type") or "") or None,
        )


class MultimodalRAGService:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.backend = LanceBackend(base_dir)

    @property
    def api_key_present(self) -> bool:
        return bool(resolve_gemini_api_key())

    @property
    def offline_mode(self) -> bool:
        return not self.api_key_present

    def _dataset_counts(self) -> tuple[int, int]:
        text_dir = self.base_dir / "data" / "text"
        image_dir = self.base_dir / "data" / "images"
        text_count = len(list(text_dir.glob("*.txt"))) if text_dir.exists() else 0
        image_count = len([p for p in image_dir.glob("*") if p.suffix.lower() in MIME_MAP]) if image_dir.exists() else 0
        return text_count, image_count

    def sync_index(self, force: bool = False) -> dict[str, Any]:
        records = ingest_records(self.base_dir)
        if not records:
            raise RuntimeError("No retrievable records found yet. Add files under data/text/*.txt and/or data/images/* first.")
        if not self.backend.available:
            raise RuntimeError(f"LanceDB unavailable: {self.backend.error}")
        embedder = Embedder(offline=self.offline_mode)
        if force:
            if self.backend.meta_file.exists():
                self.backend.meta_file.unlink()
        return self.backend.sync(records, embedder)

    def ensure_index(self) -> dict[str, Any]:
        if not self.backend.available:
            return {"ok": False, "backend": "fallback", "error": self.backend.error}
        embedder = Embedder(offline=self.offline_mode)
        records = ingest_records(self.base_dir)
        if not records:
            raise RuntimeError("No retrievable records found yet. Add files under data/text/*.txt and/or data/images/* first.")
        info = self.backend.sync(records, embedder)
        return {"ok": True, "backend": "lancedb", **info}

    def retrieve_text(self, query: str, k: int = 5) -> list[tuple[Record, float]]:
        if not self.backend.available:
            raise RuntimeError(f"LanceDB unavailable: {self.backend.error}")
        self.ensure_index()
        embedder = Embedder(offline=self.offline_mode)
        q = embedder.embed_query(query)
        return self.backend.retrieve(q, k=k)

    def retrieve_image(self, image_bytes: bytes, mime_type: str = "image/png", k: int = 5) -> list[tuple[Record, float]]:
        if not self.backend.available:
            raise RuntimeError(f"LanceDB unavailable: {self.backend.error}")
        self.ensure_index()
        embedder = Embedder(offline=self.offline_mode)
        q = embedder.embed_query_image(image_bytes=image_bytes, mime_type=mime_type)
        return self.backend.retrieve(q, k=k)

    def get_record(self, record_id: str) -> Optional[Record]:
        if self.backend.available:
            return self.backend.get_record(record_id)
        records = ingest_records(self.base_dir)
        return next((r for r in records if r.record_id == record_id), None)

    def status(self) -> dict[str, Any]:
        text_count, image_count = self._dataset_counts()
        ready = (text_count + image_count) > 0
        backend = self.backend.status()

        return {
            "api_key_present": self.api_key_present,
            "offline_mode": self.offline_mode,
            "index_ready": ready,
            "text_count": text_count,
            "image_count": image_count,
            "base_dir": str(self.base_dir),
            "backend_mode": backend.get("backend", "fallback"),
            "backend_available": backend.get("available", False),
            "backend_error": backend.get("error", ""),
            "lancedb_path": backend.get("db_dir", ""),
            "lancedb_table": backend.get("table", ""),
            "lancedb_count": backend.get("table_count", 0),
            "last_index_sync": backend.get("last_sync_utc", ""),
            "vector_dim": backend.get("vector_dim", 0),
        }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Multimodal RAG index utility")
    parser.add_argument("command", choices=["status", "sync"], help="Operation")
    parser.add_argument("--base-dir", default=os.getenv("MULTIMODAL_RAG_BASE_DIR", "/Users/fox/.openclaw/workspace/gemini-multimodal-rag-lab"))
    parser.add_argument("--force", action="store_true", help="Force full rebuild")
    args = parser.parse_args()

    service = MultimodalRAGService(Path(args.base_dir))
    if args.command == "status":
        print(json.dumps(service.status(), indent=2))
    else:
        result = service.sync_index(force=args.force)
        print(json.dumps({"ok": True, "backend": "lancedb", **result}, indent=2))
