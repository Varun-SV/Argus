"""Chat understanding for the Argus desktop app.

The desktop app is a conversation: people type what they want and Argus
answers with cards (runs, roams, specs, knowledge, …). This module turns a
message into one *intent* the app can execute.

* Slash commands (``/run checkout``) are parsed deterministically, with no
  routing call to the model (the command itself may still use it, e.g. /write).
* Free text is routed through the configured LLM provider, which may only
  pick from a fixed intent vocabulary. Its answer is validated before the app
  acts on it: tests must exist, a roam target must come from the user's own
  words, and a provider switch must name a configured provider.

It also drafts ``.test.yaml`` specs from plain English and explains failed
runs. Drafts are parsed with the real spec parser and are never saved
without an explicit user action.
"""

from __future__ import annotations

import json
import re
from typing import Iterable, List, Mapping, Optional, Sequence

import yaml

from argus.engine.spec import ASSERTION_KINDS, SpecError, parse_spec

# parse_spec converts scalars directly, so a well-formed YAML file such as `retries: once`
# raises ValueError/TypeError rather than SpecError. Anything reading user specs catches all.
SPEC_ERRORS = (SpecError, ValueError, TypeError, AttributeError, KeyError)

ADAPTERS = ("desktop-gui", "browser", "cli")
ENVIRONMENTS = ("local", "capsule")
CAPSULE_PROVIDERS = ("auto", "hyperv", "libvirt")
KNOWLEDGE_ACTIONS = ("show", "reset", "export")

INTENTS = {
    "help": "list what Argus can do",
    "stop": "stop the current run, roam or watch",
    "run": 'run tests. args: {"tests": ["<file>", ...] or "all", "environment"?: "local"|"capsule", '
           '"capsule_provider"?: "auto"|"hyperv"|"libvirt", "retain"?: bool}',
    "dry_run": 'parse tests and list their steps without running. args: {"tests": [...] or "all" or "draft"}',
    "roam": 'explore an app autonomously to find bugs. args: {"target": "<exact target from the message>", '
            '"adapter"?: "desktop-gui"|"browser"|"cli", "minutes"?: number, "memory"?: bool}',
    "write_test": 'draft a new .test.yaml from a description. args: {"description": "<what to test>"}',
    "save_test": "save the most recent drafted test to .argus/",
    "explain": "explain why the most recent failed run failed",
    "knowledge": 'show, reset or export what Argus learned about a target. args: {"target"?: str, '
                 '"action": "show"|"reset"|"export"}',
    "evidence": "show and verify the ATES evidence of the most recent finished run",
    "report": "show recent run history",
    "tokens": "show token usage",
    "providers": "check the model provider connection and vision support",
    "switch_provider": 'use a different configured provider. args: {"provider": "<name>"}',
    "environment": 'show or change where runs execute. args: {"environment"?: "local"|"capsule", '
                   '"capsule_provider"?: "auto"|"hyperv"|"libvirt", "retain"?: bool}',
    "init": "create .argus/ with a starter config and example test",
    "watch": 'watch .argus/*.test.yaml and re-run tests on change. args: {"action": "start"|"stop"}',
    "chat": 'anything else: answer briefly. args: {"reply": "<short answer>"}',
}

HELP_GROUPS = [
    {"title": "Test", "items": [
        {"cmd": "/run", "desc": "Run one test or the whole suite", "text": "/run all"},
        {"cmd": "/dry-run", "desc": "Parse specs, show steps, run nothing", "text": "/dry-run all"},
        {"cmd": "/watch", "desc": "Re-run tests when a spec changes", "text": "/watch"},
        {"cmd": "/write …", "desc": "Plain English to .test.yaml", "text": ""},
    ]},
    {"title": "Explore", "items": [
        {"cmd": "/roam …", "desc": "Autonomous exploration, findings, regression stubs", "text": ""},
        {"cmd": "/knowledge", "desc": "State graph, bug zones, export, reset", "text": "/knowledge"},
    ]},
    {"title": "Configure", "items": [
        {"cmd": "/init", "desc": "Scaffold .argus/", "text": "/init"},
        {"cmd": "/providers", "desc": "Connection and vision check", "text": "/providers"},
        {"cmd": "/env", "desc": "Local or Capsule (Hyper-V · libvirt)", "text": "/env"},
    ]},
    {"title": "Inspect", "items": [
        {"cmd": "/report", "desc": "Recent run history", "text": "/report"},
        {"cmd": "/evidence", "desc": "ATES manifest and verification", "text": "/evidence"},
        {"cmd": "/tokens", "desc": "Session and project usage", "text": "/tokens"},
        {"cmd": "/explain", "desc": "Why did the last run fail?", "text": "/explain"},
    ]},
]


class IntentError(ValueError):
    """The message could not be turned into a safe, executable intent."""


def intent(name: str, **args) -> dict:
    return {"intent": name, "args": args}


def adapter_for(target: str) -> str:
    """Guess the adapter for a roam target the same way people describe them."""
    t = target.strip().lower()
    if re.match(r"^https?://", t) or t.startswith("localhost") or t.startswith("127.0.0.1"):
        return "browser"
    if re.search(r"\.(sh|py|js|bat|ps1|cmd)\b", t) or t.startswith("./"):
        return "cli"
    return "desktop-gui"


def resolve_tests(query: str, tests: Sequence[Mapping]) -> List[str]:
    """Return test file names matching ``query`` (file, stem or spec name)."""
    q = query.strip().lower()
    if not q:
        return []
    exact = [t["file"] for t in tests
             if q in (t["file"].lower(), _test_stem(t["file"]), str(t.get("name", "")).lower())]
    if exact:
        return exact
    return [
        t["file"] for t in tests
        if q in t["file"].lower() or q in str(t.get("name", "")).lower()
    ]


def _test_stem(file_name: str) -> str:
    name = file_name.lower()
    for suffix in (".test.yaml", ".test.yml", ".yaml", ".yml"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


# --------------------------------------------------------------- slash ----

_SLASH_ALIASES = {
    "/?": "/help", "/commands": "/help", "/dry": "/dry-run", "/dryrun": "/dry-run",
    "/history": "/report", "/runs": "/report", "/provider": "/providers",
    "/model": "/providers", "/environment": "/env", "/why": "/explain",
    "/test": "/write", "/new": "/write",
}


def parse_slash(text: str, tests: Sequence[Mapping] = (), last_target: str = "") -> Optional[dict]:
    """Parse a ``/command``. Returns None when ``text`` is not a slash command."""
    text = text.strip()
    if not text.startswith("/"):
        return None
    head, _, rest = text.partition(" ")
    cmd = _SLASH_ALIASES.get(head.lower(), head.lower())
    rest = rest.strip()

    if cmd == "/help":
        return intent("help")
    if cmd == "/stop":
        return intent("stop")
    if cmd in ("/run", "/dry-run"):
        name = "run" if cmd == "/run" else "dry_run"
        if not rest or rest.lower() in ("all", "everything", "suite"):
            return intent(name, tests="all")
        if name == "dry_run" and rest.lower() in ("draft", "it"):
            return intent(name, tests="draft")
        matches = resolve_tests(rest, tests)
        if not matches:
            raise IntentError(f"No test matches '{rest}'. Try /run all, or check the Tests list.")
        return intent(name, tests=matches)
    if cmd == "/roam":
        return _parse_roam_args(rest, last_target)
    if cmd == "/write":
        if not rest:
            raise IntentError("Describe the test after /write, e.g. /write login works at http://localhost:3000/login")
        return intent("write_test", description=rest)
    if cmd == "/save":
        return intent("save_test")
    if cmd == "/explain":
        return intent("explain")
    if cmd == "/knowledge":
        words = rest.split(None, 1)
        if words and words[0].lower() in KNOWLEDGE_ACTIONS:
            return intent("knowledge", action=words[0].lower(), target=(words[1] if len(words) > 1 else "").strip())
        return intent("knowledge", action="show", target=rest)
    if cmd == "/evidence":
        return intent("evidence")
    if cmd == "/report":
        return intent("report")
    if cmd == "/tokens":
        return intent("tokens")
    if cmd == "/providers":
        if rest:
            return intent("switch_provider", provider=rest.lower())
        return intent("providers")
    if cmd == "/env":
        choice = rest.lower()
        if not choice:
            return intent("environment")
        if choice == "local":
            return intent("environment", environment="local")
        if choice in ("capsule", "auto"):
            return intent("environment", environment="capsule", capsule_provider="auto")
        if choice in ("hyperv", "hyper-v"):
            return intent("environment", environment="capsule", capsule_provider="hyperv")
        if choice in ("libvirt", "kvm"):
            return intent("environment", environment="capsule", capsule_provider="libvirt")
        raise IntentError("Use /env local, /env capsule, /env hyperv or /env libvirt.")
    if cmd == "/init":
        return intent("init")
    if cmd == "/watch":
        return intent("watch", action="stop" if rest.lower() == "stop" else "start")
    raise IntentError(f"Unknown command {head}. Type /help to see what Argus can do.")


# A token is a quoted run or any run of non-space characters. Backslashes are kept as
# typed, so Windows paths such as C:\Tools\app.exe survive (POSIX shlex would eat them).
_ROAM_TOKEN = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def _parse_roam_args(rest: str, last_target: str) -> dict:
    parts = _ROAM_TOKEN.findall(rest)
    # Argus's own options are --minutes/-m N, --adapter X and --[no-]memory; every other
    # token belongs to the target command, e.g. `/roam python tool.py --check`, and is
    # kept verbatim (quotes included) for the adapter to split.
    words, adapter, minutes, memory = [], None, None, None
    i = 0
    while i < len(parts):
        p = parts[i]
        if p in ("--minutes", "-m") and i + 1 < len(parts):
            minutes = _minutes(_unquote(parts[i + 1]))
            i += 2
            continue
        if p == "--adapter" and i + 1 < len(parts):
            adapter = _unquote(parts[i + 1])
            i += 2
            continue
        if p in ("--no-memory", "--memory"):
            memory = p == "--memory"
        else:
            words.append(p)
        i += 1
    if len(words) == 1:
        # One (possibly quoted) token is the whole target: "C:\Program Files\App\app.exe"
        # -> C:\Program Files\App\app.exe, so the executable name reaches the adapter bare.
        target = _unquote(words[0])
    else:
        # A multi-part command line keeps its quotes for the adapter to split.
        target = " ".join(words) if words else last_target
    if not target:
        raise IntentError('Tell Argus what to roam, e.g. /roam notepad.exe --minutes 5')
    adapter = adapter or adapter_for(target)
    if adapter not in ADAPTERS:
        raise IntentError(f"Unknown adapter {adapter!r}; use one of {', '.join(ADAPTERS)}.")
    return intent("roam", target=target, adapter=adapter, minutes=minutes, memory=memory)


def _minutes(value) -> Optional[float]:
    try:
        m = float(value)
    except (TypeError, ValueError):
        return None
    if m != m or m <= 0:  # NaN or non-positive
        return None
    return min(m, 240.0)


# ----------------------------------------------------------------- LLM ----

_ROUTER_SYSTEM = """You route messages for Argus, an autonomous application-testing tool.
Pick exactly ONE intent for the user's message and reply with a single JSON object:
{"intent": "<name>", "args": {...}}
No prose, no markdown fences.

Intents:
%s

Rules:
- Only use test files listed in the context.
- For "roam", copy the target (app name, command or URL) exactly as the user wrote it.
  If the user refers to the previous target ("roam it again"), use context.last_target.
- Use "chat" for questions you can answer in one or two sentences, or when the request
  is unclear; ask a short clarifying question in "reply".
"""


def router_prompt() -> str:
    lines = "\n".join(f"- {name}: {desc}" for name, desc in INTENTS.items())
    return _ROUTER_SYSTEM % lines


def route_with_llm(provider, text: str, context: Mapping) -> dict:
    """Ask the provider to classify ``text`` and validate its answer."""
    user = json.dumps({"message": text, "context": context}, ensure_ascii=False)
    response = provider.chat(router_prompt(), user)
    raw = extract_json(response.text)
    if raw is None:
        return intent("chat", reply=_plain(response.text) or "Sorry, I didn't catch that. Try /help.")
    return validate_intent(raw, text, context)


def extract_json(text: str) -> Optional[dict]:
    """Return the first JSON object in ``text`` (tolerates fences and prose)."""
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", text)
    start = cleaned.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for end in range(start, len(cleaned)):
            ch = cleaned[end]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(cleaned[start:end + 1])
                    except json.JSONDecodeError:
                        break
                    return value if isinstance(value, dict) else None
        start = cleaned.find("{", start + 1)
    return None


_ROAM_VERB = re.compile(r"\b(?:roam|explore)\b", re.IGNORECASE)
_DURATION_UNITS = {"s": 1 / 60, "m": 1.0, "h": 60.0}
_DURATION = (r"for\s+(?:(?P<n>\d+(?:\.\d+)?)\s*(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?)"
             r"|(?P<one>a\s+minute|an\s+hour))")
_MEMORY = r"(?P<mem>with|without)\s+memory"
_ENVIRONMENT = r"(?:(?P<local>locally)|in\s+(?:a\s+)?(?:(?P<cap>hyper-?v|libvirt)\s+)?capsule)"
# Argus-owned modifiers. After the target they are stripped from the end (repeatedly, in any
# order); before the roam verb they may appear anywhere. Their values always come from the
# user's own words, never from the model.
_ROAM_SUFFIXES = tuple(re.compile(r"\s+" + p + r"\s*[.!]?\s*$", re.IGNORECASE)
                       for p in (_DURATION, _MEMORY, _ENVIRONMENT))
_ROAM_PHRASES = tuple(re.compile(r"\b" + p + r"\b", re.IGNORECASE)
                      for p in (_DURATION, _MEMORY, _ENVIRONMENT))
# Words that signal a run modifier; left over after parsing, they make a request ambiguous.
_MODIFIER_WORDS = re.compile(
    r"\b(?:capsules?|locally|local|memory|hyper-?v|libvirt|seconds?|secs?|minutes?|mins?|hours?|hrs?)\b",
    re.IGNORECASE)
_DEICTIC_TARGET = re.compile(
    r"^(?:(?:it|that)(?:\s+again)?|again|(?:the\s+)?same(?:\s+(?:app|target))?|"
    r"(?:the\s+)?(?:previous|last)(?:\s+(?:app|target))?)[.!]?$", re.IGNORECASE)
_QUOTED_HEAD = re.compile(r'^(?P<q>["\'])(?P<body>.*?)(?P=q)(?P<tail>.*)$', re.DOTALL)


def _roam_modifier(m: "re.Match") -> dict:
    g = m.groupdict()
    if g.get("n") or g.get("one"):
        if g.get("one"):
            minutes = 60.0 if "hour" in g["one"].lower() else 1.0
        else:
            minutes = float(g["n"]) * _DURATION_UNITS[g["unit"][0].lower()]
        return {"minutes": _minutes(minutes)}
    if g.get("mem"):
        return {"memory": g["mem"].lower() == "with"}
    if g.get("local"):
        return {"environment": "local"}
    if g.get("cap"):  # "in a hyper-v capsule"; a plain "capsule" keeps the session's provider
        return {"environment": "capsule", "capsule_provider": g["cap"].lower().replace("-", "")}
    return {"environment": "capsule"}


class RoamRequest:
    """A free-text roam request read from the user's words: target, modifiers, or a problem."""

    def __init__(self, target: str, modifiers: dict, problem: Optional[str] = None) -> None:
        self.target = target
        self.modifiers = modifiers
        self.problem = problem


_QUOTE_HINT = ('Put the command in quotes to separate it, e.g. roam "python tool.py --flag" '
               "in a capsule for 5 minutes, or use /roam with --minutes and --memory/--no-memory "
               "and /env for the environment.")

_CLI_COMMAND_HEAD = re.compile(
    r"^(?:python(?:3)?|py|node|deno|bun|bash|sh|zsh|fish|pwsh|powershell|cmd|"
    r"git|npm|npx|pnpm|yarn|pytest|cargo|go|java|dotnet)\b",
    re.IGNORECASE,
)


def _looks_like_cli_target(target: str) -> bool:
    """Conservatively identify multi-part command lines that need quoting."""
    value = target.strip()
    return (
        adapter_for(value) == "cli"
        or bool(_CLI_COMMAND_HEAD.search(value))
        or bool(re.search(r"(?:^|\s)--?[A-Za-z0-9]", value))
        or bool(re.search(r"(?:&&|\|\||[|<>])", value))
    )


def _merge(into: dict, found: dict) -> Optional[str]:
    for key, value in found.items():
        if key in into and into[key] != value:
            return "You asked for two different settings for this roam, so I didn't start it. " + _QUOTE_HINT
        into[key] = value
    return None


def _strip_suffixes(candidate: str, modifiers: dict) -> tuple:
    """Strip trailing modifiers off ``candidate``. Returns (rest, stripped phrases, problem)."""
    stripped, problem = [], None
    changed = True
    while changed:
        changed = False
        for pattern in _ROAM_SUFFIXES:
            m = pattern.search(candidate)
            if m:
                problem = problem or _merge(modifiers, _roam_modifier(m))
                stripped.append(m.group(0).strip().rstrip(".!"))
                candidate = candidate[:m.start()].rstrip()
                changed = True
    return candidate, stripped, problem


def split_roam_request(text: str) -> Optional[RoamRequest]:
    """Read a free-text roam request deterministically from the user's words.

    The target follows the explicit roam/explore verb. A quoted target is taken literally,
    with modifiers allowed only after the closing quote. An unquoted target has trailing
    modifiers (duration, memory, environment) stripped, but only when what remains is a
    single token: stripping a modifier-shaped phrase off a multi-word command could cut off
    one of its arguments, so that is reported as a problem instead of guessed. Modifiers may
    also come before the verb ("In a capsule, roam notepad.exe"). Any modifier wording that
    can't be read unambiguously is a problem, so nothing runs with settings the user didn't
    choose. Returns None when the message has no roam/explore verb.
    """
    match = _ROAM_VERB.search(text)
    if not match:
        return None
    modifiers: dict = {}
    problem = None

    prefix = text[:match.start()]
    for pattern in _ROAM_PHRASES:
        for m in pattern.finditer(prefix):
            problem = problem or _merge(modifiers, _roam_modifier(m))
        prefix = pattern.sub(" ", prefix)
    leftover = _MODIFIER_WORDS.search(prefix)
    if leftover and not problem:
        problem = f'I couldn\'t tell how "{leftover.group(0)}" should apply to this roam. ' + _QUOTE_HINT

    rest = text[match.end():].strip()
    quoted = _QUOTED_HEAD.match(rest)
    if quoted:
        target = quoted.group("body").strip()
        tail, _, tail_problem = _strip_suffixes(" " + quoted.group("tail").strip(), modifiers)
        problem = problem or tail_problem
        if tail.strip(" .!") and not problem:
            problem = (f'I couldn\'t read "{tail.strip()}" after the quoted command. ' + _QUOTE_HINT)
        return RoamRequest(target, modifiers, problem)

    target, stripped, suffix_problem = _strip_suffixes(rest, modifiers)
    problem = problem or suffix_problem
    target = _unquote(target)
    deictic = bool(_DEICTIC_TARGET.match(target))
    if not problem and not deictic:
        if stripped and re.search(r"\s", target) and _looks_like_cli_target(target):
            problem = (f'I wasn\'t sure whether "{stripped[-1]}" is part of the command '
                       f'"{target}". ' + _QUOTE_HINT)
        elif (re.search(r"\s", target) and _looks_like_cli_target(target)
              and any(p.search(target) for p in _ROAM_PHRASES)):
            problem = ("Part of that command reads like a run setting, so I didn't guess. " + _QUOTE_HINT)
    return RoamRequest(target, modifiers, problem)


def run_settings_from_text(text: str) -> tuple:
    """Execution settings explicitly and positively requested in free text.

    Negated/ambiguous environment wording is never inverted into an override. A
    Local override is accepted only as an execution adverb ("run X locally" or
    "locally, run X"), which avoids treating a test name containing "locally" as
    an execution setting.
    """
    settings: dict = {}
    problem = None

    negated_environment = re.search(
        r"\b(?:don'?t|do\s+not|never)\b[^.;!?]{0,80}"
        r"(?:locally|local\s+(?:host|machine)|in\s+(?:a\s+)?(?:hyper-?v\s+|libvirt\s+)?capsule)\b",
        text,
        re.IGNORECASE,
    )
    if negated_environment:
        return {}, (
            "I won't infer an execution environment from a negated instruction. "
            "Keep the session picker as-is, or choose one explicitly with /env."
        )

    local_positive = (
        re.search(
            r"\b(?:run|execute|test|check)\b[^.;!?]{0,120}\s+locally\s*[.!?]?\s*$",
            text,
            re.IGNORECASE,
        )
        or re.search(
            r"^\s*locally\s*[,;:]?\s*(?:please\s+)?(?:run|execute|test|check)\b",
            text,
            re.IGNORECASE,
        )
    )
    if local_positive:
        settings["environment"] = "local"

    # Capsule wording is sufficiently explicit to parse directly, but never after
    # a negation (handled above).
    for m in re.finditer(
        r"\bin\s+(?:a\s+)?(?:(?P<cap>hyper-?v|libvirt)\s+)?capsule\b",
        text,
        re.IGNORECASE,
    ):
        found = {"environment": "capsule"}
        if m.group("cap"):
            found["capsule_provider"] = m.group("cap").lower().replace("-", "")
        problem = problem or _merge(settings, found)

    if re.search(r"\b(?:don'?t|do\s+not|without)\s+(?:keep(?:ing)?|retain(?:ing)?)\s+(?:the\s+)?"
                 r"failure\s+capsule\b", text, re.IGNORECASE):
        settings["retain"] = False
    elif re.search(r"\b(?:keep(?:ing)?|retain(?:ing)?)\s+(?:the\s+)?failure\s+capsule\b", text, re.IGNORECASE):
        settings["retain"] = True
    if "retain" in settings and "environment" not in settings:
        settings["environment"] = "capsule"
    if problem:
        problem = "You asked for both Local and a Capsule, so I didn't run anything. Pick one, e.g. /env capsule."
    return settings, problem


def _question_about_action(text: str) -> bool:
    """Questions about a feature are not authorization to perform it."""
    return bool(re.match(r"^\s*(?:what|which|why|how|when|where)\b", text, re.IGNORECASE)
                or re.match(r"^\s*(?:do|should|would|could|can)\s+(?:i|we)\b", text, re.IGNORECASE))


def _run_scope_from_text(text: str, tests: Sequence[Mapping]) -> tuple:
    """Return the test scope explicitly authorized by the user's own words."""
    if _question_about_action(text):
        return None, "I can describe the tests, but I won't execute them unless you explicitly ask me to run one."

    executes = bool(
        re.search(r"\b(?:run|execute|rerun|re-run)\b", text, re.IGNORECASE)
        or re.search(
            r"(?:^|\b(?:please|can\s+you|could\s+you|would\s+you|i\s+want\s+you\s+to)\s+)"
            r"(?:test|check)\b",
            text,
            re.IGNORECASE,
        )
    )
    if not executes:
        return None, "I won't execute tests unless you explicitly ask me to run, execute, test, or check them."

    if re.search(r"\b(?:all\s+(?:the\s+)?tests?|every\s+test|everything|(?:whole|full)\s+suite)\b",
                 text, re.IGNORECASE):
        return "all", None

    lowered = text.casefold()
    chosen = []
    for item in tests:
        aliases = {
            str(item.get("file") or "").casefold(),
            _test_stem(str(item.get("file") or "")),
            str(item.get("name") or "").casefold(),
        }
        aliases.discard("")
        if any(re.search(r"(?<![\w-])" + re.escape(alias) + r"(?![\w-])", lowered)
               for alias in sorted(aliases, key=len, reverse=True)):
            chosen.append(str(item["file"]))
    if not chosen:
        return None, "Which test should I run? Name a test from the sidebar, or say 'run all tests'."
    return list(dict.fromkeys(chosen)), None


def _authorized_simple_action(text: str, action: str) -> bool:
    if _question_about_action(text):
        return False
    patterns = {
        "stop": r"\b(?:stop|cancel|abort)\b",
        "save_test": r"\b(?:save|write)\b[^.!?]{0,80}\b(?:draft|test|spec|it)\b|^\s*(?:please\s+)?save\b",
        "init": r"\b(?:init|initialize|initialise|setup|set\s+up|scaffold)\b[^.!?]{0,80}\b(?:argus|project|workspace)\b",
        "write_test": r"\b(?:write|draft|create|make)\b[^.!?]{0,80}\b(?:test|spec)\b",
    }
    return bool(re.search(patterns[action], text, re.IGNORECASE))


def _watch_action_from_text(text: str) -> Optional[str]:
    if _question_about_action(text):
        return None
    if re.search(r"\b(?:stop|disable|turn\s+off)\b[^.!?]{0,40}\bwatch\b|"
                 r"\bwatch\b[^.!?]{0,40}\b(?:stop|off)\b", text, re.IGNORECASE):
        return "stop"
    if re.search(r"\b(?:start|enable|turn\s+on)\b[^.!?]{0,40}\bwatch\b|"
                 r"^\s*(?:please\s+)?watch\b|"
                 r"\b(?:watch|monitor)\b[^.!?]{0,60}\b(?:tests?|files?|specs?|changes)\b",
                 text, re.IGNORECASE):
        return "start"
    return None


def _mentions_target(text: str, target: str, adapter: str) -> bool:
    """Verify that the routed target preserves the user's complete roam target.

    Free-text execution is deliberately conservative: after the explicit roam/explore
    verb, the target must equal the remaining user text after removing only Argus-owned
    modifiers (duration, memory and execution environment). This prevents a model from
    silently dropping command arguments, flags, or URL query strings.
    """
    del adapter  # kept in the signature so validation is explicit about the routed adapter
    request = split_roam_request(text)
    if not target or request is None or request.problem:
        return False
    candidate = request.target
    wanted = target.casefold()
    if candidate.casefold() == wanted:
        return True
    # Permit ordinary sentence punctuation only when removing it yields the exact
    # target. A URL query marker is never treated as punctuation here.
    return bool(candidate.endswith((".", "!")) and candidate[:-1].casefold() == wanted)


def _refers_to_last_target(text: str) -> bool:
    """Require an explicit command-like reference before reusing the previous target."""
    return bool(re.search(
        r"\b(?:roam|explore)\s+(?:(?:it|that)(?:\s+again)?|again|"
        r"(?:the\s+)?same(?:\s+(?:app|target))?|"
        r"(?:the\s+)?(?:previous|last)(?:\s+(?:app|target))?)\b",
        text,
        re.IGNORECASE,
    ))

def validate_intent(raw: Mapping, text: str, context: Mapping) -> dict:
    """Turn a model's routing answer into a safe intent, or a chat reply."""
    name = str(raw.get("intent", "")).strip()
    args = raw.get("args") if isinstance(raw.get("args"), Mapping) else {}
    tests = context.get("tests") or []
    if name not in INTENTS:
        return intent("chat", reply="I'm not sure what to do with that. Type /help to see what Argus can do.")

    if name in ("help", "explain", "evidence", "report", "tokens", "providers"):
        return intent(name)

    if name in ("stop", "save_test", "init"):
        if _authorized_simple_action(text, name):
            return intent(name)
        return intent("chat", reply="I won't perform that action unless you explicitly ask for it.")

    if name in ("run", "dry_run"):
        wanted = args.get("tests", "all")
        if wanted != "all" and not (name == "dry_run" and wanted == "draft"):
            names = [str(n) for n in (wanted if isinstance(wanted, list) else [wanted])]
            known = {t["file"] for t in tests}
            missing = []
            for n in names:
                if n not in known and not resolve_tests(n, tests):
                    missing.append(n)
            if missing:
                return intent("chat", reply=(
                    f"I couldn't find {', '.join(missing)} in .argus/, so I didn't run anything. "
                    "The Tests list in the sidebar shows what's there."))

        if name == "run":
            chosen, authorization_problem = _run_scope_from_text(text, tests)
            if authorization_problem:
                return intent("chat", reply=authorization_problem)
            settings, problem = run_settings_from_text(text)
            if problem:
                return intent("chat", reply=problem)
            out = intent("run", tests=chosen)
            out["args"].update(settings)
            return out

        if wanted == "all" or wanted == "draft":
            chosen = wanted
        else:
            names = [str(n) for n in (wanted if isinstance(wanted, list) else [wanted])]
            chosen = []
            for n in names:
                chosen.extend([n] if n in {t["file"] for t in tests} else resolve_tests(n, tests))
            chosen = list(dict.fromkeys(chosen))
        return intent("dry_run", tests=chosen)

    if name == "roam":
        target = _unquote(str(args.get("target") or "").strip())
        last = str(context.get("last_target") or "")
        adapter = adapter_for(target)
        request = split_roam_request(text)
        if request is not None and request.problem:
            return intent("chat", reply=request.problem)
        explicitly_named = _mentions_target(text, target, adapter)
        reuses_previous = bool(last and target == last and _refers_to_last_target(text))
        if not target or not (explicitly_named or reuses_previous):
            return intent("chat", reply="Which app should I roam? Give me the command, file or URL, e.g. roam notepad.exe for 5 minutes.")
        # Duration, memory and environment come from the user's own words, never from the
        # model: a dropped "in a capsule" must not turn into a local roam. Anything the user
        # didn't say stays None, so the session's pickers and project defaults apply.
        mods = request.modifiers if request is not None else {}
        return intent(
            "roam", target=target, adapter=adapter, minutes=mods.get("minutes"),
            memory=mods.get("memory"),
            **{k: mods[k] for k in ("environment", "capsule_provider") if k in mods},
        )

    if name == "write_test":
        if not _authorized_simple_action(text, "write_test"):
            return intent("chat", reply="Tell me explicitly to write or draft a test, and what it should cover.")
        description = str(args.get("description") or text).strip()
        return intent("write_test", description=description[:2000])

    if name == "knowledge":
        action = args.get("action") if args.get("action") in KNOWLEDGE_ACTIONS else "show"
        if action in ("reset", "export"):
            verb = r"(?:reset|clear|forget)" if action == "reset" else r"export"
            if _question_about_action(text) or not (
                re.search(verb + r"\b[^.!?]{0,60}\bknowledge\b", text, re.IGNORECASE)
                or re.search(r"\bknowledge\b[^.!?]{0,60}" + verb + r"\b", text, re.IGNORECASE)
            ):
                return intent("chat", reply=f"I won't {action} knowledge unless you explicitly ask me to.")
        return intent("knowledge", action=action, target=str(args.get("target") or "").strip())

    if name == "switch_provider":
        wanted = str(args.get("provider") or "").lower().strip()
        configured = [str(p).lower() for p in context.get("providers") or []]
        named = next((p for p in configured if re.search(
            r"(?<![\w-])" + re.escape(p) + r"(?![\w-])", text, re.IGNORECASE)), None)
        authorized = bool(named and re.search(
            r"\b(?:use|switch|change|select|choose)\b[^.!?]{0,80}\b(?:provider|model)?\b",
            text,
            re.IGNORECASE,
        ))
        if not authorized or named != wanted:
            return intent("chat", reply="Name the configured provider you want me to switch to explicitly.")
        if wanted not in configured:
            return intent("chat", reply=f"'{wanted or '?'}' isn't configured in .argus/config.yaml. Configured: {', '.join(configured) or 'none'}.")
        return intent("switch_provider", provider=wanted)

    if name == "environment":
        settings, problem = run_settings_from_text(text)
        if not problem and "environment" not in settings:
            # "switch to local" / "use a hyper-v capsule": read the choice from the user's words.
            words = {w.lower().replace("-", "") for w in re.findall(
                r"\b(?:local|capsule|hyper-?v|libvirt)\b", text, re.IGNORECASE)}
            if words == {"local"}:
                settings["environment"] = "local"
            elif words and "local" not in words:
                cap = next((w for w in ("hyperv", "libvirt") if w in words), "auto")
                settings.update(environment="capsule", capsule_provider=cap)
        if problem or "environment" not in settings:
            return intent("chat", reply=problem or "Local or a Capsule? Try /env local, /env capsule, /env hyperv or /env libvirt.")
        if settings["environment"] == "capsule":
            settings.setdefault("capsule_provider", "auto")  # as /env capsule does
        return intent("environment", **settings)

    if name == "watch":
        action = _watch_action_from_text(text)
        if action is None:
            return intent("chat", reply="Say 'start watch' to watch tests, or 'stop watch' to stop it.")
        return intent("watch", action=action)

    return intent("chat", reply=_plain(str(args.get("reply") or "")) or "Type /help to see what Argus can do.")


def _plain(text: str, limit: int = 1500) -> str:
    text = re.sub(r"```.*?```", "", text or "", flags=re.S).strip()
    return text[:limit]


# ------------------------------------------------------------ drafting ----

_DRAFT_SYSTEM = """You write Argus test specs (.test.yaml). Reply with the YAML only, no fences.

Format:
name: <short name>
target:
  adapter: desktop-gui | browser | cli
  launch: <executable, URL or command>
retries: 1
steps:
  - "<natural-language step a user would do>"
  - assert:
      <assertion>: <expected>
teardown:
  - close

Assertions (deterministic, never judged by the model):
%s
desktop-gui: text_visible, window_title_contains, element_exists {name, control_type}, process_running, dialog_open
browser: url_contains, page_title_contains, text_visible
cli: exit_code_is, stdout_contains, stderr_contains

Rules: keep 2-8 steps; assert the outcome the user cares about; use placeholders like
"[EXPECTED TEXT]" for facts you don't know; never add staging or collect sections."""


def draft_spec(provider, description: str, tests: Iterable[str] = ()) -> dict:
    """Draft a spec from ``description``. Returns {ok, yaml, file, name, adapter, steps, error}."""
    system = _DRAFT_SYSTEM % ", ".join(ASSERTION_KINDS)
    response = provider.chat(system, description)
    text = _strip_fences(response.text)
    return check_draft(text, tests)


def check_draft(text: str, existing: Iterable[str] = ()) -> dict:
    """Validate drafted YAML with the real spec parser."""
    out = {"ok": False, "yaml": text, "file": "", "name": "", "adapter": "", "steps": [], "error": None}
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        out["error"] = f"invalid YAML: {exc}"
        return out
    if isinstance(data, dict) and ("staging" in data or "collect" in data):
        out["error"] = "drafts may not declare staging or collect sections"
        return out
    try:
        spec = parse_spec(text)
    except SpecError as exc:
        out["error"] = str(exc)
        return out
    except SPEC_ERRORS as exc:
        out["error"] = f"invalid spec: {exc}"
        return out
    if spec.adapter not in ADAPTERS:
        out["error"] = f"unknown adapter {spec.adapter!r}"
        return out
    out.update(
        ok=True, name=spec.name, adapter=spec.adapter, launch=spec.launch,
        file=unique_file_name(slug(spec.name) + ".test.yaml", existing),
        steps=describe_steps(spec.steps),
    )
    return out


def describe_steps(steps) -> List[dict]:
    from argus.engine.spec import AssertStep

    return [
        {"kind": step.kind, "text": step.describe() if isinstance(step, AssertStep) else step.text}
        for step in steps
    ]


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return s[:48].strip("-") or "new-test"


def unique_file_name(name: str, existing: Iterable[str]) -> str:
    taken = {e.lower() for e in existing}
    if name.lower() not in taken:
        return name
    stem = name[: -len(".test.yaml")]
    n = 2
    while f"{stem}-{n}.test.yaml".lower() in taken:
        n += 1
    return f"{stem}-{n}.test.yaml"


def _strip_fences(text: str) -> str:
    m = re.search(r"```(?:ya?ml)?\s*\n(.*?)```", text or "", flags=re.S)
    return (m.group(1) if m else (text or "")).strip() + "\n"


# ------------------------------------------------------------ explain ----

_EXPLAIN_SYSTEM = """You explain Argus test failures to a developer.
Given one run result as JSON, say in plain prose (at most 120 words, no markdown):
which step failed, what was expected versus what happened, whether it looks like a
product bug, a flaky environment or a problem in the test itself, and one concrete next step.
Assertions are deterministic: never claim the model judged them."""


def explain_failure(provider, result: Mapping) -> str:
    compact = {
        "test": result.get("test_file"),
        "status": result.get("status"),
        "error": result.get("error"),
        "environment": result.get("environment_type"),
        "steps": [
            {k: s.get(k) for k in ("index", "kind", "text", "status", "expected", "actual", "note", "actions")}
            for s in result.get("steps", [])
        ],
    }
    response = provider.chat(_EXPLAIN_SYSTEM, json.dumps(compact, ensure_ascii=False))
    return _plain(response.text, limit=2000)
