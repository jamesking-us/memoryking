"""Optional pinned tokenizer using a vendored, hash-verified vocabulary. No network."""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Callable

from memoryking.domain.local import LocalError

ESTIMATE = "chars-estimate-v1"
LEGACY_ESTIMATE = "utf8-byte-estimate-v1"
CHARS_PER_TOKEN = 3
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


def estimate(text: str) -> int:
    """Tokens without a tokenizer: one per three ASCII characters, one per other character.

    Real BPE tokenizers average about four characters per token on English prose and three
    to three and a half on code and identifiers, so three is conservative for both: a
    budget counted this way is not exceeded by the real count. Every non-ASCII character is
    counted as a whole token (accented text and CJK often cost one or more each). It
    overstates tokens for prose by roughly a quarter, which is the safe direction for a
    budget. `utf8-byte-estimate-v1` (one token per byte) remains selectable and is far
    more conservative.
    """
    ascii_chars = sum(1 for ch in text if ch < "\x80")
    return -(-ascii_chars // CHARS_PER_TOKEN) + (len(text) - ascii_chars)


def counter(name: str) -> tuple[Callable[[str], int], dict[str, Any]]:
    if name == "cl100k_base":
        enc = encoding()
        return lambda text: len(enc.encode_ordinary(text)), {
            "counting": "exact",
            "tokenizer": name,
            "revision": "tiktoken-0.12.0:" + VOCAB_HASH,
        }
    if name == ESTIMATE:
        return estimate, {
            "counting": "estimate",
            "tokenizer": name,
            "revision": f"ascii-{CHARS_PER_TOKEN}-chars-per-token-v1",
        }
    return lambda text: len(text.encode()), {
        "counting": "estimate",
        "tokenizer": name,
        "revision": "utf8-byte-estimate-v1",
    }
