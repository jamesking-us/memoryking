"""Optional pinned tokenizer using a vendored, hash-verified vocabulary. No network."""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Callable

from memoryking.domain.local import LocalError

VOCAB_HASH = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"


@lru_cache(maxsize=1)
def encoding() -> Any:
    try:
        if version("tiktoken") != "0.12.0":
            raise LocalError("tokenizer_version_unsupported")
        import tiktoken
    except (ImportError, PackageNotFoundError) as exc:
        raise LocalError("tokenizer_unavailable") from exc
    raw = Path(__file__).with_name("tokenizers").joinpath("cl100k_base.tiktoken").read_bytes()
    if hashlib.sha256(raw).hexdigest() != VOCAB_HASH:
        raise LocalError("tokenizer_integrity_failure")
    ranks = {base64.b64decode(t): int(n) for t, n in (line.split() for line in raw.splitlines())}
    return tiktoken.Encoding(
        name="cl100k_base",
        pat_str=r"'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+| ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s",
        mergeable_ranks=ranks,
        special_tokens={
            "<|endoftext|>": 100257,
            "<|fim_prefix|>": 100258,
            "<|fim_middle|>": 100259,
            "<|fim_suffix|>": 100260,
            "<|endofprompt|>": 100276,
        },
    )


def counter(name: str) -> tuple[Callable[[str], int], dict[str, Any]]:
    if name == "cl100k_base":
        enc = encoding()
        return lambda text: len(enc.encode_ordinary(text)), {
            "counting": "exact",
            "tokenizer": name,
            "revision": "tiktoken-0.12.0:" + VOCAB_HASH,
        }
    return lambda text: len(text.encode()), {
        "counting": "estimate",
        "tokenizer": name,
        "revision": "utf8-byte-estimate-v1",
    }
