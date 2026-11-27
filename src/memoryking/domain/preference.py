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


# --- A preference the assistant proposes and the user accepts ------------------------------
#
# The user may never state the preference themselves: the assistant says it ("We settled your
# standing preference for every project: ...") and the user answers "Yes" or "Confirmed". The
# pair is recognised by a stated rule, without a model, from two adjacent turns and nothing
# else (the hooks pass no tool input and no tool result):
#
# 1. The assistant's turn has exactly one sentence that proposes or restates a standing rule of
#    the user's for every project: an every-project phrase (the one above, or a bare `for every
#    project` when the sentence also says `your preference`), an attribution to the user (`your
#    standing preference`, `you prefer`, `I'll always`), and none of what vetoes a sentence above
#    (a local word, a named project, code-like text, a rule of someone else) or a hedge (`if`,
#    `could`, `but`, `instead`). `We settled` and `We agreed` at the start are not a local word
#    here. A sentence with a scope phrase and no attribution (`Settled for all your projects:
#    ...`) is not enough: it may report a fact, not state a rule.
# 2. That turn has no question, no quotation mark, fence or quoted line and no copied text, and
#    says nothing that narrows the rule to this project, so a `yes` cannot be answering anything
#    else and the sentence is the assistant's own.
# 3. The user's very next turn is short and says yes and nothing else: only whole clauses from a
#    closed list of clear acceptances (`Yes`, `Confirmed`, `That is settled`, `Keep it that way`)
#    and thanks, with no condition, no narrowing, no question and no negation. A bare `ok` or
#    `thanks` is not an acceptance: it may answer the work rather than the rule.
#
# When in doubt nothing is captured. Both sentences are kept as they were written.

MAX_TURN_CHARS = 800
MAX_ACCEPTANCE_CHARS = 60
PROPOSED = "The assistant proposed: "
ACCEPTED = "The user accepted: "

_BARE_EVERY = re.compile(
    r"(?i)\b(?:in|on|for|across)\s+(?:all|every|each)\s+(?:of\s+)?(?:projects?|repos?|repositories|"
    r"codebases)\b(?!\s+(?:in|of|under|inside|within|that|which|here|we|you)\b)"
)
_YOUR_PREFERENCE = re.compile(
    r"(?i)\byour\s+(?:standing\s+|personal\s+|general\s+)?(?:preference|preferences|rule|convention)s?\b"
)
_PROPOSES = re.compile(
    r"(?i)\byour\s+(?:standing\s+|personal\s+|general\s+)?(?:preference|preferences|rule|convention)s?\b"
    r"|\byou\s+(?:(?:always|generally|usually|really|never)\s+)?(?:want|prefer|like|need|expect|require|"
    r"dislike|hate|(?:do\s+not|don't)\s+want)\b"
    r"|\bI(?:'ll|\s+will)\s+(?:always|never)\b"
)
_WE_SETTLED = re.compile(
    r"(?i)^we\s+(?:settled|agreed|decided|confirmed|noted)(?:\s+(?:on|that))?\b"
)
_WIDEN = re.compile(
    r"(?i),?\s*(?:and\s+)?not\s+(?:only|just)\s+(?:here|this\s+one|in\s+this\s+\w+|for\s+this\s+\w+)"
)
_HEDGE = re.compile(
    r"(?i)\b(?:if|unless|would|could|might|may|maybe|perhaps|possibly|whether|suppose|assuming|"
    r"provided|but|however|except|instead|although|though|unsure|think|guess|probably|seems?)\b"
)
_NARROW = re.compile(
    r"(?i)\b(?:only|just)\s+(?:here|in\s+this|for\s+this|this)\b|\bthis\s+(?:project|repo|repository|"
    r"codebase)\s+only\b|\bnot\s+(?:for|in)\s+(?:all|every|any|other)\b"
)
_QUOTING = re.compile(r"[\"“”`]|\[quoted text left out")

_STRONG = frozenset(
    {
        "yes", "yes please", "yep", "yeah", "yup", "correct", "exactly", "confirmed", "agreed",
        "absolutely", "definitely", "sounds good", "that works", "that works for me",
        "that is right", "that's right", "that is correct", "that's correct", "that is settled",
        "that's settled", "that is what we agreed", "that's what we agreed", "that is what i want",
        "that's what i want", "that is what i wanted", "that's what i wanted", "keep it that way",
        "keep it like that", "please keep it that way", "please do", "do that",
    }
)  # fmt: skip
_FILLER = frozenset(
    {
        "right", "ok", "okay", "good", "great", "perfect", "fine", "nice", "cool", "thanks",
        "thank you", "thx", "please",
    }
)  # fmt: skip
_CLAUSES = re.compile(r"\s*(?:[,.;!:]+|\s-\s|\s+and\s+)\s*")


def is_proposal(sentence: str) -> bool:
    """True when one sentence of the assistant's turn proposes or restates a standing rule of the
    user's for every project (rule 1 above). The caller applies rule 2 to the whole turn."""
    text = " ".join(sentence.split())
    text = _WE_SETTLED.sub("", _WIDEN.sub("", text), count=1).strip()
    if not MIN_CHARS <= len(text) <= MAX_CHARS or "?" in text:
        return False
    if NOT_A_STATEMENT.match(text) or CODE_LIKE.search(text) or LOCAL.search(text):
        return False
    if NAMED_PROJECT.search(text) or OTHERS.search(text) or _HEDGE.search(text):
        return False
    if _QUOTING.search(text):
        return False
    scope = EVERY_PROJECT.search(text) or (
        _BARE_EVERY.search(text) and _YOUR_PREFERENCE.search(text)
    )
    return bool(scope and _PROPOSES.search(text))


def is_acceptance(turn: str) -> bool:
    """True when the user's turn is a short, plain yes (rule 3 above)."""
    text = " ".join(turn.replace("’", "'").split()).strip().lower()
    if not text or len(text) > MAX_ACCEPTANCE_CHARS or "?" in text:
        return False
    clauses = [c for c in _CLAUSES.split(text) if c]
    if not clauses or len(clauses) > 4:
        return False
    if any(c not in _STRONG and c not in _FILLER for c in clauses):
        return False
    return any(c in _STRONG for c in clauses)


def proposal_in(turn: str) -> str | None:
    """The one sentence of an assistant's turn that proposes a standing rule, or None when the
    turn has none, has two, or is of a shape a `yes` could answer something else in (rule 2)."""
    text = turn.strip()
    if not text or len(text) > MAX_TURN_CHARS or "?" in text:
        return None
    if FENCE.search(text) or "```" in text or _QUOTING.search(text):
        return None
    if any(ln.lstrip().startswith((">", "|")) for ln in text.split("\n")):
        return None
    flat = _WIDEN.sub("", " ".join(text.split()))
    if _NARROW.search(flat):
        return None
    sentences = [" ".join(s.split()) for s in SENTENCES.split(text) if s.strip()]
    proposals = [s for s in sentences if is_proposal(s)]
    if len(proposals) != 1:
        return None
    for other in sentences:  # a second scope claim that is not the proposal: not clear which
        if other != proposals[0] and (EVERY_PROJECT.search(other) or _BARE_EVERY.search(other)):
            return None
    return proposals[0]


def accepted_proposals(pieces: list[str]) -> list[tuple[str, str]]:
    """(the assistant's sentence, the user's accepting words) for each proposal the user's very
    next turn accepts. `pieces` are the conversation in order, each opened by `Assistant: `,
    `User: ` or `Tool error: `; a piece that is none of the first two breaks the pair. At most
    `MAX_PER_CAPTURE`, each sentence once."""
    found: list[tuple[str, str]] = []
    for before, after in zip(pieces, pieces[1:], strict=False):
        if not (before.startswith("Assistant: ") and after.startswith("User: ")):
            continue
        said = after[len("User: ") :].strip()
        if not is_acceptance(said):
            continue
        sentence = proposal_in(before[len("Assistant: ") :])
        if sentence and all(sentence != s for s, _ in found):
            found.append((sentence, " ".join(said.split())))
            if len(found) == MAX_PER_CAPTURE:
                break
    return found


def evidence(sentence: str, acceptance: str) -> str:
    """The text a recognised pair is kept as: both sets of words, as written, each labelled."""
    return f"{PROPOSED}{sentence}\n{ACCEPTED}{acceptance}"
