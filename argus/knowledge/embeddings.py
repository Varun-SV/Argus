"""Embedding generation with lazy-loaded sentence-transformers."""

from __future__ import annotations

from typing import List, Optional


class EmbeddingGenerator:
    """Lazy-loads sentence-transformers on first use; gracefully no-ops if missing."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        self._model_name = model_name
        self._model = None
        self._available: Optional[bool] = None

    def _load(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self._model_name)
            self._available = True
        except ImportError:
            self._available = False
        return self._available

    @property
    def available(self) -> bool:
        return self._load()

    def embed(self, text: str) -> Optional[List[float]]:
        if not self._load():
            return None
        result = self._model.encode([text], show_progress_bar=False)
        return result[0].tolist()

    def embed_batch(self, texts: List[str]) -> Optional[List[List[float]]]:
        if not self._load():
            return None
        results = self._model.encode(texts, show_progress_bar=False)
        return [r.tolist() for r in results]
