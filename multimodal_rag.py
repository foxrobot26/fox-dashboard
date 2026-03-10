import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np


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

        api_key = os.getenv("GEMINI_API_KEY")
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


def build_index(records: Sequence[Record], embedder: Embedder) -> dict[str, np.ndarray]:
    return {r.record_id: embedder.embed(r) for r in records}


def retrieve_by_vector(
    query_vec: np.ndarray,
    records: Sequence[Record],
    index: dict[str, np.ndarray],
    k: int = 5,
) -> list[tuple[Record, float]]:
    scored: list[tuple[Record, float]] = []
    for r in records:
        sim = float(np.dot(query_vec, index[r.record_id]))
        scored.append((r, sim))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:k]


def retrieve_text(
    query: str,
    records: Sequence[Record],
    index: dict[str, np.ndarray],
    embedder: Embedder,
    k: int = 5,
) -> list[tuple[Record, float]]:
    q = embedder.embed_query(query)
    return retrieve_by_vector(q, records, index, k=k)


class MultimodalRAGService:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir

    @property
    def api_key_present(self) -> bool:
        return bool(os.getenv("GEMINI_API_KEY"))

    @property
    def offline_mode(self) -> bool:
        return not self.api_key_present

    def load_runtime(self) -> tuple[Embedder, list[Record], dict[str, np.ndarray]]:
        embedder = Embedder(offline=self.offline_mode)
        records = ingest_records(self.base_dir)
        if not records:
            raise RuntimeError(
                "No retrievable records found yet. Add files under data/text/*.txt and/or data/images/* first."
            )
        index = build_index(records, embedder)
        return embedder, records, index

    def status(self) -> dict[str, Any]:
        text_dir = self.base_dir / "data" / "text"
        image_dir = self.base_dir / "data" / "images"
        text_count = len(list(text_dir.glob("*.txt"))) if text_dir.exists() else 0
        image_count = len([p for p in image_dir.glob("*") if p.suffix.lower() in MIME_MAP]) if image_dir.exists() else 0
        ready = (text_count + image_count) > 0

        return {
            "api_key_present": self.api_key_present,
            "offline_mode": self.offline_mode,
            "index_ready": ready,
            "text_count": text_count,
            "image_count": image_count,
            "base_dir": str(self.base_dir),
        }
