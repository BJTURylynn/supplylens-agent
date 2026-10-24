"""Small, auditable SOP retrieval; local mode is lexical, not a semantic model.

Both modes require a lexical anchor before ranking. This precision-first rule
deliberately misses synonyms with no shared terms instead of inventing citations.
Cloud embedding failures are explicit and never switch to local embeddings.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
import unicodedata
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import numpy as np

CHUNK_SIZE = 500
CHUNK_OVERLAP = 100
INDEX_VERSION = "sop-v1-char234-heading500-overlap100"
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_WORDS = re.compile(r"[\u3400-\u9fff]+|[a-z0-9]+")
_COMMON = {"什么", "怎么", "如何", "是否", "可以", "进行", "一个", "我们", "这些", "以及"}


class EmbeddingError(RuntimeError):
    """The configured cloud embedding service could not provide valid vectors."""


def _terms(text: str) -> Counter[str]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    terms: Counter[str] = Counter()
    for word in _WORDS.findall(normalized):
        if re.fullmatch(r"[a-z0-9]+", word):
            if len(word) > 1:
                terms["w:" + word] += 1
            continue
        for width in (2, 3, 4):
            for start in range(len(word) - width + 1):
                token = word[start : start + width]
                if token not in _COMMON:
                    terms["c:" + token] += 1
    return terms


def _normalize(vectors: np.ndarray) -> np.ndarray:
    vectors = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(vectors.astype(np.float64), axis=1, keepdims=True)
    return np.divide(vectors, norms, out=np.zeros_like(vectors), where=norms > 0)


class SOPIndex:
    """Thread-safe index with stable citations and atomic, non-pickle caches.

    ``build`` checks source content, embedding settings, and cache integrity.
    ``search`` returns up to ``top_k`` chunks, never forced unrelated results.
    """

    def __init__(self, settings: Any):
        self.settings = settings
        self.mode = settings.embedding_mode
        if self.mode not in {"local", "cloud"}:
            raise ValueError("embedding_mode must be local or cloud")
        self._dimensions = int(settings.embedding_dimensions)
        if self._dimensions <= 0:
            raise ValueError("embedding_dimensions must be positive")
        self._lock = threading.RLock()
        self._fingerprint: str | None = None
        self._chunks: list[dict] = []
        self._vectors = np.empty((0, self._dimensions), dtype=np.float32)
        self._term_sets: list[set[str]] = []

    def _sources(self) -> list[tuple[str, bytes]]:
        root = Path(self.settings.sop_dir)
        sources = [(path.relative_to(root).as_posix(), path.read_bytes()) for path in sorted(root.rglob("*.md"))]
        if not sources:
            raise ValueError(f"No Markdown SOP documents found in {root}")
        return sources

    def _source_fingerprint(self, sources: list[tuple[str, bytes]]) -> str:
        config = {
            "version": INDEX_VERSION,
            "mode": self.mode,
            "model": self.settings.embedding_model,
            "endpoint": self.settings.model_base_url.rstrip("/"),
            "dimensions": self._dimensions,
            "sources": [(name, hashlib.sha256(content).hexdigest()) for name, content in sources],
        }
        return hashlib.sha256(json.dumps(config, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    @staticmethod
    def _split_document(document: str, content: bytes) -> list[dict]:
        text = content.decode("utf-8-sig").replace("\r\n", "\n")
        sections: list[tuple[str, str]] = []
        headings: list[tuple[int, str]] = []
        current_title = Path(document).stem
        lines: list[str] = []
        for line in text.splitlines():
            match = _HEADING.match(line)
            if match:
                if "\n".join(lines).strip():
                    sections.append((current_title, "\n".join(lines).strip()))
                level, title = len(match[1]), match[2].strip()
                headings = [(old_level, old_title) for old_level, old_title in headings if old_level < level]
                headings.append((level, title))
                current_title = " / ".join(item[1] for item in headings)
                lines = [line]
            else:
                lines.append(line)
        if "\n".join(lines).strip():
            sections.append((current_title, "\n".join(lines).strip()))

        chunks: list[dict] = []
        occurrences: Counter[str] = Counter()
        for title, body in sections:
            occurrences[title] += 1
            # Provenance notices stay in the source documents but are not useful
            # evidence for operational questions; do not rank standalone notices.
            substantive = [
                line for line in body.splitlines()
                if line.strip() and not _HEADING.match(line)
                and not line.strip().startswith(("来源声明", "自编模拟"))
            ]
            if not substantive:
                continue
            identity = f"{document}\0{title}\0{occurrences[title]}"
            section_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
            start = 0
            number = 0
            while start < len(body):
                end = min(start + CHUNK_SIZE, len(body))
                chunk_text = body[start:end]
                if _terms(chunk_text):
                    chunks.append({
                        "id": f"sop:{section_id}:{number:03d}",
                        "document": document,
                        "title": title,
                        "text": chunk_text,
                        "source_type": "sop",
                    })
                if end == len(body):
                    break
                start = end - CHUNK_OVERLAP
                number += 1
        return chunks

    def _local_embeddings(self, texts: list[str]) -> np.ndarray:
        vectors = np.zeros((len(texts), self._dimensions), dtype=np.float32)
        for row, text in enumerate(texts):
            for term, count in _terms(text).items():
                digest = hashlib.blake2b(term.encode("utf-8"), digest_size=8).digest()
                column = int.from_bytes(digest, "big") % self._dimensions
                vectors[row, column] += 1.0 + math.log(count)
        return _normalize(vectors)

    def _cloud_embeddings(self, texts: list[str]) -> np.ndarray:
        if not self.settings.model_api_key:
            raise EmbeddingError("Cloud embeddings require MODEL_API_KEY; no local fallback is performed")
        url = self.settings.model_base_url.rstrip("/") + "/embeddings"
        batches: list[np.ndarray] = []
        try:
            with httpx.Client(timeout=self.settings.request_timeout_seconds) as client:
                # A conservative batch size also supports DashScope text-embedding-v4.
                for start in range(0, len(texts), 10):
                    batch = texts[start : start + 10]
                    response = client.post(
                        url,
                        headers={"Authorization": f"Bearer {self.settings.model_api_key}"},
                        json={
                            "model": self.settings.embedding_model,
                            "input": batch,
                            "dimensions": self._dimensions,
                            "encoding_format": "float",
                        },
                    )
                    if response.status_code >= 400:
                        raise EmbeddingError(f"Cloud embeddings failed (HTTP {response.status_code}); no local fallback")
                    payload = response.json()
                    data = payload["data"]
                    if not isinstance(data, list) or len(data) != len(batch):
                        raise ValueError("embedding response count mismatch")
                    # Never rely on service response order for the citation/vector mapping.
                    if any(type(item["index"]) is not int for item in data):
                        raise ValueError("embedding response index must be an integer")
                    if {item["index"] for item in data} != set(range(len(batch))):
                        raise ValueError("embedding response indices are missing or duplicated")
                    data = sorted(data, key=lambda item: item["index"])
                    matrix = np.asarray([item["embedding"] for item in data], dtype=np.float32)
                    if matrix.shape != (len(batch), self._dimensions) or not np.isfinite(matrix).all():
                        raise ValueError("invalid embedding shape or non-finite values")
                    if (np.linalg.norm(matrix.astype(np.float64), axis=1) == 0).any():
                        raise ValueError("cloud returned an empty vector")
                    batches.append(matrix)
        except EmbeddingError:
            raise
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"Cloud embeddings transport failure ({type(exc).__name__}); no local fallback") from None
        except (KeyError, TypeError, ValueError, OverflowError):
            raise EmbeddingError("Cloud embeddings returned an invalid response; no local fallback") from None
        return _normalize(np.concatenate(batches, axis=0))

    def _embed(self, texts: list[str]) -> np.ndarray:
        return self._local_embeddings(texts) if self.mode == "local" else self._cloud_embeddings(texts)

    def _adopt(self, chunks: list[dict], vectors: np.ndarray, fingerprint: str) -> None:
        self._chunks = chunks
        self._vectors = vectors
        self._term_sets = [set(_terms(chunk["title"] + " " + chunk["text"])) for chunk in chunks]
        self._fingerprint = fingerprint

    def _load_cache(self, fingerprint: str, expected_chunks: list[dict]) -> bool:
        root = Path(self.settings.index_dir)
        try:
            metadata = json.loads((root / "index.json").read_text(encoding="utf-8"))
            if metadata["fingerprint"] != fingerprint or metadata["chunks"] != expected_chunks:
                return False
            filename = metadata["vectors_file"]
            if not isinstance(filename, str) or not re.fullmatch(r"vectors\.[0-9a-f]{32}\.npy", filename):
                return False
            matrix_path = root / filename
            if hashlib.sha256(matrix_path.read_bytes()).hexdigest() != metadata["vectors_sha256"]:
                return False
            vectors = np.load(matrix_path, allow_pickle=False)
            if vectors.dtype != np.float32 or vectors.shape != (len(expected_chunks), self._dimensions):
                return False
            if not np.isfinite(vectors).all():
                return False
            norms = np.linalg.norm(vectors, axis=1)
            if not np.all(np.isclose(norms, 1, atol=1e-5) | np.isclose(norms, 0, atol=1e-8)):
                return False
            self._adopt(expected_chunks, vectors, fingerprint)
            return True
        except (OSError, ValueError, KeyError, TypeError, AttributeError, EOFError):
            return False

    def _save_cache(self, chunks: list[dict], vectors: np.ndarray, fingerprint: str) -> None:
        root = Path(self.settings.index_dir)
        root.mkdir(parents=True, exist_ok=True)
        filename = f"vectors.{uuid.uuid4().hex}.npy"
        temporary: list[Path] = []
        try:
            with tempfile.NamedTemporaryFile(dir=root, suffix=".npy.tmp", delete=False) as handle:
                temporary.append(Path(handle.name))
                np.save(handle, vectors, allow_pickle=False)
                handle.flush()
                os.fsync(handle.fileno())
            vector_path = root / filename
            os.replace(temporary[-1], vector_path)
            temporary.pop()
            metadata = {
                "version": INDEX_VERSION,
                "fingerprint": fingerprint,
                "mode": self.mode,
                "model": self.settings.embedding_model,
                "dimensions": self._dimensions,
                "vectors_file": filename,
                "vectors_sha256": hashlib.sha256(vector_path.read_bytes()).hexdigest(),
                "chunks": chunks,
            }
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, suffix=".json.tmp", delete=False) as handle:
                temporary.append(Path(handle.name))
                json.dump(metadata, handle, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            # Publishing one pointer last means readers see a complete generation.
            # Old generations are intentionally retained to avoid racing other readers.
            os.replace(temporary[-1], root / "index.json")
            temporary.pop()
        finally:
            for path in temporary:
                path.unlink(missing_ok=True)

    def build(self) -> dict:
        with self._lock:
            if self.mode == "cloud" and not self.settings.model_api_key:
                raise EmbeddingError("Cloud embeddings require MODEL_API_KEY; no local fallback is performed")
            sources = self._sources()
            fingerprint = self._source_fingerprint(sources)
            cache_hit = self._fingerprint == fingerprint
            if not cache_hit:
                chunks = [chunk for name, content in sources for chunk in self._split_document(name, content)]
                if not chunks:
                    raise ValueError("SOP documents contain no indexable text")
                cache_hit = self._load_cache(fingerprint, chunks)
                if not cache_hit:
                    vectors = self._embed([chunk["title"] + " " + chunk["text"] for chunk in chunks])
                    self._save_cache(chunks, vectors, fingerprint)
                    self._adopt(chunks, vectors, fingerprint)
            return {
                "mode": self.mode,
                "documents": len(sources),
                "chunks": len(self._chunks),
                "cache_hit": cache_hit,
                "fingerprint": fingerprint,
                "embedding_model": self.settings.embedding_model if self.mode == "cloud" else "lexical-char-ngram-hash",
                "requires_lexical_anchor": True,
            }

    def search(self, query: str, top_k: int = 3) -> list[dict]:
        if not isinstance(query, str) or not query.strip() or not _terms(query):
            return []
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
            return []
        with self._lock:
            self.build()
            query_terms = set(_terms(query))
            candidates = [row for row, terms in enumerate(self._term_sets) if query_terms.intersection(terms)]
            if not candidates:
                return []
            query_vector = self._embed([query])[0]
            scores = self._vectors[candidates] @ query_vector
            ranked = sorted(zip(candidates, scores, strict=True), key=lambda item: (-float(item[1]), self._chunks[item[0]]["id"]))
            return [
                {**self._chunks[row], "score": round(float(score), 6)}
                for row, score in ranked[:top_k]
                if float(score) > 1e-6
            ]
