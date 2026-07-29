"""Chunk documents and retrieve the passages relevant to a question.

This module is what keeps the project inside free-tier limits. A fetched page can be 50k+
tokens; sending that to an LLM is both impossible and wasteful. Instead: chunk, embed
locally (zero cost, no rate limit), and pass only the top-k passages to the model.

Embeddings run through fastembed (ONNX, no torch). If the model cannot be downloaded, a
pure-numpy TF-IDF ranker takes over — degraded relevance, but the pipeline still runs.
"""

from __future__ import annotations

import re
import threading

import numpy as np

from .. import config

_MODEL = None
_MODEL_LOCK = threading.Lock()
_MODEL_FAILED = False

# fastembed's ONNX session and its Rust tokenizer are NOT safe to call from several threads
# at once — concurrent embed() calls corrupt the heap ("double free or corruption") and take
# the whole process down. Inference is only a few milliseconds, so serialising it costs
# almost nothing next to the network I/O the reader threads exist to overlap.
_INFERENCE_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #
def chunk_text(
    text: str, size: int = config.CHUNK_CHARS, overlap: int = config.CHUNK_OVERLAP
) -> list[str]:
    """Split text into overlapping chunks, preferring paragraph then sentence boundaries.

    Overlap matters: a fact split across a chunk edge is a fact the retriever cannot find.
    """
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    if not text:
        return []
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))

        if end < len(text):
            window = text[start:end]
            # Prefer a paragraph break in the last third, else a sentence end.
            split_at = window.rfind("\n\n")
            if split_at < size // 3:
                match = None
                for match in re.finditer(r"[.!?]\s", window):
                    pass
                split_at = match.end() if match and match.end() > size // 3 else -1
            if split_at > 0:
                end = start + split_at

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        if end >= len(text):
            break
        start = max(end - overlap, start + 1)

    return chunks


# --------------------------------------------------------------------------- #
# Embedding backend
# --------------------------------------------------------------------------- #
def _get_model():
    """Lazy-load the ONNX embedding model. Returns ``None`` if unavailable."""
    global _MODEL, _MODEL_FAILED
    if _MODEL is not None or _MODEL_FAILED:
        return _MODEL

    with _MODEL_LOCK:
        if _MODEL is not None or _MODEL_FAILED:
            return _MODEL
        try:
            from fastembed import TextEmbedding

            _MODEL = TextEmbedding(
                model_name=config.EMBED_MODEL,
                cache_dir=str(config.CACHE_DIR / "embeddings"),
            )
        except Exception:  # noqa: BLE001 - no network, no disk, no problem: fall back
            _MODEL_FAILED = True
            _MODEL = None
        return _MODEL


def _embed(texts: list[str]) -> np.ndarray | None:
    model = _get_model()
    if model is None:
        return None
    try:
        with _INFERENCE_LOCK:
            vectors = np.array(list(model.embed(texts)), dtype=np.float32)
    except Exception:  # noqa: BLE001
        return None
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.clip(norms, 1e-9, None)


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tfidf_matrix(docs: list[str]) -> np.ndarray:
    """Minimal TF-IDF with L2 normalisation. Fallback path only."""
    tokenized = [_TOKEN_RE.findall(d.lower()) for d in docs]
    vocab: dict[str, int] = {}
    for tokens in tokenized:
        for token in tokens:
            vocab.setdefault(token, len(vocab))

    if not vocab:
        return np.zeros((len(docs), 1), dtype=np.float32)

    tf = np.zeros((len(docs), len(vocab)), dtype=np.float32)
    for row, tokens in enumerate(tokenized):
        for token in tokens:
            tf[row, vocab[token]] += 1.0

    df = (tf > 0).sum(axis=0)
    idf = np.log((1 + len(docs)) / (1 + df)) + 1.0
    matrix = tf * idf
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.clip(norms, 1e-9, None)


def using_embeddings() -> bool:
    """True when the ONNX model is live; False when running on the TF-IDF fallback."""
    return _get_model() is not None


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #
def top_k(
    query: str, chunks: list[str], k: int = config.TOP_K_PASSAGES
) -> list[tuple[str, float]]:
    """Return the ``k`` chunks most relevant to ``query``, as ``(chunk, score)`` pairs."""
    if not chunks:
        return []
    if len(chunks) <= k:
        return [(c, 1.0) for c in chunks]

    embedded = _embed([query, *chunks])
    if embedded is not None:
        scores = embedded[1:] @ embedded[0]
    else:
        matrix = _tfidf_matrix([query, *chunks])
        scores = matrix[1:] @ matrix[0]

    best = np.argsort(-scores)[:k]
    return [(chunks[i], float(scores[i])) for i in best]
