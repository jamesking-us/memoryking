"""The small Python repositories the tasks are made from.

A family is one repository skeleton (a stub module the agent must implement), the prompt that
asks for it without giving the values, an oracle solution that is written from the values, a
probe that runs the agent's code and reports which values it uses, and the slots: the facts
(a delimiter, a retry count, a prefix) that earlier sessions established and that the code has
to follow. Every slot has a pool of plausible values and the words that describe it in a
sentence, so a task's needles, corrections and near-miss distractors are generated from it
and nothing is pasted by hand.

The probe is Python source that runs with the working directory on `sys.path` and prints one
JSON object. `__POOL:<slot>__` in it is replaced by the JSON list of that slot's values.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal


@dataclass(frozen=True)
class Slot:
    name: str
    attr: str  # how the sentence names it: "column delimiter"
    pool: tuple[tuple[str, str], ...]  # (value, how a sentence writes it)
    kind: str = "decision"  # decision | constraint | interface | preference
    others: tuple[str, ...] = ()  # other attributes of the same subject (near-miss)
    flag: tuple[str, str] | None = None  # (text when on, text when off) for a retractable fact


@dataclass(frozen=True)
class Family:
    id: str
    project: str
    subject: str  # "orders export": the words a sentence and the task share
    package: str
    module: str
    prompt: str
    stub: str
    oracle: str  # module source with {slot} placeholders, formatted with repr() values
    probe: str
    slots: tuple[Slot, ...]
    other_subjects: tuple[str, ...]
    decode: object = None  # optional callable(raw: dict) -> dict of slot -> str
    files: dict[str, str] = field(default_factory=dict)  # extra repository files

    def pool_values(self, slot: str) -> list[str]:
        return [v for v, _ in next(s for s in self.slots if s.name == slot).pool]


def pool(*pairs):
    return tuple((str(v), p if p is not None else str(v)) for v, p in pairs)


def same(*values):
    return tuple((str(v), str(v)) for v in values)


# ------------------------------------------------------------------- decoders


TYPOGRAPHIC = {0xA0: " ", 0x202F: " ", 0x2009: " ", 0x2019: "'", 0x02BC: "'"}


def _decode_date(raw: dict) -> dict:
    out = dict(raw)
    shown = raw.get("date_out")
    out["date_fmt"] = next(
        (
            p
            for p in ("%d.%m.%Y", "%Y/%m/%d", "%d-%b-%Y", "%m.%d.%y")
            if date(2024, 3, 9).strftime(p) == shown
        ),
        "?",
    )
    # a typographic space or apostrophe is the same separator as the plain one
    number = str(raw.get("number_out") or "").translate(TYPOGRAPHIC)
    out["thousands"] = number[1] if number.startswith("1") and number[2:5] == "234" else "?"
    return out


MODES = {
    "ROUND_HALF_EVEN": ROUND_HALF_EVEN,
    "ROUND_HALF_UP": ROUND_HALF_UP,
    "ROUND_DOWN": ROUND_DOWN,
    "ROUND_CEILING": ROUND_CEILING,
}
MONEY_INPUTS = [
    "2.5",
    "0.1245",
    "0.12345",
    "0.123445",
    "-1.5",
    "7.55555",
    "1.99999",
    "0.99999991",
    "-0.123456789",
]
DECIMALS = [0, 3, 4, 5]


def money_apply(value: str, decimals: int, mode: str) -> str:
    return str(Decimal(value).quantize(Decimal(1).scaleb(-decimals), rounding=MODES[mode]))


def _money_text(text) -> str:
    """The sign of a zero is not a fact of the history: `-0.000` and `0.000` are one answer."""
    text = str(text).strip()
    return text[1:] if text.startswith("-") and not any(c in "123456789" for c in text) else text


def _decode_money(raw: dict) -> dict:
    outs = raw.get("outputs")
    found = {"decimals": "?", "rounding": "?"}
    if isinstance(outs, list):
        seen = [_money_text(o) for o in outs]
        for d in DECIMALS:
            for m in MODES:
                if [_money_text(money_apply(v, d, m)) for v in MONEY_INPUTS] == seen:
                    found = {"decimals": str(d), "rounding": m}
    return found


def _decode_logfmt(raw: dict) -> dict:
    level = str(raw.get("level_part") or "")
    case = "upper" if level.isupper() else "lower" if level.islower() else "title"
    return {"sep": raw.get("sep"), "level_case": case if level else "?"}


# ------------------------------------------------------------------- families

SEP = pool(
    (" >> ", "a space, two greater-than signs and a space (' >> ')"),
    (" :: ", "a space, two colons and a space (' :: ')"),
    (" ~ ", "a space, a tilde and a space (' ~ ')"),
    (" ; ", "a space, a semicolon and a space (' ; ')"),
)

F: dict[str, Family] = {}


def add(f: Family) -> None:
    F[f.id] = f


add(
    Family(
        id="export-delimiter",
        project="ledger-sync",
        subject="orders export",
        package="ledger",
        module="exporter",
        prompt=(
            "In ledger-sync, implement `write_orders(rows, path)` in `ledger/exporter.py`. `rows` "
            "is a list of `(ref, amount, status)` tuples of strings. Write the orders export file "
            "to `path`: one header line, then one line per row. Follow the conventions this team "
            "has already settled for the orders export. Standard library only."
        ),
        stub='def write_orders(rows, path):\n    """Write the orders export file."""\n    raise NotImplementedError\n',
        oracle=(
            'def write_orders(rows, path):\n    with open(path, "w", encoding="utf-8") as out:\n'
            '        out.write({delimiter}.join([{header}, "Amount", "Status"]) + "\\n")\n'
            '        for row in rows:\n            out.write({delimiter}.join(row) + "\\n")\n'
        ),
        probe=(
            "import json, os, tempfile\nfrom ledger.exporter import write_orders\n"
            'p = os.path.join(tempfile.mkdtemp(), "o.txt")\nwrite_orders([("A1", "5", "ok")], p)\n'
            # the file encoding and the quote character are other settled conventions of the
            # export (the history states them), so the probe reads any of them
            'import re\nraw = open(p, "rb").read()\n'
            'if raw[:2] in (b"\\xff\\xfe", b"\\xfe\\xff"):\n    text = raw.decode("utf-16")\n'
            'elif raw[:1] == b"\\x00":\n    text = raw.decode("utf-16-be", "replace")\n'
            'elif b"\\x00" in raw:\n    text = raw.decode("utf-16-le", "replace")\n'
            'else:\n    text = raw.decode("utf-8-sig", "replace")\n'
            "lines = [x.translate({34: None, 39: None, 96: None}) for x in text.splitlines() if x.strip()]\n"
            # spaces around the delimiter or the fields are not a fact of the history either
            'm = re.match(r" *A1 *([^ \\t])", lines[1]) if len(lines) > 1 else None\n'
            "d = m.group(1) if m else None\n"
            'print(json.dumps({"delimiter": d, "header": lines[0].split(d)[0].strip() if d else None}))\n'
        ),
        slots=(
            Slot(
                "delimiter",
                "column delimiter",
                pool(
                    (";", "a semicolon (;)"),
                    ("|", "a pipe (|)"),
                    ("^", "a caret (^)"),
                    ("~", "a tilde (~)"),
                ),
                "decision",
                ("file encoding", "line ending", "quote character"),
            ),
            Slot(
                "header",
                "label of the first header column",
                pool(
                    ("OrderRef", "OrderRef"),
                    ("ord_id", "ord_id"),
                    ("Order#", "Order#"),
                    ("RefNo", "RefNo"),
                ),
                "interface",
                ("column count", "header casing"),
            ),
        ),
        other_subjects=("refunds export", "customer export", "audit export"),
    )
)

add(
    Family(
        id="money-rounding",
        project="ledger-sync",
        subject="invoice amounts",
        package="ledger",
        module="money",
        prompt=(
            "In ledger-sync, implement `round_amount(value)` in `ledger/money.py`: `value` is a "
            "decimal string; return the rounded amount as a decimal string. Follow the "
            "conventions this team has already settled for invoice amounts. Standard library "
            "only."
        ),
        stub='def round_amount(value):\n    """Round an invoice amount given as a decimal string."""\n    raise NotImplementedError\n',
        oracle=(
            "from decimal import {rounding_name}, Decimal\n\n\ndef round_amount(value):\n"
            "    return str(Decimal(value).quantize(Decimal(1).scaleb(-{decimals_int}), "
            "rounding={rounding_name}))\n"
        ),
        probe=(
            "import json\nfrom ledger.money import round_amount\n"
            'print(json.dumps({"outputs": [str(round_amount(v)) for v in __INPUTS__]}))\n'
        ),
        slots=(
            Slot(
                "decimals",
                "number of decimal places",
                same(*DECIMALS),
                "constraint",
                ("currency code", "minimum amount"),
            ),
            Slot(
                "rounding",
                "rounding mode",
                pool(
                    ("ROUND_HALF_EVEN", "banker's rounding (half to even)"),
                    ("ROUND_HALF_UP", "half up, so ties go away from zero"),
                    ("ROUND_DOWN", "truncation toward zero"),
                    ("ROUND_CEILING", "rounding up toward positive infinity"),
                ),
                "decision",
                ("tax handling", "currency code"),
            ),
        ),
        other_subjects=("tax lines", "refund amounts", "ledger totals"),
        decode=_decode_money,
    )
)

add(
    Family(
        id="id-format",
        project="ledger-sync",
        subject="record identifiers",
        package="ledger",
        module="ids",
        prompt=(
            "In ledger-sync, implement `new_id(n)` in `ledger/ids.py`: it returns the identifier "
            "string for the n-th record. Follow the conventions this team has already settled "
            "for record identifiers. Standard library only."
        ),
        stub='def new_id(n):\n    """The identifier of the n-th record."""\n    raise NotImplementedError\n',
        oracle='def new_id(n):\n    return {prefix} + "-" + str(n).zfill({width_int})\n',
        probe=(
            "import json, re\nfrom ledger.ids import new_id\n"
            # the separator is not one of the task's facts: any one character, or none, is read
            'm = re.match(r"([A-Za-z]+)[^A-Za-z0-9]{0,3}(\\d+)$", str(new_id(42)))\n'
            'print(json.dumps({"prefix": m.group(1) if m else None, '
            '"width": len(m.group(2)) if m else None}))\n'
        ),
        slots=(
            Slot(
                "prefix",
                "prefix",
                same("ORD", "INV", "TXN", "LDG"),
                "constraint",
                ("separator", "case"),
            ),
            Slot(
                "width",
                "zero-padded digit count",
                same(4, 6, 8, 9),
                "interface",
                ("starting number", "check digit"),
            ),
        ),
        other_subjects=("batch identifiers", "customer identifiers", "file names"),
    )
)

add(
    Family(
        id="retry-policy",
        project="gateway-client",
        subject="gateway retry policy",
        package="gwc",
        module="retry",
        prompt=(
            "In gateway-client, implement `call_with_retry(fn, sleep)` in `gwc/retry.py`: call "
            "`fn()`; when it raises, wait with `sleep(seconds)` and try again; after the last "
            "attempt re-raise the error. Follow the conventions this team has already settled "
            "for the gateway retry policy. Standard library only."
        ),
        stub='def call_with_retry(fn, sleep):\n    """Call fn with retries."""\n    raise NotImplementedError\n',
        oracle=(
            "def call_with_retry(fn, sleep):\n    last = None\n    for attempt in range({attempts_int}):\n"
            "        try:\n            return fn()\n        except Exception as error:\n"
            "            last = error\n            if attempt < {attempts_int} - 1:\n"
            "                sleep({base_ms_int} / 1000 * 2 ** attempt)\n    raise last\n"
        ),
        probe=(
            "import json\nfrom gwc.retry import call_with_retry\ncalls, naps = [], []\n\n\n"
            'def boom():\n    calls.append(1)\n    raise ValueError("x")\n\n\ntry:\n'
            "    call_with_retry(boom, naps.append)\nexcept ValueError:\n    pass\n"
            'print(json.dumps({"attempts": len(calls), '
            '"base_ms": round(naps[0] * 1000) if naps else None}))\n'
        ),
        slots=(
            Slot(
                "attempts",
                "number of attempts",
                same(4, 6, 7, 9),
                "constraint",
                ("jitter", "status codes that retry"),
            ),
            Slot(
                "base_ms",
                "first backoff delay in milliseconds",
                pool((150, "150 ms"), (250, "250 ms"), (400, "400 ms"), (750, "750 ms")),
                "decision",
                ("maximum delay", "jitter"),
            ),
        ),
        other_subjects=("webhook retry policy", "export retry policy", "login retry policy"),
    )
)

add(
    Family(
        id="net-timeouts",
        project="gateway-client",
        subject="gateway client timeouts",
        package="gwc",
        module="net",
        prompt=(
            "In gateway-client, implement `timeouts()` in `gwc/net.py`: return the "
            "`(connect, read)` timeouts in seconds as a tuple of integers. Follow the "
            "conventions this team has already settled for the gateway client timeouts."
        ),
        stub='def timeouts():\n    """The (connect, read) timeouts in seconds."""\n    raise NotImplementedError\n',
        oracle="def timeouts():\n    return ({connect_int}, {read_int})\n",
        probe=(
            "import json\nfrom gwc.net import timeouts\nt = timeouts()\n"
            'print(json.dumps({"connect": t[0], "read": t[1]}))\n'
        ),
        slots=(
            Slot(
                "connect",
                "connect timeout in seconds",
                same(2, 3, 7, 11),
                "interface",
                ("pool size", "proxy"),
            ),
            Slot(
                "read",
                "read timeout in seconds",
                same(20, 45, 75, 120),
                "decision",
                ("pool size", "proxy"),
            ),
        ),
        other_subjects=(
            "webhook client timeouts",
            "export client timeouts",
            "health check timeouts",
        ),
    )
)

add(
    Family(
        id="port-config",
        project="gateway-client",
        subject="gateway client settings",
        package="gwc",
        module="settings",
        prompt=(
            "In gateway-client, implement `load(env)` in `gwc/settings.py`: `env` is a dict of "
            "environment variables; return a dict with the listen port as an integer under "
            "`port`. The port can be overridden by the environment variable `PORT` with the "
            "team's prefix in front of the name. Follow the conventions this team has already "
            "settled for the gateway client settings."
        ),
        stub='def load(env):\n    """The settings dict."""\n    raise NotImplementedError\n',
        oracle=(
            'def load(env):\n    return {{"port": int(env.get({env_prefix} + "PORT", {port_int}))}}\n'
        ),
        probe=(
            'import json\nfrom gwc.settings import load\nd = load({})["port"]\nprefix = None\n'
            'for cand in __POOL:env_prefix__:\n    if load({cand + "PORT": "1234"})["port"] == 1234:\n'
            '        prefix = cand\nprint(json.dumps({"port": d, "env_prefix": prefix}))\n'
        ),
        slots=(
            Slot(
                "port",
                "default listen port",
                same(7411, 8317, 9532, 6044),
                "decision",
                ("bind address", "workers"),
            ),
            Slot(
                "env_prefix",
                "prefix of the environment variable overrides",
                same("GWC_", "GATE_", "GW_CLIENT_", "EDGEGW_"),
                "constraint",
                ("bind address",),
            ),
        ),
        other_subjects=("webhook client settings", "export client settings", "worker settings"),
    )
)

add(
    Family(
        id="rate-limit",
        project="edge-proxy",
        subject="edge rate limiter",
        package="edge",
        module="limiter",
        prompt=(
            "In edge-proxy, implement the class `Limiter(clock)` in `edge/limiter.py`; "
            "`clock()` returns the current time in seconds and `allow(key)` returns whether a "
            "request from `key` may go through now. Follow the conventions this team has "
            "already settled for the edge rate limiter. Standard library only."
        ),
        stub=(
            "class Limiter:\n    def __init__(self, clock):\n        self.clock = clock\n\n"
            "    def allow(self, key):\n        raise NotImplementedError\n"
        ),
        oracle=(
            "class Limiter:\n    def __init__(self, clock):\n        self.clock = clock\n"
            "        self.seen = {{}}\n\n    def allow(self, key):\n        now = self.clock()\n"
            "        recent = [t for t in self.seen.get(key, []) if now - t < {window_int}]\n"
            "        if len(recent) >= {limit_int}:\n            self.seen[key] = recent\n"
            "            return False\n        recent.append(now)\n        self.seen[key] = recent\n"
            "        return True\n"
        ),
        probe=(
            # how the limiter counts (a sliding log, a fixed window, a token bucket) is not a
            # fact of the history: the limit is what one instant allows, and the window is the
            # first half second at which a key that used its whole limit may use it all again
            "import json\nfrom edge.limiter import Limiter\n\n\ndef fresh():\n"
            "    now = [0.0]\n    return now, Limiter(lambda: now[0])\n\n\n"
            "def used(l):\n    n = 0\n"
            '    while n < 200 and l.allow("k"):\n        n += 1\n    return n\n\n\n'
            "now, l = fresh()\nn = used(l)\nwindow = None\n"
            "for step in range(1, 801 if n else 1):\n"
            "    now, l = fresh()\n    used(l)\n    now[0] = step / 2\n"
            '    if all(l.allow("k") for _ in range(n)):\n        window = int(now[0])\n        break\n'
            'print(json.dumps({"limit": n, "window": window}))\n'
        ),
        slots=(
            Slot(
                "limit",
                "requests allowed per window",
                same(7, 12, 18, 25),
                "constraint",
                ("burst size", "key"),
            ),
            Slot(
                "window",
                "window length in seconds",
                same(20, 45, 90, 150),
                "decision",
                ("burst size", "key"),
            ),
        ),
        other_subjects=("login rate limiter", "export rate limiter", "webhook rate limiter"),
    )
)

add(
    Family(
        id="log-format",
        project="edge-proxy",
        subject="proxy log lines",
        package="edge",
        module="logfmt",
        prompt=(
            "In edge-proxy, implement `format_line(level, message)` in `edge/logfmt.py`: it "
            "returns the log line for a level name such as `warn` and a message. Follow the "
            "conventions this team has already settled for the proxy log lines."
        ),
        stub='def format_line(level, message):\n    """One log line."""\n    raise NotImplementedError\n',
        oracle=(
            'def format_line(level, message):\n    shown = {{"upper": level.upper(), "lower": level.lower(), '
            '"title": level.title()}}[{level_case}]\n    return shown + {sep} + message\n'
        ),
        probe=(
            # the level is found by its own letters, wherever the line puts it and whatever the
            # separator is (a forgotten separator leaves the code free to use another one)
            "import json, re\nfrom edge.logfmt import format_line\n"
            'line = format_line("warn", "disk low")\n'
            "sep = None\nfor cand in __POOL:sep__:\n    if cand in line:\n        sep = cand\n"
            'found = re.search(r"warn\\w*", line, re.I)\n'
            'print(json.dumps({"line": line, "sep": sep, '
            '"level_part": found.group(0) if found else ""}))\n'
        ),
        slots=(
            Slot("sep", "field separator", SEP, "decision", ("timestamp format", "field order")),
            Slot(
                "level_case",
                "level casing",
                pool(
                    ("upper", "upper case, like WARN"),
                    ("lower", "lower case, like warn"),
                    ("title", "capitalized, like Warn"),
                ),
                "preference",
                ("timestamp format", "field order"),
            ),
        ),
        other_subjects=("worker log lines", "audit log lines", "access log lines"),
        decode=_decode_logfmt,
    )
)

add(
    Family(
        id="cache-ttl",
        project="edge-proxy",
        subject="response cache",
        package="edge",
        module="cache",
        prompt=(
            "In edge-proxy, implement `ttl_seconds()` and `make_key(name)` in `edge/cache.py`: "
            "the time to live of a cached response in seconds as an integer, and the cache key "
            "for a response name. Follow the conventions this team has already settled for the "
            "response cache."
        ),
        stub=(
            "def ttl_seconds():\n    raise NotImplementedError\n\n\ndef make_key(name):\n"
            "    raise NotImplementedError\n"
        ),
        oracle=(
            "def ttl_seconds():\n    return {ttl_int}\n\n\ndef make_key(name):\n"
            "    return {key_prefix} + name\n"
        ),
        probe=(
            'import json\nfrom edge.cache import make_key, ttl_seconds\nk = make_key("abc")\n'
            'print(json.dumps({"ttl": ttl_seconds(), "key_prefix": k[:-3] if k.endswith("abc") else None}))\n'
        ),
        slots=(
            Slot(
                "ttl",
                "time to live in seconds",
                same(90, 300, 1800, 5400),
                "decision",
                ("eviction policy", "size limit"),
            ),
            Slot(
                "key_prefix",
                "key prefix",
                same("v3:", "cfg/", "mk.", "c2:"),
                "constraint",
                ("eviction policy", "size limit"),
            ),
        ),
        other_subjects=("session cache", "asset cache", "token cache"),
    )
)

add(
    Family(
        id="date-style",
        project="report-portal",
        subject="report pages",
        package="portal",
        module="formats",
        prompt=(
            "In report-portal, implement `render_date(d)` and `render_number(n)` in "
            "`portal/formats.py`: `d` is a `datetime.date`, `n` a non-negative integer; both "
            "return strings for display on a report page. Follow the conventions this team "
            "has already settled for report pages."
        ),
        stub=(
            "def render_date(d):\n    raise NotImplementedError\n\n\ndef render_number(n):\n"
            "    raise NotImplementedError\n"
        ),
        oracle=(
            "def render_date(d):\n    return d.strftime({date_fmt})\n\n\ndef render_number(n):\n"
            '    return f"{{n:,}}".replace(",", {thousands})\n'
        ),
        probe=(
            "import json\nfrom datetime import date\nfrom portal.formats import render_date, "
            'render_number\nprint(json.dumps({"date_out": render_date(date(2024, 3, 9)), '
            '"number_out": render_number(1234567)}))\n'
        ),
        slots=(
            Slot(
                "date_fmt",
                "date format",
                pool(
                    ("%d.%m.%Y", "day.month.year with dots, like 09.03.2024"),
                    ("%Y/%m/%d", "year/month/day with slashes, like 2024/03/09"),
                    ("%d-%b-%Y", "day-abbreviated month-year, like 09-Mar-2024"),
                    ("%m.%d.%y", "month.day.two-digit year, like 03.09.24"),
                ),
                "preference",
                ("time zone", "week start"),
            ),
            Slot(
                "thousands",
                "thousands separator",
                pool(
                    (" ", "a space"), (".", "a dot"), ("'", "an apostrophe"), ("_", "an underscore")
                ),
                "decision",
                ("decimal separator", "currency symbol"),
            ),
        ),
        other_subjects=("email digests", "printed reports", "dashboard tiles"),
        decode=_decode_date,
    )
)

add(
    Family(
        id="batch-export",
        project="report-portal",
        subject="export batches",
        package="portal",
        module="batching",
        prompt=(
            "In report-portal, implement `batches(items)` in `portal/batching.py`: `items` is a "
            "list of dicts that each have a `ts` integer; return a list of lists (batches) "
            "covering every item. Follow the conventions this team has already settled for "
            "export batches."
        ),
        stub='def batches(items):\n    """Split items into export batches."""\n    raise NotImplementedError\n',
        oracle=(
            'def batches(items):\n    ordered = sorted(items, key=lambda i: i["ts"], '
            "reverse={reverse})\n    return [ordered[i:i + {size_int}] for i in range(0, len(ordered), {size_int})]\n"
        ),
        probe=(
            "import json, random\nfrom portal.batching import batches\n"
            'items = [{"ts": n} for n in range(1, 301)]\nrandom.Random(3).shuffle(items)\n'
            # the order is that of the batches (the order inside one is not a fact), and every
            # item has to be in some batch
            "bs = batches(items)\n"
            'mins = [min(i["ts"] for i in b) for b in bs]\n'
            'covered = sorted(i["ts"] for b in bs for i in b) == list(range(1, 301))\n'
            'order = "partial" if not covered else "oldest-first" if mins == sorted(mins) '
            'else "newest-first" if mins == sorted(mins, reverse=True) else "unordered"\n'
            'print(json.dumps({"size": max(len(b) for b in bs), "order": order}))\n'
        ),
        slots=(
            Slot(
                "size",
                "batch size",
                same(25, 40, 64, 120),
                "constraint",
                ("compression", "file naming"),
            ),
            Slot(
                "order",
                "processing order",
                pool(("oldest-first", "oldest first"), ("newest-first", "newest first")),
                "decision",
                ("compression", "file naming"),
            ),
        ),
        other_subjects=("import batches", "email batches", "archive batches"),
    )
)

add(
    Family(
        id="retention",
        project="report-portal",
        subject="report retention",
        package="portal",
        module="retention",
        prompt=(
            "In report-portal, implement `keep_days()` and `archive_path(name)` in "
            "`portal/retention.py`: the number of days a report stays online as an integer, and "
            "the relative path (with `/`) of an archived report file. Follow the conventions "
            "this team has already settled for report retention."
        ),
        stub=(
            "def keep_days():\n    raise NotImplementedError\n\n\ndef archive_path(name):\n"
            "    raise NotImplementedError\n"
        ),
        oracle=(
            "def keep_days():\n    return {days_int}\n\n\ndef archive_path(name):\n"
            '    return {archive_dir} + "/" + name\n'
        ),
        probe=(
            "import json\nfrom portal.retention import archive_path, keep_days\n"
            'parts = [p for p in archive_path("x.csv").split("/") if p not in ("", ".")]\n'
            'print(json.dumps({"days": keep_days(), "archive_dir": parts[0] if len(parts) > 1 else None}))\n'
        ),
        slots=(
            Slot(
                "days",
                "days kept online",
                same(14, 45, 120, 400),
                "constraint",
                ("legal hold", "compression"),
            ),
            Slot(
                "archive_dir",
                "archive folder",
                same("cold", "attic", "vault", "deepfreeze"),
                "interface",
                ("legal hold", "compression"),
            ),
        ),
        other_subjects=("log retention", "export retention", "backup retention"),
    )
)

add(
    Family(
        id="trace-header",
        project="gateway-client",
        subject="gateway request headers",
        package="gwc",
        module="headers",
        prompt=(
            "In gateway-client, implement `build_headers(token)` in `gwc/headers.py`: return the "
            "dict of HTTP headers for an outgoing request that carries the bearer `token`. "
            "Follow the conventions this team has already settled for the gateway request "
            "headers."
        ),
        stub='def build_headers(token):\n    """Headers of an outgoing request."""\n    raise NotImplementedError\n',
        oracle=(
            'def build_headers(token):\n    headers = {{"Authorization": "Bearer " + token, '
            '{client_header}: "gwc"}}\n{trace_block}    return headers\n'
        ),
        probe=(
            # header names are case-insensitive, and the value of the client header (and of
            # the trace header) is not a fact of the history, only its name and presence
            'import json\nfrom gwc.headers import build_headers\nh = build_headers("tok")\n'
            "low = {str(k).lower() for k in h}\n"
            "client = None\nfor cand in __POOL:client_header__:\n    if cand.lower() in low:\n"
            '        client = cand\nprint(json.dumps({"trace_header": "on" if "x-trace-id" in low else "off", '
            '"client_header": client}))\n'
        ),
        slots=(
            Slot(
                "trace_header",
                "X-Trace-Id header",
                same("on", "off"),
                "decision",
                ("timeout header",),
                flag=(
                    "Every outgoing gateway request carries an X-Trace-Id header.",
                    "We no longer send the X-Trace-Id header on gateway requests; the collector "
                    "that read it is gone.",
                ),
            ),
            Slot(
                "client_header",
                "name of the client identification header",
                same("X-Client-Ref", "X-Gwc-Client", "X-Caller", "X-Origin-App"),
                "interface",
                ("timeout header",),
            ),
        ),
        other_subjects=(
            "webhook request headers",
            "export request headers",
            "login request headers",
        ),
    )
)

add(
    Family(
        id="audit-line",
        project="edge-proxy",
        subject="job runner log",
        package="edge",
        module="jobs",
        prompt=(
            "In edge-proxy, implement `run_job(name, log)` in `edge/jobs.py`: `log(line)` "
            "receives each log line of the job run. Write a start line `START <name>` and an "
            "end line, following the conventions this team has already settled for the job "
            "runner log."
        ),
        stub='def run_job(name, log):\n    """Run a job and log it."""\n    raise NotImplementedError\n',
        oracle=(
            'def run_job(name, log):\n    log("START " + name)\n{audit_block}'
            '    log({end_marker} + " " + name)\n'
        ),
        probe=(
            # a prefix such as `[job]`, a colon after the marker, and the order of the lines are
            # not facts of the history
            'import json, re\nfrom edge.jobs import run_job\nlines = []\nrun_job("sync", lines.append)\n'
            'audit = [l for l in lines if re.match(r"\\W*AUDIT\\b", str(l))]\n'
            'ends = [l for l in lines if not re.match(r"\\W*(START|AUDIT)\\b", str(l))]\n'
            'word = re.match(r"\\W*([A-Za-z]+)", str(ends[-1])) if ends else None\n'
            'print(json.dumps({"audit_line": "on" if audit else "off", '
            '"end_marker": word.group(1) if word else None}))\n'
        ),
        slots=(
            Slot(
                "audit_line",
                "audit line",
                same("on", "off"),
                "constraint",
                ("log level",),
                flag=(
                    "Every job run must write an AUDIT line to the job runner log.",
                    "We no longer write AUDIT lines in the job runner log; compliance dropped "
                    "that requirement.",
                ),
            ),
            Slot(
                "end_marker",
                "word that opens the end line",
                same("DONE", "FINISHED", "COMPLETE", "OVER"),
                "decision",
                ("log level",),
            ),
        ),
        other_subjects=("export runner log", "import runner log", "backup runner log"),
    )
)

T1_ORDER = [
    "export-delimiter",
    "money-rounding",
    "id-format",
    "retry-policy",
    "net-timeouts",
    "port-config",
    "rate-limit",
    "log-format",
    "cache-ttl",
    "date-style",
    "batch-export",
    "retention",
]


def render_oracle(family: Family, values: dict[str, str]) -> str:
    """The oracle module source for these slot values."""
    fmt: dict[str, str] = {}
    for key, val in values.items():
        fmt[key] = repr(val)
        try:
            fmt[key + "_int"] = str(int(val))
        except ValueError:
            pass
    if "rounding" in values:
        fmt["rounding_name"] = values["rounding"]
    if "order" in values:
        fmt["reverse"] = "False" if values["order"] == "oldest-first" else "True"
    if "trace_header" in values:
        fmt["trace_block"] = (
            '    headers["X-Trace-Id"] = "t-" + token[-4:]\n'
            if values["trace_header"] == "on"
            else ""
        )
    if "audit_line" in values:
        fmt["audit_block"] = '    log("AUDIT " + name)\n' if values["audit_line"] == "on" else ""
    return family.oracle.format(**fmt)


def render_probe(family: Family) -> str:
    src = family.probe
    for slot in family.slots:
        src = src.replace(f"__POOL:{slot.name}__", json.dumps(family.pool_values(slot.name)))
    return src.replace("__INPUTS__", json.dumps(MONEY_INPUTS))


def repo_files(family: Family) -> dict[str, str]:
    pkg = family.package
    files = {
        "README.md": f"# {family.project}\n\nSmall service library. Run the tests with "
        "`python -m unittest discover -s tests`.\n",
        f"{pkg}/__init__.py": "",
        f"{pkg}/{family.module}.py": family.stub,
        "tests/__init__.py": "",
        f"tests/test_{family.module}.py": (
            "import unittest\n\nfrom {pkg} import {module}\n\n\nclass Smoke(unittest.TestCase):\n"
            "    def test_module_loads(self):\n        self.assertTrue({module})\n\n\n"
            'if __name__ == "__main__":\n    unittest.main()\n'
        ).format(pkg=pkg, module=family.module),
    }
    files.update(family.files)
    return files
