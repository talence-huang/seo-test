"""確定性雜湊工具。世界中的一切屬性都由 (種子, 識別字串…) 推導，不使用全域亂數狀態。"""

from __future__ import annotations

import hashlib
import math
import random
import re
from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")


def h64(*parts: object) -> int:
    data = "\x1f".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.blake2b(data, digest_size=8).digest(), "big")


def unit(*parts: object) -> float:
    """[0, 1) 之間的確定性數值。"""
    return h64(*parts) / 2**64


def rng(*parts: object) -> random.Random:
    return random.Random(h64(*parts))


def gauss(*parts: object) -> float:
    """標準常態分布的確定性樣本（Box–Muller）。"""
    u1 = max(unit(*parts, "g1"), 1e-12)
    u2 = unit(*parts, "g2")
    return math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2)


def pick(seq: Sequence[T], *parts: object) -> T:
    return seq[h64(*parts) % len(seq)]


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
