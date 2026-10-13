"""Embeddings as the stores carry them, and the one similarity everything uses.

Shoal's vector search scores a cell whose value is the embedding packed as
big-endian float32, and hands back that cell with the value replaced by a
big-endian float32 score. The sqlite twin stores the same bytes and scores
them here, so the two stores answer `nearest` identically — which is what the
parity suite checks.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence

# The column family shoal's own tooling (shoal-ivf-train, agentmem) treats as
# "this cell is an embedding". Using the same one means a Foreman table can be
# indexed by shoal's trainer without a schema conversation.
EMBEDDING_CF = "vec:"
EMBEDDING_CQ = "embedding"
# The identity shoal requires of a raw-vector query: provider / model /
# version / dimension / normalization. A vector belongs to exactly one space,
# and shoal refuses to compare a query against files that do not declare the
# same one — so the vectors table is created declaring this, and every vector
# written to it must come from this model. Change the model, change this.
EMBEDDING_SPACE = "ollama/nomic-embed-text/v1.5/768/none"


def pack(vector: Sequence[float]) -> bytes:
    return struct.pack(f">{len(vector)}f", *vector)


def unpack(raw: bytes) -> list[float]:
    n = len(raw) // 4
    return list(struct.unpack(f">{n}f", raw[: n * 4]))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if not na or not nb:
        return 0.0
    return dot / (na * nb)
