"""Which sentences of the user's own turns state a standing personal preference.

A stated rule, no model, and nothing but the text of the sentence: it is applied only to what
the user wrote (the host hooks never pass it assistant text, tool output or text a turn copied
from a tool). A sentence counts when all of these hold:

1. It says how the user wants work done: a first-person stance (`I want`, `I prefer`, `I like`,
   `I need`, `I expect`, `I hate`, `I don't want`, `my standing preference`, or `never ... for me`).
2. The user says it holds beyond this project, in their own words: `in all my projects`,
   `across my repos`, `whatever the project`, `regardless of the repo`, `everywhere I work`,
   `in any project I work on`. A generalizer alone (`always`, `never`, `by default`) is not
   enough, because `I always want the importer to log at debug level` is as likely a fact of one
   project as a habit of the person.
3. Nothing in it ties it to this project or this moment: no `this`, `here`, `we`, `our`, `let's`,
   `for now`, no path, file name, identifier, URL, port or code, and no named project.
4. It is a statement: not a question, not conditional or hypothetical, not about someone else, and
   short enough to be one rule (at most 300 characters).

When a sentence could be either a personal preference or a fact of the project, it is not
recognised and stays only in the project: a missed preference costs a sentence the user can say
again, a project fact shown in another project breaks isolation. The rule never decides what the
sentence means, only that it has this shape.
"""

from __future__ import annotations

import re

MAX_CHARS = 300
MIN_CHARS = 16  # shorter than this is not a rule
MAX_PER_CAPTURE = 5  # sentences one capture may add to the user's scope

_I = r"\bI(?:'m|\s+am)?"
_ADVERB = r"(?:\s+(?:really|generally|usually|always|never|personally|simply|just|strongly))*"
STANCE = re.compile(
    rf"(?i:{_I}{_ADVERB}\s+(?:want|prefer|like|need|expect|require|insist|hate|dislike|can't stand"
    rf"|cannot stand|do not want|don't want|do not like|don't like|never want|always want))\b"
    rf"|(?i:\bI(?:'d|\s+would)\s+(?:like|prefer|rather|want)\b)"
    rf"|(?i:\bmy\s+(?:standing\s+|personal\s+|general\s+)?(?:preference|preferences|rule|convention)s?\b)"
    rf"|(?i:\b(?:standing|personal)\s+preferences?\b)"
    rf"|(?i:\b(?:never|always|don't|do\s+not)\b[^.!;]*\bfor\s+me\b)"
)
EVERY_PROJECT = re.compile(
    r"(?i)\b(?:"
    r"(?:in|on|for|across|within|to)\s+(?:all|any|every|each)(?:\s+of)?\s+(?:my|your)"
    r"\s+(?:projects?|repos?|repositories|codebases|code\s+bases|workspaces)"
    r"|(?:whatever|no\s+matter\s+(?:which|what|the))\s+(?:the\s+)?(?:project|repo|repository|codebase)"
    r"|regardless\s+of\s+(?:the\s+)?(?:project|repo|repository|codebase)"
    r"|(?:everywhere|wherever)\s+I\s+(?:work|code|write)"
    r"|(?:in|on)\s+any\s+(?:project|repo|repository|codebase)\s+I\s+(?:work|am\s+on|touch)"
    r")\b"
)
# Ties a sentence to this project, this session or this moment.
LOCAL = re.compile(
    r"(?i)\b(?:this|these|here|our|we|we're|we've|we'll|let's|(?-i:us)|today|tonight|tomorrow|"
    r"yesterday|now|currently|at\s+the\s+moment|for\s+the\s+moment|for\s+now|just\s+this|"
    r"this\s+time|temporar\w*|until|already|just|still|again|recently|"
    r"previous|last|above|below|earlier|the\s+same\s+as)\b"
)
# A project named in the sentence ("in project alpha", "for the alpha repo").
NAMED_PROJECT = re.compile(
    r"(?i)\b(?:(?:in|for|on|of|to)\s+(?:the\s+)?(?:project|repo|repository)\s+(?!I\b|you\b|that\b|"
    r"where\b|and\b|or\b|not\b|only\b)[\w-]+|the\s+[\w-]+\s+(?:project|repo|repository|codebase)\b)"
)
CODE_LIKE = re.compile(
    r"[`<>{}\[\]\\|=@#$%^*~]|://|\w\(\)|::|\b\w+_\w+\b|\b[a-z]+[A-Z]\w*\b|\b[A-Z]{2,}_[A-Z]+\b"
    r"|\b[\w-]+\.(?:py|js|ts|tsx|jsx|json|ya?ml|toml|md|txt|sql|sh|cfg|ini|rs|go|java|rb|cpp|lock|env)\b"
    r"|(?:^|\s)(?:\.{1,2}|~)?/[\w.-]+|\b[\w.-]+/[\w.-]+/"
    r"|(?:^|\s)-{1,2}[A-Za-z][\w-]*|:\d{2,5}\b|\b\d{1,3}(?:\.\d{1,3}){3}\b"
)
NOT_A_STATEMENT = re.compile(
    r"(?i)^\s*(?:if|when|whenever|unless|suppose|imagine|what\s+if|should|can|could|would|do|does|"
    r"did|is|are|was|were|why|how|what|which|who|where|maybe|perhaps|i\s+wonder|i\s+used\s+to|"
    r"i\s+didn't|i\s+did\s+not|i\s+never\s+said|i\s+told|he|she|they|my\s+(?:manager|boss|"
    r"client|team|colleague|friend))\b"
)
OTHERS = re.compile(
    r"(?i)\b(?:he|she|they|them|their|his|her|client|customer|boss|manager|colleague|"
    r"someone|somebody|everyone|everybody|people|users?)\b"
)
# "... in every project, not only here": the speaker widens the scope, it does not narrow it.
NOT_ONLY_HERE = re.compile(
    r"(?i),?\s*(?:and\s+)?not\s+(?:only|just)\s+(?:here|in\s+this\s+\w+|for\s+this\s+\w+)"
)
SENTENCES = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])|\n+")
SPEAKER = re.compile(r"^(?:User|Assistant): ")
FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)


def is_preference(sentence: str) -> bool:
    """True when one sentence of the user's own words has the shape described above."""
    text = " ".join(sentence.split())
    if EVERY_PROJECT.search(text):
        text = NOT_ONLY_HERE.sub("", text)
    if not MIN_CHARS <= len(text) <= MAX_CHARS or "?" in text:
        return False
    if NOT_A_STATEMENT.match(text) or CODE_LIKE.search(text) or LOCAL.search(text):
        return False
    if NAMED_PROJECT.search(text) or OTHERS.search(text):
        return False
    return bool(STANCE.search(text) and EVERY_PROJECT.search(text))


def preferences(text: str) -> list[str]:
    """The sentences of `text`, the user's own turns, that state a standing personal preference,
    in order, each once, at most `MAX_PER_CAPTURE`. Fenced code and quoted lines are skipped."""
    found: list[str] = []
    for paragraph in re.split(r"\n\s*\n", FENCE.sub("\n", text)):
        paragraph = SPEAKER.sub("", paragraph.strip())
        lines = [ln for ln in paragraph.split("\n") if not ln.lstrip().startswith((">", "|"))]
        for sentence in SENTENCES.split("\n".join(lines)):
            sentence = " ".join(sentence.split())
            if is_preference(sentence) and sentence not in found:
                found.append(sentence)
                if len(found) == MAX_PER_CAPTURE:
                    return found
    return found
