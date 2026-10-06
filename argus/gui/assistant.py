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
import math
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
    head = _ROAM_TOKEN.match(t)
    executable = _unquote(head.group()).replace("\\", "/").rsplit("/", 1)[-1] if head else ""
    if executable.endswith(".exe"):
        executable = executable[:-4]
    if re.match(r"^https?://", t) or t.startswith("localhost") or t.startswith("127.0.0.1"):
        return "browser"
    if (re.search(r"\.(sh|py|js|bat|ps1|cmd)\b", t) or t.startswith("./")
            or _CLI_COMMAND_HEAD.match(t)
            or _CLI_COMMAND_HEAD.fullmatch(executable)
            or _CLI_COMMAND_HEAD.fullmatch(t.replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".exe"))
            or re.match(r"^/(?:[^\s/]+/)*[^\s/.]+(?:\s|$)", t)):
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


# Commands that need neither the project config nor the test list, so they work
# even while .argus/config.yaml or a spec is broken (e.g. stopping a running job).
_PROJECT_FREE_COMMANDS = ("/help", "/stop")


def parse_project_free_slash(text: str) -> Optional[dict]:
    """Parse ``/stop`` and ``/help`` without project state; None for anything else."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    head = text.partition(" ")[0].lower()
    if _SLASH_ALIASES.get(head, head) not in _PROJECT_FREE_COMMANDS:
        return None
    return parse_slash(text)


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
        rest, settings = _run_flags(rest) if name == "run" else (rest, {})
        if not rest or rest.lower() in ("all", "everything", "suite"):
            return intent(name, tests="all", **settings)
        if name == "dry_run" and rest.lower() in ("draft", "it"):
            return intent(name, tests="draft")
        known = {str(t["file"]).casefold(): str(t["file"]) for t in tests}
        words = rest.split()
        if len(words) > 1 and all(w.casefold() in known for w in words):
            # Several complete file names: exactly those tests, in the order given (repeats too).
            return intent(name, tests=[known[w.casefold()] for w in words], **settings)
        matches = resolve_tests(rest, tests)
        if not matches:
            raise IntentError(f"No test matches '{rest}'. Try /run all, or check the Tests list.")
        if len(matches) > 1:
            # A slash command runs what it names; a partial or shared name is not a choice.
            raise IntentError(f"'{rest}' matches {len(matches)} tests ({', '.join(matches)}). "
                              f"Name one by its file, e.g. /run {matches[0]}, or use /run all.")
        return intent(name, tests=matches, **settings)
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
        words = rest.lower().split()
        retain = None
        for flag in [w for w in words if w in ("--retain", "--no-retain")]:
            if retain is not None and retain != (flag == "--retain"):
                raise IntentError("Choose either --retain or --no-retain.")
            retain = flag == "--retain"
        words = [w for w in words if w not in ("--retain", "--no-retain")]
        if len(words) > 1:
            raise IntentError("Use /env local, /env capsule, /env hyperv or /env libvirt.")
        choice = words[0] if words else ""
        extra = {} if retain is None else {"retain": retain}
        if not choice:
            if extra:
                # Failure retention only applies to Capsule runs.
                return intent("environment", environment="capsule", **extra)
            return intent("environment")
        if choice == "local":
            if extra:
                raise IntentError("--retain keeps a failed Capsule; it doesn't apply to /env local.")
            return intent("environment", environment="local")
        settings = _ENV_CHOICES.get(choice)
        if settings is None:
            raise IntentError("Use /env local, /env capsule, /env hyperv or /env libvirt.")
        return intent("environment", **settings, **extra)
    if cmd == "/init":
        return intent("init")
    if cmd == "/watch":
        action = rest.lower()
        if action not in ("", "start", "stop"):
            raise IntentError("Use /watch, /watch start or /watch stop.")
        return intent("watch", action=action or "start")
    raise IntentError(f"Unknown command {head}. Type /help to see what Argus can do.")


_ENV_CHOICES = {
    "capsule": {"environment": "capsule", "capsule_provider": "auto"},
    "auto": {"environment": "capsule", "capsule_provider": "auto"},
    "hyperv": {"environment": "capsule", "capsule_provider": "hyperv"},
    "hyper-v": {"environment": "capsule", "capsule_provider": "hyperv"},
    "libvirt": {"environment": "capsule", "capsule_provider": "libvirt"},
    "kvm": {"environment": "capsule", "capsule_provider": "libvirt"},
}


def _run_flags(rest: str) -> tuple:
    """Split ``/run`` execution flags (--env X, --retain/--no-retain) from the test names."""
    words, settings = [], {}
    parts = rest.split()
    i = 0
    while i < len(parts):
        part = parts[i].lower()
        if part == "--env":
            if i + 1 >= len(parts):
                raise IntentError("--env needs local, capsule, hyperv or libvirt.")
            choice = parts[i + 1].lower()
            chosen = {"environment": "local"} if choice == "local" else _ENV_CHOICES.get(choice)
            if chosen is None:
                raise IntentError("--env needs local, capsule, hyperv or libvirt.")
            if any(settings.get(k, v) != v for k, v in chosen.items()):
                raise IntentError("Choose one environment for this run.")
            settings.update(chosen)
            i += 2
            continue
        if part in ("--retain", "--no-retain"):
            if settings.get("retain", part == "--retain") != (part == "--retain"):
                raise IntentError("Choose either --retain or --no-retain.")
            settings["retain"] = part == "--retain"
        else:
            words.append(parts[i])
        i += 1
    if settings.get("retain") is not None and settings.get("environment") == "local":
        raise IntentError("--retain keeps a failed Capsule; it doesn't apply to --env local.")
    return " ".join(words), settings


# --------------------------------------------------------- confirmation ----

# Free text never changes anything by itself. These intents come back as a proposal that
# shows the equivalent slash command; only a click on it, or typing the command, acts.
# Reading, explaining and drafting (a draft is never saved without /save) stay direct.
CONFIRM_INTENTS = ("run", "roam", "stop", "save_test", "init", "switch_provider", "watch", "providers")


def needs_confirmation(routed: Mapping) -> bool:
    name, args = routed.get("intent"), routed.get("args") or {}
    if name in CONFIRM_INTENTS:
        return True
    if name == "environment":
        return any(k in args for k in ("environment", "capsule_provider", "retain"))
    return name == "knowledge" and args.get("action") in ("reset", "export")


def _slash_token(value: str) -> Optional[str]:
    """``value`` as one /roam token, or None when no quoting can carry it unchanged."""
    if value and not re.search(r"\s|[\"']", value) and not value.startswith("-"):
        return value
    for quote in ('"', "'"):
        if quote not in value:
            return quote + value + quote
    return None


def to_slash(routed: Mapping) -> Optional[str]:
    """The slash command that does exactly what ``routed`` does, or None if none can."""
    name, a = routed.get("intent"), routed.get("args") or {}
    if name == "run":
        tests = a.get("tests", "all")
        if tests != "all" and len(tests) > 1 and any(re.search(r"\s", t) for t in tests):
            return None  # several names, one with a space: no unambiguous /run form
        parts = ["/run", "all" if tests == "all" else " ".join(tests)]
        env = a.get("environment")
        if env == "local":
            parts.append("--env local")
        elif env == "capsule":
            parts.append("--env " + {"hyperv": "hyperv", "libvirt": "libvirt"}.get(a.get("capsule_provider"), "capsule"))
        if isinstance(a.get("retain"), bool):
            parts.append("--retain" if a["retain"] else "--no-retain")
        return " ".join(parts)
    if name == "roam":
        target = _slash_token(str(a.get("target") or ""))
        if target is None:
            return None
        parts = ["/roam", target, "--adapter", str(a.get("adapter") or adapter_for(str(a.get("target") or "")))]
        if a.get("minutes") is not None:
            parts += ["--minutes", f"{float(a['minutes']):g}"]
        if isinstance(a.get("memory"), bool):
            parts.append("--memory" if a["memory"] else "--no-memory")
        if a.get("environment") == "local":
            parts.append("--env local")
        elif a.get("environment") == "capsule":
            parts.append("--env " + {"hyperv": "hyperv", "libvirt": "libvirt"}.get(a.get("capsule_provider"), "capsule"))
        return " ".join(parts)
    if name == "environment":
        if a.get("environment") == "local":
            command = "/env local"
        elif a.get("environment") == "capsule":
            command = "/env " + {"hyperv": "hyperv", "libvirt": "libvirt"}.get(a.get("capsule_provider"), "capsule")
        else:
            command = "/env"
        if isinstance(a.get("retain"), bool):
            command += " --retain" if a["retain"] else " --no-retain"
        return command
    if name == "knowledge":
        return f"/knowledge {a.get('action', 'show')} {a.get('target') or ''}".rstrip()
    if name == "switch_provider":
        return f"/providers {a.get('provider', '')}".rstrip()
    if name == "watch":
        return f"/watch {a.get('action', 'start')}"
    return {"stop": "/stop", "save_test": "/save", "init": "/init", "providers": "/providers"}.get(name)


def _describe(routed: Mapping) -> str:
    name, a = routed.get("intent"), routed.get("args") or {}
    if name == "run":
        tests = a.get("tests", "all")
        return "Run all tests" if tests == "all" else "Run " + ", ".join(tests)
    if name == "roam":
        minutes = f" for {float(a['minutes']):g} minutes" if a.get("minutes") is not None else ""
        return f"Roam {a.get('target', '')}{minutes}"
    if name == "environment":
        return {"local": "Run tests locally"}.get(a.get("environment"), "Run tests in a Capsule") \
            if a.get("environment") else "Change failure-capsule retention"
    if name == "knowledge":
        return f"{a.get('action', 'show').capitalize()} the knowledge for {a.get('target') or 'the last target'}"
    if name == "watch":
        return "Stop watching tests" if a.get("action") == "stop" else "Watch tests and re-run them on change"
    if name == "switch_provider":
        return f"Switch the model provider to {a.get('provider', '')}"
    return {"stop": "Stop the current job", "save_test": "Save the draft to .argus",
            "init": "Set up Argus in this project", "providers": "Check the provider connection"}.get(name, name)


def confirmation(routed: dict) -> dict:
    """Turn a state-changing free-text intent into a proposal; pass everything else through."""
    if not needs_confirmation(routed):
        return routed
    return intent("confirm", proposed=routed, command=to_slash(routed), summary=_describe(routed))


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
    words, adapter, minutes, memory, env = [], None, None, None, {}
    i = 0
    while i < len(parts):
        p = parts[i]
        if p in ("--minutes", "-m", "--adapter"):
            if i + 1 >= len(parts) or parts[i + 1].startswith("--"):
                raise IntentError(f"{p} needs a value. Use /roam <target> --minutes 5 --adapter cli.")
            value = _unquote(parts[i + 1])
            if p == "--adapter":
                if value not in ADAPTERS:
                    raise IntentError(f"Unknown adapter; use one of {', '.join(ADAPTERS)}.")
                if adapter is not None and adapter != value:
                    raise IntentError("Choose one adapter for this roam.")
                adapter = value
            else:
                try:
                    duration = float(value)
                except (TypeError, ValueError):
                    raise IntentError("--minutes needs a number greater than 0 and at most 240.") from None
                if not 0 < duration <= 240:
                    raise IntentError("--minutes needs a finite number greater than 0 and at most 240.")
                if minutes is not None and minutes != duration:
                    raise IntentError("Choose one duration for this roam.")
                minutes = duration
            i += 2
            continue
        if p == "--env":
            if i + 1 >= len(parts):
                raise IntentError("--env needs local, capsule, hyperv or libvirt.")
            choice = _unquote(parts[i + 1]).lower()
            chosen = {"environment": "local"} if choice == "local" else _ENV_CHOICES.get(choice)
            if chosen is None:
                raise IntentError("--env needs local, capsule, hyperv or libvirt.")
            if env and env != chosen:
                raise IntentError("Choose one environment for this roam.")
            env = chosen
            i += 2
            continue
        if p.startswith(("--minutes=", "--adapter=", "-m=", "--env=")):
            raise IntentError("Separate the option and value with a space, e.g. --minutes 5 --adapter cli.")
        if p in ("--no-memory", "--memory"):
            if memory is not None and memory != (p == "--memory"):
                raise IntentError("Choose either --memory or --no-memory for this roam.")
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
    return intent("roam", target=target, adapter=adapter, minutes=minutes, memory=memory, **env)


def _minutes(value) -> Optional[float]:
    try:
        m = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(m) or not 0 < m <= 240:
        return None
    return m


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
_DURATION = (r"for\s+(?:(?P<n>[-+]?(?:\d+(?:\.\d+)?|\.\d+))\s*(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?)"
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
# The same refusal forms run, watch and the simple actions reject ("no need to",
# "should not", "would rather not", ...), anywhere before the roam verb in the clause.
_REFUSAL = r"\b(?:don'?t|do\s+not|never|not|no|avoid|can'?t|cannot|won'?t|shouldn'?t|mustn'?t|without)\b"
_ROAM_REFUSAL = re.compile(_REFUSAL + r"[^.;!?]*\b(?:roam|explore)\b", re.IGNORECASE)
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
    if "minutes" in found and found["minutes"] is None:
        return "Roam duration must be greater than zero, finite and at most 240 minutes. Choose a supported duration, e.g. for 5 minutes."
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
                # "roam notepad.exe, for 5 minutes": the separator comma goes with the modifier.
                if candidate.endswith(","):
                    candidate = candidate[:-1].rstrip()
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
    # A roam verb inside reported speech ("the docs say: roam X") isn't the user's request.
    match = _ROAM_VERB.search(_without_reported_speech(text))
    if not match:
        return None
    modifiers: dict = {}
    problem = None

    prefix = text[:match.start()]
    for pattern in _ROAM_PHRASES:
        for m in pattern.finditer(prefix):
            problem = problem or _merge(modifiers, _roam_modifier(m))
        prefix = pattern.sub(" ", prefix)
    # Checked after the modifiers are removed, so "without memory, roam X" is not a refusal.
    if _ROAM_REFUSAL.search(prefix.replace("\u2019", "'") + match.group(0)):
        return RoamRequest("", {}, "I won't roam a target when the instruction is negated.")
    # The verb must be asked of Argus ("roam X", "please explore X", "in a capsule, roam X"),
    # not part of a description ("the CI will roam X").
    if not re.search(r"(?:" + _REQUEST_LEAD + r")" + _REQUEST_ADVERBS + r"\s*$", prefix, re.IGNORECASE):
        return None
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


# Refusal words for execution settings ("not locally", "would rather not keep ...").
# Hyphenated forms such as a "no-login" test name are not refusals.
_SETTING_REFUSAL = (r"(?<![\w-])(?:don'?t|do\s+not|never|not|no|avoid(?:ing)?|can'?t|cannot|won'?t|"
                    r"shouldn'?t|mustn'?t|without)(?![\w-])")


# "on this machine", "on my computer", "on the local host": Local, said in other words.
_ON_THIS_MACHINE = (r"(?:on\s+(?:this|my|the\s+local|the\s+same)\s+(?:machine|computer|pc|device|host|box)|"
                    r"on\s+the\s+local\s*host)")


def run_settings_from_text(text: str) -> tuple:
    """Execution settings explicitly and positively requested in free text.

    Negated/ambiguous environment wording is never inverted into an override. A
    Local override is accepted only as an execution adverb ("run X locally" or
    "locally, run X"), which avoids treating a test name containing "locally" as
    an execution setting.
    """
    settings: dict = {}
    problem = None
    # Quoted titles and filenames are data; curly apostrophes are still apostrophes. Settings
    # quoted from elsewhere ("the README says to run it in a capsule") aren't the user's.
    words = _without_literals(_without_reported_speech(text)).replace("\u2019", "'")

    negated_environment = re.search(
        _SETTING_REFUSAL + r"[^.;!?]{0,80}"
        r"(?:locally|local(?![\w-])(?:\s+(?:host|machine))?|" + _ON_THIS_MACHINE + r"|"
        r"in\s+(?:a\s+)?(?:hyper-?v\s+|libvirt\s+)?capsule)\b",
        words,
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
            words,
            re.IGNORECASE,
        )
        or re.search(
            r"^\s*locally\s*[,;:]?\s*(?:please\s+)?(?:run|execute|test|check)\b",
            words,
            re.IGNORECASE,
        )
        or re.search(r"\b(?:run|execute|test|check)\b[^.;!?]{0,120}" + _ON_THIS_MACHINE + r"\b",
                     words, re.IGNORECASE)
    )
    if local_positive:
        settings["environment"] = "local"

    # Capsule wording is sufficiently explicit to parse directly, but never after
    # a negation (handled above).
    for m in re.finditer(
        r"\bin\s+(?:a\s+)?(?:(?P<cap>hyper-?v|libvirt)\s+)?capsule\b",
        words,
        re.IGNORECASE,
    ):
        found = {"environment": "capsule"}
        if m.group("cap"):
            found["capsule_provider"] = m.group("cap").lower().replace("-", "")
        problem = problem or _merge(settings, found)

    if re.search(_SETTING_REFUSAL + r"[^.;!?]{0,40}\b(?:keep(?:ing)?|retain(?:ing)?)\s+(?:the\s+)?"
                 r"failure\s+capsule\b", words, re.IGNORECASE):
        settings["retain"] = False
    elif re.search(r"\b(?:keep(?:ing)?|retain(?:ing)?)\s+(?:the\s+)?failure\s+capsule\b", words, re.IGNORECASE):
        settings["retain"] = True
    if "retain" in settings and "environment" not in settings:
        settings["environment"] = "capsule"
    if problem:
        problem = "You asked for both Local and a Capsule, so I didn't run anything. Pick one, e.g. /env capsule."
    return settings, problem


# Verbs that name an action Argus can take; text before the first one frames the request.
_ACTION_VERB = re.compile(
    r"\b(?:run|execute|rerun|re-run|stop|cancel|abort|save|persist|init|initiali[sz]e|set\s+up|setup|"
    r"scaffold|write|draft|create|roam|explore|switch|select|choose|start|watch|enable|disable|"
    r"export|reset|clear|forget|explain)\b", re.IGNORECASE)


def _question_about_action(text: str) -> bool:
    """Questions/advice prompts are not authorization to perform an action."""
    # Embedded questions: "I want to know whether you can run checkout", "I wonder if ...".
    # Only the wording before the first action verb counts, so a request whose *subject* is
    # a question ("write a test to check whether I can log in") is still a request.
    words = _without_literals(text).replace("\u2019", "'")
    verb = _ACTION_VERB.search(words)
    lead = words[:verb.start()] if verb else words
    if re.search(r"\b(?:want|would\s+like|'d\s+like|need|trying)\s+to\s+(?:know|find\s+out|understand)\b|"
                 r"\b(?:wonder(?:ing)?|curious|not\s+sure)\b[^.!?]{0,40}\b(?:whether|if|how|what)\b|"
                 r"\bwhether\b[^.!?]{0,60}\b(?:you|argus|it|we|i)\s+(?:can|could|should|would|will|may|might)\b",
                 lead, re.IGNORECASE):
        return True
    if re.match(r"^\s*(?:please\s+)?(?:tell\s+me|explain|describe|show\s+me|"
                r"teach\s+me|help\s+me\s+understand)\b[^.!?]*\b(?:how|why|when|whether)\b",
                text, re.IGNORECASE):
        return True
    # A direct polite request is authorization; a capability/consequence question
    # is not, even if a model proposes a mutating intent for it.
    if re.match(r"^\s*(?:can|could|would|will)\s+you\s+(?:please\s+)?"
                r"(?:run|execute|rerun|test|check|stop|cancel|abort|save|persist|init|"
                r"initialize|initialise|setup|set|scaffold|write|draft|create|make|"
                r"roam|explore|switch|change|use|select|choose|start|watch|enable|disable)\b",
                text, re.IGNORECASE):
        return False
    return bool(re.match(
        r"^\s*(?:what|which|why|how|when|where|is|are|was|were|has|have|had|"
        r"does|did|do|will|would|should|could|can|may|might)\b", text, re.IGNORECASE))


_FUTURE_TIME = re.compile(
    r"\b(?:tomorrow|tonight|later|afterwards?|eventually|soon|overnight|"
    r"next\s+(?:minute|hour|day|week|weekend|month|year|time|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday)|"
    r"this\s+(?:morning|afternoon|evening|weekend)|"
    r"(?:on\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"in\s+(?:a|an|a\s+few|a\s+couple\s+(?:of\s+)?|half\s+an?|a\s+half|another|\d+(?:\.\d+)?|"
    r"one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|forty|fifty|sixty)"
    r"(?:\s+and\s+a\s+half)?\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)|"
    r"in\s+a\s+(?:bit|while|moment)|"
    r"(?:an?|a\s+few|a\s+couple\s+(?:of\s+)?|half\s+an?|another|\d+(?:\.\d+)?|"
    r"one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|forty|fifty|sixty)"
    r"(?:\s+and\s+a\s+half)?\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)"
    r"(?:\s+and\s+a\s+half)?\s+from\s+now|"
    r"at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?|at\s+(?:noon|midnight))\b",
    re.IGNORECASE)


# A correction after the request ("run checkout, actually don't") takes it back.
# _TAKE_BACK is safe to apply to free text such as a test description or a roam target;
# _CANCELLATION also treats a bare "don't"/"but not" after the verb as a take-back.
_TAKE_BACK = (r"never\s*mind|nevermind|cancel\s+(?:that|it|this)|cancel(?=\s*(?:[.!,;]|$))|"
              r"scratch\s+that|forget\s+(?:it|that|about\s+(?:it|that))|on\s+second\s+thoughts?|hold\s+off|"
              r"(?:i(?:'ve|\s+have)?\s+)?changed\s+my\s+mind|(?:i\s+)?take\s+(?:that|it)\s+back|"
              r"no\s*,?\s*wait|wait\s*,?\s*no|"
              r"actually\s*,?\s*(?:no|not|don'?t|do\s+not)")
_TAKE_BACK_RE = re.compile(r"(?<![\w-])(?:" + _TAKE_BACK + r")(?![\w-])", re.IGNORECASE)
_CANCELLATION = re.compile(
    r"(?<![\w-])(?:" + _TAKE_BACK + r"|don'?t|do\s+not|dont|but\s+(?:not|never))(?![\w-])",
    re.IGNORECASE)
# The app can't wait for a condition, so "stop it if it hangs" / "roam X once the build
# passes" must not act immediately.
_CONDITION = re.compile(
    r"(?<![\w-])(?:if|when|whenever|once|after|before|until|till|unless|as\s+soon\s+as|"
    r"in\s+case|provided\s+that)(?![\w-])", re.IGNORECASE)
_ROAM_TAKE_BACK = re.compile(
    r"(?<![\w-])(?:" + _TAKE_BACK + r"|but\s+(?:don'?t|do\s+not|not|never))(?![\w-])",
    re.IGNORECASE)


def _deferred_or_withdrawn(text: str, verb_pattern: str) -> bool:
    """True when an action meant to happen now is conditional, scheduled, or taken back.

    The desktop app can neither wait for a condition nor schedule work, and a take-back
    after the verb ("switch to local, actually don't") withdraws the request.
    """
    words = _without_literals(text).replace("\u2019", "'")
    if _CONDITION.search(words) or _FUTURE_TIME.search(words):
        return True
    verb = re.search(verb_pattern, words, re.IGNORECASE)
    return bool(verb and _CANCELLATION.search(words, verb.end()))


def _without_literals(text: str) -> str:
    """Blank quoted text, URLs, paths and filenames: their words aren't instructions."""
    return re.sub(r'"[^"\n]*"|(?<!\w)\'[^\'\n]*\'|\S*[/\\]\S*|\S+\.[A-Za-z_]\S*',
                  lambda m: " " * len(m.group()), text)


_RETENTION_CLAUSE = re.compile(
    r"(?:" + _SETTING_REFUSAL + r"(?:(?!\b(?:run|execute|rerun|re-run|test|check)\b)[^.;!?]){0,40})?"
    r"\b(?:keep(?:ing)?|retain(?:ing)?)\s+(?:the\s+)?failure\s+capsule\b",
    re.IGNORECASE)


def _mentioned_tests(text: str, tests: Sequence[Mapping]) -> List[str]:
    """Tests named in ``text`` (file, stem or title), in the order they appear."""
    lowered = text.casefold()
    found = []
    for item in tests:
        aliases = {str(item.get("file") or "").casefold(), _test_stem(str(item.get("file") or "")),
                   str(item.get("name") or "").casefold()} - {""}
        positions = [m.start() for alias in aliases
                     for m in re.finditer(r"(?<![\w.-])" + re.escape(alias) + r"(?![\w-]|\.[\w-])", lowered)]
        if positions:
            found.append((min(positions), str(item["file"])))
    return [file for _, file in sorted(found)]


def _run_scope_from_text(text: str, tests: Sequence[Mapping]) -> tuple:
    """Return the test scope explicitly authorized by the user's own words."""
    # Literal quoted titles and complete filenames are data, not instructions.
    # Keep offsets intact so scope extraction still uses the original request.
    command = re.sub(r'"[^"\n]*"|(?<!\w)\'[^\'\n]*\'|(?<![\w.-])[\w.-]+\.test\.ya?ml(?![\w.-])',
                     lambda m: " " * len(m.group()), text).replace("\u2019", "'")
    # Failure-retention settings ("don't retain the failure capsule") are parsed by
    # run_settings_from_text(); they neither withdraw the run nor exclude tests.
    command = _RETENTION_CLAUSE.sub(lambda m: " " * len(m.group()), command)
    if _question_about_action(text):
        return None, "I can describe the tests, but I won't execute them unless you explicitly ask me to run one."
    if re.search(
        r"\b(?:don'?t|do\s+not|never|not|no|avoid|can'?t|cannot|won'?t|"
        r"shouldn'?t|mustn'?t|without)\b[^.;!?]*\b(?:run(?:ning)?|execut(?:e|ing)|rerun(?:ning)?|re-run(?:ning)?|test(?:ing)?|check(?:ing)?)\b",
        command,
        re.IGNORECASE,
    ):
        return None, "I won't execute a test when the instruction is negated."

    if re.search(r"\b(?:either|or|maybe|perhaps|possibly|might)\b", command, re.IGNORECASE):
        return None, "I didn't run anything: alternatives or uncertainty need an explicit test list. Name the tests you want to run."

    executes = (
        _requested(command, r"run|execute|rerun|re-run")
        or re.search(
            r"(?:^|\b(?:please|can\s+you|could\s+you|would\s+you|i\s+want\s+you\s+to)\s+)"
            r"(?:test|check)\b",
            _without_reported_speech(command),
            re.IGNORECASE,
        )
    )
    if not executes:
        return None, "I won't execute tests unless you explicitly ask me to run, execute, test, or check them."
    if _CANCELLATION.search(command, executes.end()):
        return None, "I didn't run anything: the request was taken back. Ask again when you want it to run."

    if _CONDITION.search(command) or re.search(
            r"\b(?:except|excluding|exclude|skip|skipping|omit|omitting|without|"
            r"only\s+when|but\s+not|all\s+but|other\s+than|apart\s+from|instead\s+of)\b",
            command, re.IGNORECASE):
        return None, "I didn't run anything: exclusions or conditions need an explicit list of tests to run. Name only the tests you want."

    # The desktop app has no scheduler, so a future time can't be honoured by running now.
    if _FUTURE_TIME.search(command):
        return None, ("I didn't run anything: I can't schedule runs for later. "
                      "Ask again when you want the tests to run now.")

    # Reported instructions ("the README says: run all tests") can't widen the scope either.
    if re.search(
        r"\b(?:all\s+(?:the\s+)?tests?|every\s+test|everything|(?:whole|full)\s+suite|"
        r"(?:run|execute|rerun|re-run)\s+(?:them\s+)?all)\b",
        _without_reported_speech(command),
        re.IGNORECASE,
    ):
        return "all", None

    # Only the requested test phrase can authorize tests. Command and execution
    # settings are not names, even when a test happens to share their vocabulary.
    lowered = _without_reported_speech(text)[executes.end():].casefold()
    quoted = [(m.start(), m.end()) for m in re.finditer(r'"[^"]*"|\'[^\']*\'', lowered)]
    settings = re.finditer(r"\b(?:in\s+(?:a\s+)?(?:(?:hyper-?v|libvirt)\s+)?capsule|"
                           r"(?:" + _ON_THIS_MACHINE + r"|on\s+(?:this\s+)?(?:device|machine)|locally)|"
                           r"(?:keep|retain)(?:ing)?\s+(?:the\s+)?failure\s+capsule)(?![\w.-])",
                           lowered)
    # Blank each setting in place (keeping offsets) so tests named after it still count:
    # "run checkout in a capsule then profile" asks for both.
    for setting in reversed(list(settings)):
        if not any(a <= setting.start() < b for a, b in quoted):
            lowered = lowered[:setting.start()] + " " * (setting.end() - setting.start()) + lowered[setting.end():]
    matches = []
    chosen = []
    for item in tests:
        aliases = {
            str(item.get("file") or "").casefold(),
            _test_stem(str(item.get("file") or "")),
            str(item.get("name") or "").casefold(),
        }
        aliases.discard("")
        for alias in aliases:
            for match in re.finditer(r"(?<![\w.-])" + re.escape(alias) + r"(?![\w-]|\.[\w-])", lowered):
                matches.append((match.start(), match.end(), str(item["file"])))
    # A complete filename or longer title takes precedence over a short alias
    # contained within it. Equal spans remain explicit ambiguous selections.
    occupied = []
    for start, end, file in sorted(matches, key=lambda m: m[1] - m[0], reverse=True):
        if any(start < b and end > a and (start, end) != (a, b) for a, b in occupied):
            continue
        occupied.append((start, end))
        chosen.append((start, end, file))
    for start, end in occupied:
        if len({file for a, b, file in matches if (a, b) == (start, end)}) > 1:
            return None, "That name matches multiple tests. Use the exact filename of each test you want to run."
    if not chosen:
        return None, "Which test should I run? Name a test from the sidebar, or say 'run all tests'."
    ordered = [file for start, end, file in sorted(set(chosen))]
    # Repeated sequential mentions authorize repeated runs; ordinary duplicate
    # mentions still select each test once, preserving their first occurrence.
    if not re.search(r"\b(?:then|followed\s+by|in\s+(?:this\s+)?order)\b", lowered):
        ordered = list(dict.fromkeys(ordered))
    return ordered, None


# Where an instruction to Argus can start: the start of the message or a clause, after a
# connective or "please", or after a direct request ("can you", "I want you to"). A verb
# anywhere else ("the CI will run checkout", "the app will stop responding") describes
# something; it doesn't ask Argus to act.
_REQUEST_LEAD = (r"(?:^|(?<=[.!?;,])|\b(?:and|then|now|so|ok(?:ay)?|also|just|go\s+ahead\s+and|"
                 r"let'?s|please|kindly|argus)\b|\b(?:can|could|would|will)\s+you\b|"
                 r"\b(?:i|we)(?:'d|\s+would)?\s+(?:want|need|like)\s+(?:you\s+|argus\s+)?to\b|"
                 r"\b(?:time|ready)\s+to\b)")
_REQUEST_ADVERBS = r"(?:\s*\b(?:please|just|also|now|quickly|immediately|again|kindly)\b)*"


# Reported or quoted speech ("the CI says: run checkout", "please explain, run checkout")
# introduces someone else's words, not an instruction to Argus. The clause after the
# reporting verb is blanked (offsets kept) before looking for a request.
_REPORTED_SPEECH = re.compile(
    r"(?:\b(?:says?|said|saying|asks?|asked|reads?|writes?|wrote|told\s+\w+|tells?\s+\w+|"
    r"explain|describe|means?|meant|quotes?|quoted)\s*[:,]|"
    # "the CI says to run X", "the README asks us to run X", "it told me to stop"
    # ("I want you to run X" is the user's own request, so not when the subject is I/we.)
    r"\b(?:says?|said|asks?|asked|tells?|told|(?<!\bi\s)(?<!\bwe\s)(?:wants?|wanted)|"
    r"instructs?|instructed|recommends?|suggests?)"
    r"(?:\s+(?:us|me|you|them|people|users|everyone))?\s+to\b)"
    # ... up to the end of the clause: a sentence break, or a contrastive turn back to the
    # user's own words ("..., but please run smoke"), which is read as a request again.
    # Sequencing words ("first run checkout, then run smoke", "now stop") stay reported.
    r"(?:(?!,?\s*\b(?:but|however)\b)[^.!?;\n])*",
    re.IGNORECASE)


def _without_reported_speech(text: str) -> str:
    return _REPORTED_SPEECH.sub(lambda m: " " * len(m.group()), text)


def _requested(command: str, verbs: str):
    """The first of ``verbs`` used as a request to Argus, or None."""
    return re.search(_REQUEST_LEAD + _REQUEST_ADVERBS + r"\s*\b(?:" + verbs + r")\b",
                     _without_reported_speech(command), re.IGNORECASE)


def _authorized_simple_action(text: str, action: str) -> bool:
    if _question_about_action(text):
        return False
    action_words = {
        "stop": r"stop|cancel|abort|halt",
        "save_test": r"save|persist",
        "init": r"init|initialize|initialise|setup|set\s+up|scaffold",
        "write_test": r"write|draft|create|make",
    }
    patterns = {
        "stop": _REQUEST_LEAD + _REQUEST_ADVERBS + r"\s*(?:stop|cancel|abort|halt)\b",
        "save_test": (
            r"^\s*(?:please\s+)?(?:save|persist)\s*[.!]?\s*$|"
            r"\b(?:save|persist)\b\s+(?:(?:this|that)\s+|(?:the\s+)?current\s+|the\s+)?"
            r"(?:draft|test|spec|it)\b"
        ),
        "init": r"\b(?:init|initialize|initialise|setup|set\s+up|scaffold)\b[^.!?]{0,80}\b(?:argus|project|workspace)\b",
        "write_test": _REQUEST_LEAD + _REQUEST_ADVERBS + r"\s*(?:write|draft|create|make)\b[^.!?]{0,80}\b(?:test|spec)\b",
    }
    # Quoted descriptions are data. Scope negation to the requested action,
    # so "write a test ensuring users cannot create accounts" remains a draft.
    command = text.replace("\u2019", "'")
    command = re.sub(r'"[^"\n]*"|(?<!\w)\'[^\'\n]*\'',
                     lambda m: " " * len(m.group()), command)
    requested = re.search(patterns[action], _without_reported_speech(command), re.IGNORECASE)
    if not requested:
        return False
    verb = re.search(r"\b(?:" + action_words[action] + r")\b", requested.group(), re.IGNORECASE)
    verb_end = requested.start() + verb.end()
    # A take-back after the verb ("stop the run, actually don't") withdraws the request.
    # A drafted test's description may itself say "don't", so it only honours clear take-backs.
    # For stop, "cancel" repeats the action ("stop it, cancel"), so it isn't a take-back.
    trailing = _TAKE_BACK_RE if action == "write_test" else _CANCELLATION
    if any(not (action == "stop" and m.group().lower().startswith("cancel"))
           for m in trailing.finditer(command, verb_end)):
        return False
    # A drafted test's description may say "when ..."; the other actions take effect now.
    if action != "write_test" and (_CONDITION.search(command) or _FUTURE_TIME.search(_without_literals(command))):
        return False
    return not re.search(
        r"\b(?:don'?t|do\s+not|never|not|no|avoid|can'?t|cannot|won'?t|"
        r"shouldn'?t|mustn'?t|without)\b[^.;!?]*\b(?:" + action_words[action] + r")\b",
        command[:verb_end], re.IGNORECASE,
    )


def _authorized_explain(text: str) -> bool:
    """Authorize disclosure of prior failure details only for an explicit explanation request."""
    # "Why did the last test fail?" is itself the explicit disclosure request.
    # Feature/advice questions such as "what can Argus explain?" or "should you
    # explain this?" are not.
    if re.match(r"^\s*(?:what|which|how|when|where)\b", text, re.IGNORECASE):
        return False
    if re.match(r"^\s*should\s+(?:you|argus|i|we)\b", text, re.IGNORECASE):
        return False
    if re.search(
        r"\b(?:don'?t|do\s+not|never)\b[^.;!?]{0,60}\b(?:explain|why|failure|failed|error)\b",
        text,
        re.IGNORECASE,
    ):
        return False
    # The request must be the user's own and for now: not reported ("the report explains
    # why it failed"), described ("the CI will explain ..."), postponed or taken back.
    words = _without_reported_speech(text).replace("\u2019", "'")
    if _FUTURE_TIME.search(_without_literals(words)):
        return False
    failure = r"\b(?:fail(?:ed|ure|s|ing)?|error(?:ed|s)?|broke|broken)\b"
    asked = (
        # A direct why-question: "why did the last test fail?"
        re.match(r"^\s*(?:(?:so|and|but|ok(?:ay)?|hmm)\s*,?\s*)?why\b[^.!?]{0,100}" + failure, words, re.IGNORECASE)
        or re.search(_REQUEST_LEAD + _REQUEST_ADVERBS + r"\s*(?:explain|analy[sz]e|tell\s+me|show\s+me)\b"
                     r"[^.!?]{0,100}(?:" + failure + r"|\bwhy\b)", words, re.IGNORECASE)
    )
    if not asked:
        return False
    return not _CANCELLATION.search(words, asked.end())


def _environment_change_from_text(text: str) -> tuple:
    """Return an explicitly requested session-environment change, or (None, problem)."""
    if _question_about_action(text):
        return None, None
    # Retention ("... and don't keep the failure capsule") is a separate setting read by
    # run_settings_from_text(); its "don't" is not a take-back of the environment change.
    text = _RETENTION_CLAUSE.sub(lambda m: " " * len(m.group()), text)
    text = re.sub(r"(?:\s*(?:,|\band\b|\bbut\b))?\s*$", "", text)
    if re.search(
        _REFUSAL + r"[^.;!?]{0,60}\b(?:switch|change|set|use|select|choose)\b",
        text.replace("\u2019", "'"),
        re.IGNORECASE,
    ) or _deferred_or_withdrawn(text, r"\b(?:switch|change|set|use|select|choose)\b"):
        return None, None

    # First establish that the user actually requested a picker change. Provider
    # resolution happens afterwards so "libvirt capsule" cannot collapse to the
    # less-specific plain "capsule" token.
    change_requested = bool(
        re.search(
            r"(?:^|[.!?;]\s*|\bplease\s+)"
            r"(?:switch|change|set|select|choose)\b[^.!?]{0,100}"
            r"\b(?:local|capsule|hyper-?v|libvirt)\b",
            text,
            re.IGNORECASE,
        )
        or re.search(
            r"^\s*(?:please\s+)?use\s+(?:a\s+)?"
            r"(?:local|capsule|hyper-?v|libvirt)\b"
            r"(?:\s+capsule)?(?:\s+(?:mode|environment|for\s+argus))?\s*[.!]?\s*$",
            text,
            re.IGNORECASE,
        )
    )
    if not change_requested:
        return None, None

    words = {
        w.lower().replace("-", "")
        for w in re.findall(r"\b(?:local|capsule|hyper-?v|libvirt)\b", text, re.IGNORECASE)
    }
    if "local" in words and ({"capsule", "hyperv", "libvirt"} & words):
        return None, "You named both Local and a Capsule. Pick one environment."
    if "libvirt" in words:
        return {"environment": "capsule", "capsule_provider": "libvirt"}, None
    if "hyperv" in words:
        return {"environment": "capsule", "capsule_provider": "hyperv"}, None
    if "capsule" in words:
        return {"environment": "capsule", "capsule_provider": "auto"}, None
    if "local" in words:
        return {"environment": "local"}, None
    return None, None


_PROVIDER_SUBJECT = (r"provider|model|llm|connection|connectivity|vision|api(?:\s+key)?|key|"
                     r"openai|anthropic|claude|gpt|gemini|ollama")


def _authorized_provider_check(text: str, configured: Sequence[str] = ()) -> bool:
    """A provider ping must be asked for now, about the provider, and not refused."""
    words = _without_literals(_without_reported_speech(text)).replace("\u2019", "'")
    names = "|".join(re.escape(str(p)) for p in configured if str(p).strip())
    subject = r"\b(?:" + _PROVIDER_SUBJECT + (r"|" + names if names else "") + r")\b"
    if not re.search(subject, words, re.IGNORECASE):
        return False
    verbs = r"\b(?:check(?:ing)?|test(?:ing)?|ping(?:ing)?|probe|probing|verify(?:ing)?|validate|try|call|contact)\b"
    if re.search(_REFUSAL + r"[^.;!?]{0,60}" + verbs, words, re.IGNORECASE) or re.search(
            r"\b(?:skip|no\s+need)\b[^.;!?]{0,60}(?:" + verbs + "|" + subject + ")", words, re.IGNORECASE):
        return False
    # Advice and how-to questions ask about the check; "is my provider working?" asks for it.
    if re.match(r"^\s*(?:how|why|when|where|should|shall)\b|^\s*what\s+(?:happens|does|would|will)\b",
                words, re.IGNORECASE) or re.search(
            r"\b(?:want|would\s+like|'d\s+like|need)\s+to\s+(?:know|understand)\s+how\b|"
            r"\b(?:wonder(?:ing)?|curious|not\s+sure)\b[^.!?]{0,40}\b(?:whether|if|how)\b[^.!?]{0,40}"
            r"\b(?:should|need)\b", words, re.IGNORECASE):
        return False
    # Asked of Argus ("check my provider", "can you test the model connection") or a direct
    # status question ("is my provider working?"); a description ("the CI checks the provider",
    # "I checked it") is not a request for a billed ping.
    if not (re.search(_REQUEST_LEAD + _REQUEST_ADVERBS + r"\s*" + verbs, words, re.IGNORECASE)
            or re.match(r"^\s*(?:is|are|does|do|can)\b(?!\s+(?:you|argus|i|we)\b)", words, re.IGNORECASE)):
        return False
    anchor = re.search(verbs, words, re.IGNORECASE) or re.search(subject, words, re.IGNORECASE)
    if _CONDITION.search(words) or _FUTURE_TIME.search(words):
        return False
    return not _CANCELLATION.search(words, anchor.end())


def _environment_inspection(text: str) -> bool:
    """A read-only request to see where runs execute ("where will tests run?")."""
    words = _without_literals(text).replace("\u2019", "'")
    if re.search(r"\b(?:switch|change|set|select|choose|toggle)\b", words, re.IGNORECASE):
        return False
    if re.search(_REFUSAL + r"[^.;!?]{0,40}\b(?:show|display|tell|see)\b", words, re.IGNORECASE):
        return False
    return bool(re.search(
        r"\b(?:show|display|see|view|check|tell\s+me|what|which|current)\b[^.!?]{0,60}"
        r"\b(?:environment|env|capsule|isolation|sandbox)\b|"
        r"\bwhere\b[^.!?]{0,40}\b(?:tests?|runs?|specs?)\b[^.!?]{0,20}\b(?:run|execute|go)\b|"
        r"\bwhere\s+(?:do|does|will|would|are|is)\b[^.!?]{0,20}\b(?:run|runs|running|execut\w*)\b|"
        r"\b(?:run|running)\s+(?:locally|in\s+a\s+capsule)\s*\?",
        words, re.IGNORECASE))


def _provider_change_from_text(text: str, configured: Sequence[str]) -> Optional[str]:
    """Return the explicitly requested provider; descriptive mentions do not switch."""
    if _question_about_action(text):
        return None
    if re.search(
        _REFUSAL + r"[^.;!?]{0,60}\b(?:use|switch|change|select|choose)\b",
        text.replace("\u2019", "'"),
        re.IGNORECASE,
    ) or _deferred_or_withdrawn(text, r"\b(?:use|switch|change|select|choose)\b"):
        return None

    for provider in configured:
        p = re.escape(provider)
        patterns = (
            rf"^\s*(?:please\s+)?(?:switch|change)\s+(?:the\s+)?(?:provider|model)?\s*to\s+{p}\b",
            rf"^\s*(?:please\s+)?(?:select|choose)\s+{p}(?:\s+(?:as\s+)?(?:the\s+)?(?:provider|model))?\b",
            rf"^\s*(?:please\s+)?use\s+{p}(?:\s+(?:for\s+argus|as\s+(?:the\s+)?(?:provider|model)))?\s*[.!]?\s*$",
            rf"\b(?:switch|change)\s+(?:argus\s+)?(?:provider|model)\s+to\s+{p}\b",
        )
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns):
            return provider
    return None


def _watch_only_stop(text: str) -> bool:
    """A stop aimed at the watcher alone, not at a run, roam or everything."""
    words = _without_literals(text)
    return bool(re.search(r"\bwatch(?:ing|er|es)?\b", words, re.IGNORECASE)
                and not re.search(r"\b(?:runs?|running\s+tests?|tests?|roam(?:ing)?|jobs?|everything|all|both)\b",
                                  words, re.IGNORECASE))


def _watch_action_from_text(text: str) -> Optional[str]:
    text = text.replace("\u2019", "'")
    if _question_about_action(text):
        return None
    if re.search(r"\b(?:don'?t|do\s+not|never|not|avoid|no|can'?t|cannot|won'?t|shouldn'?t|mustn'?t|without)\b[^.;!?]*"
                 r"\b(?:watch(?:ing)?|monitor(?:ing)?|start|stop|enable|disable|turn\s+(?:on|off))\b",
                 text, re.IGNORECASE):
        return None
    # Watch takes effect now, so a condition or a later take-back withdraws it.
    words = _without_literals(text)
    if _CONDITION.search(words) or _FUTURE_TIME.search(words):
        return None
    # The verb must be the user's request ("start watching tests"), not a description
    # ("I was watching tests") or someone else's words ("the docs say: watch tests").
    words = _without_reported_speech(words)
    lead = _REQUEST_LEAD + _REQUEST_ADVERBS + r"\s*"
    stop = re.search(lead + r"(?:stop|disable|cancel|end|quit|turn\s+off)\b[^.!?]{0,40}\bwatch(?:ing|er)?\b|"
                     + lead + r"(?:turn|switch)\s+(?:the\s+)?watch(?:er)?\s+off\b", words, re.IGNORECASE)
    start = None if stop else re.search(
        lead + r"(?:start|enable|turn\s+on)\b[^.!?]{0,40}\bwatch(?:ing|er)?\b|"
        + lead + r"(?:watch|monitor)\b[^.!?]{0,60}\b(?:tests?|files?|specs?|changes)\b|"
        r"^\s*(?:please\s+)?watch\b",
        words, re.IGNORECASE)
    found = stop or start
    if found is None:
        return None
    # "cancel" repeats a stop rather than withdrawing it.
    if any(not (stop and m.group().lower().startswith("cancel"))
           for m in _CANCELLATION.finditer(words, found.end())):
        return None
    return "stop" if stop else "start"


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


_DEICTIC_KNOWLEDGE = re.compile(
    r"^(?:it|that|this|them|the\s+(?:last|previous|same|current)(?:\s+(?:app|target|one))?|"
    r"(?:the\s+)?(?:last|previous)\s+(?:app|target|one))$", re.IGNORECASE)


def _knowledge_target_from_text(text: str, verb: str) -> Optional[str]:
    """The knowledge target the user named ("export knowledge for notepad.exe"), if any.

    A deictic reference ("export its knowledge", "... for the last app") names nothing,
    so the caller keeps its default of the last roamed target.
    """
    # Only the target position counts: a quoted aside elsewhere ("... because "I need a
    # backup"") is not a target, and the clause ends at "because/and/so/..." or punctuation
    # that ends a sentence; "?" or "," inside a URL ("search?q=one") is part of the target.
    quoted = r'"(?P<q1>[^"\n]+)"|(?<!\w)\'(?P<q2>[^\'\n]+)\''
    clause = r"(?P<t>.+?)(?=\s*(?:[,;!?](?:\s|$)|\.(?:\s|$)|\s+(?:because|since|so|and|then|as|but|please|now)\b|$))"
    m = (re.search(r"\bknowledge(?:\s+graph)?\s+(?:for|of|about|on)\s+(?:" + quoted + "|" + clause + ")", text, re.IGNORECASE)
         or re.search(r"\b" + verb + r"\s+(?:the\s+)?(?:" + quoted + r"|(?P<w>(?!(?:the|a|an|my|its|this|that|all|our|your)\b)\S+?))"
                      r"(?:'s)?\s+knowledge\b", text, re.IGNORECASE))
    if not m:
        return None
    groups = m.groupdict()
    if groups.get("q1") or groups.get("q2"):
        return (groups.get("q1") or groups.get("q2")).strip() or None
    found = re.sub(r"(?:[\s,]+(?:please|now|thanks|thank\s+you))+\s*$", "", groups.get("t") or groups.get("w") or "",
                   flags=re.IGNORECASE).strip()
    found = _unquote(found.rstrip(",;"))
    if not found or _DEICTIC_KNOWLEDGE.match(found):
        return None
    return found


def _names_knowledge_target(text: str, target: str) -> bool:
    """True when the user's message itself names ``target`` (as written or by its key)."""
    # Whole mentions only: "app" must not match "happy.exe", nor "notepad" match "notepad.exe".
    if re.search(r"(?<![\w.\-/\\:])" + re.escape(target) + r"(?![\w]|[.\-/\\:]\w)", text, re.IGNORECASE):
        return True
    key = re.sub(r"[^a-z0-9]+", "-", target.casefold()).strip("-")
    if not key:
        return False
    # By knowledge key ("notepad-exe"): compare against whole whitespace-separated mentions,
    # alone or as a run of consecutive words ("Visual Studio Code").
    tokens = [re.sub(r"[^a-z0-9]+", "-", t.casefold()).strip("-") for t in text.split()]
    tokens = [t for t in tokens if t]
    return any("-".join(tokens[i:j]) == key
               for i in range(len(tokens)) for j in range(i + 1, len(tokens) + 1))


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

    if name in ("help", "evidence", "report", "tokens"):
        return intent(name)

    if name == "providers":
        # Unlike the passive cards, this pings the model (a billed call, maybe a vision probe).
        if _authorized_provider_check(text, context.get("providers") or []):
            return intent("providers")
        return intent("chat", reply="Ask me to check the provider connection when you want me to ping the model.")

    if name == "explain":
        if _authorized_explain(text):
            return intent("explain")
        return intent("chat", reply="Ask me explicitly to explain a failure, e.g. 'why did the last test fail?'.")

    if name == "stop" and _watch_only_stop(text):
        # The global stop also aborts the running test; "stop watching" stops only the watcher.
        if _watch_action_from_text(text) == "stop":
            return intent("watch", action="stop")
        return intent("chat", reply="Say 'stop watch' to stop watching, or 'stop the run' to stop the current run.")

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

        # Only what the user named is shown: the router can't widen "dry run checkout" to
        # other specs (and their launch commands).
        words = _without_reported_speech(text)
        if wanted == "draft":
            if re.search(r"\b(?:draft|it|this|that)\b", words, re.IGNORECASE):
                return intent("dry_run", tests="draft")
            return intent("chat", reply="Which test should I dry-run? Name it, say 'the draft', or say 'all'.")
        named = _mentioned_tests(words, tests)
        if named:
            return intent("dry_run", tests=named)
        if re.search(r"\b(?:all|every(?:thing)?|whole|full|suite)\b", words, re.IGNORECASE) or wanted == "all":
            return intent("dry_run", tests="all")
        return intent("chat", reply="Which test should I dry-run? Name it, or say 'all'.")

    if name == "roam":
        if _question_about_action(text):
            return intent("chat", reply="Ask me explicitly to roam a target before I start testing it.")
        target = _unquote(str(args.get("target") or "").strip())
        last = str(context.get("last_target") or "")
        adapter = adapter_for(target)
        request = split_roam_request(text)
        if request is not None and request.problem:
            return intent("chat", reply=request.problem)
        verb = _ROAM_VERB.search(text)
        if verb and _ROAM_TAKE_BACK.search(_without_literals(text).replace("\u2019", "'"), verb.end()):
            return intent("chat", reply="I didn't start anything: the request was taken back.")
        if _CONDITION.search(_without_literals(text)):
            return intent("chat", reply=("I didn't start anything: I can't wait for a condition before "
                                         "roaming. Ask again when you want it to start now."))
        if _FUTURE_TIME.search(_without_literals(text)):
            return intent("chat", reply=("I didn't start anything: I can't schedule a roam for later. "
                                         "Ask again when you want it to start now."))
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
        # The router selects an action; it cannot rewrite the generator's task.
        return intent("write_test", description=text)

    if name == "knowledge":
        action = args.get("action") if args.get("action") in KNOWLEDGE_ACTIONS else "show"
        if action in ("reset", "export"):
            verb = r"(?:reset|clear|forget)" if action == "reset" else r"export"
            negated = re.search(_REFUSAL + r"[^.;!?]*\b" + verb + r"\b",
                                text.replace("\u2019", "'"), re.IGNORECASE)
            deferred = _deferred_or_withdrawn(text, r"\b" + verb + r"\b")
            # Filenames/URLs are blanked so "export notepad.exe knowledge" isn't cut at the dot,
            # and reported speech ("the README says: reset knowledge ...") isn't the user's ask.
            words = _without_literals(_without_reported_speech(text))
            if _question_about_action(text) or negated or deferred or not (
                re.search(verb + r"\b[^.!?]{0,60}\bknowledge\b", words, re.IGNORECASE)
                or re.search(r"\bknowledge\b[^.!?]{0,60}" + verb + r"\b", words, re.IGNORECASE)
            ):
                return intent("chat", reply=f"I won't {action} knowledge unless you explicitly ask me to.")
            target = _unquote(str(args.get("target") or "").strip())
            # The model picks the action, not whose graph is exported or deleted.
            if target and not _names_knowledge_target(text, target):
                return intent("chat", reply=f"Which target should I {action} knowledge for? Name it exactly as you roamed it.")
            if not target:
                # A dropped target must not fall back to the last roamed app when the user
                # named one: take it from their own words.
                target = _knowledge_target_from_text(text, verb) or ""
            return intent("knowledge", action=action, target=target)
        return intent("knowledge", action=action, target=str(args.get("target") or "").strip())

    if name == "switch_provider":
        wanted = str(args.get("provider") or "").lower().strip()
        configured = [str(p).lower() for p in context.get("providers") or []]
        requested = _provider_change_from_text(text, configured)
        if requested is None or requested != wanted:
            return intent("chat", reply="Ask me explicitly to switch Argus to a configured provider.")
        if wanted not in configured:
            return intent("chat", reply=f"'{wanted or '?'}' isn't configured in .argus/config.yaml. Configured: {', '.join(configured) or 'none'}.")
        return intent("switch_provider", provider=wanted)

    if name == "environment":
        settings, problem = _environment_change_from_text(text)
        if not problem and not settings and _environment_inspection(text):
            return intent("environment")
        if problem or not settings:
            return intent(
                "chat",
                reply=problem or "Ask me explicitly to switch the session environment, e.g. 'switch to local' or 'use a libvirt capsule'.",
            )
        # "switch to a capsule and retain the failure capsule" sets both, or neither.
        extra, retention_problem = run_settings_from_text(text)
        if retention_problem:
            return intent("chat", reply=retention_problem)
        if "retain" in extra:
            if settings.get("environment") == "local":
                return intent("chat", reply="Keeping a failed Capsule only applies to Capsule runs, not Local.")
            settings["retain"] = extra["retain"]
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
