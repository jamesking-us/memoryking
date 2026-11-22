#!/usr/bin/env python3
"""A fake reader and judge for testing track T5 offline. It calls no model.

fake_model.py reader   prompt on stdin; answers with the first sentence of the retrieved
                       material that has a digit or a capitalised word (or `I don't know.`)
fake_model.py judge    prompt on stdin; `yes` when the reference answer's words all occur in
                       the answer to grade, else `no`
"""

from __future__ import annotations

import re
import sys


def main() -> int:
    kind = sys.argv[1]
    prompt = sys.stdin.read()
    if kind == "reader":
        material = prompt.split("Retrieved material:", 1)[-1].split("The question was asked on:")[0]
        sentences = re.split(r"(?<=[.!?])\s+", " ".join(material.split()))
        for s in sentences:
            if re.search(r"\d", s) and len(s) > 20:
                print(s[:300])
                return 0
        print("I don't know.")
        return 0
    reference = (
        prompt.split("Reference answer:", 1)[-1].split("Answer to grade:")[0].strip().lower()
    )
    answer = prompt.split("Answer to grade:", 1)[-1].split("Reply with exactly")[0].strip().lower()
    words = re.findall(r"[a-z0-9]+", reference)
    print("yes" if words and all(w in answer for w in words) else "no")
    return 0


if __name__ == "__main__":
    sys.exit(main())
