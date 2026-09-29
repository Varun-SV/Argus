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
import shlex
from typing import Iterable, List, Mapping, Optional, Sequence

import yaml

from argus.engine.spec import ASSERTION_KINDS, SpecError, parse_spec

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


def _parse_roam_args(rest: str, last_target: str) -> dict:
    try:
        parts = shlex.split(rest, posix=True)
    except ValueError as exc:
        raise IntentError(f"Could not read the roam command: {exc}") from exc
    # Argus's own options are --minutes/-m N, --adapter X and --[no-]memory; every other
    # token belongs to the target command, e.g. `/roam python tool.py --check`.
    words, adapter, minutes, memory = [], None, None, None
    i = 0
    while i < len(parts):
        p = parts[i]
        if p in ("--minutes", "-m") and i + 1 < len(parts):
            minutes = _minutes(parts[i + 1])
            i += 2
            continue
        if p == "--adapter" and i + 1 < len(parts):
            adapter = parts[i + 1]
            i += 2
            continue
        if p in ("--no-memory", "--memory"):
            memory = p == "--memory"
        else:
            words.append(p)
        i += 1
    target = (words[0] if len(words) == 1 else shlex.join(words)) if words else last_target
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


def validate_intent(raw: Mapping, text: str, context: Mapping) -> dict:
    """Turn a model's routing answer into a safe intent, or a chat reply."""
    name = str(raw.get("intent", "")).strip()
    args = raw.get("args") if isinstance(raw.get("args"), Mapping) else {}
    tests = context.get("tests") or []
    if name not in INTENTS:
        return intent("chat", reply="I'm not sure what to do with that. Type /help to see what Argus can do.")

    if name in ("help", "stop", "save_test", "explain", "evidence", "report", "tokens", "providers", "init"):
        return intent(name)

    if name in ("run", "dry_run"):
        wanted = args.get("tests", "all")
        if wanted == "all" or (name == "dry_run" and wanted == "draft"):
            chosen = wanted
        else:
            names = [str(n) for n in (wanted if isinstance(wanted, list) else [wanted])]
            known = {t["file"] for t in tests}
            chosen, missing = [], []
            for n in names:
                matches = [n] if n in known else resolve_tests(n, tests)
                if matches:
                    chosen.extend(matches)
                else:
                    missing.append(n)
            chosen = list(dict.fromkeys(chosen))
            if missing or not chosen:
                return intent("chat", reply=(
                    f"I couldn't find {', '.join(missing) or 'that test'} in .argus/, so I didn't "
                    "run anything. The Tests list in the sidebar shows what's there."))
        out = intent(name, tests=chosen)
        if name == "run":
            out["args"].update(_env_args(args))
        return out

    if name == "roam":
        target = str(args.get("target") or "").strip().strip("'\"")
        last = str(context.get("last_target") or "")
        if not target or not (target.lower() in text.lower() or (last and target == last)):
            return intent("chat", reply="Which app should I roam? Give me the command, file or URL, e.g. roam notepad.exe for 5 minutes.")
        adapter = args.get("adapter") if args.get("adapter") in ADAPTERS else adapter_for(target)
        memory = args.get("memory")  # None: use the app's Memory toggle
        return intent(
            "roam", target=target, adapter=adapter, minutes=_minutes(args.get("minutes")),
            memory=memory if isinstance(memory, bool) else None,
        )

    if name == "write_test":
        description = str(args.get("description") or text).strip()
        return intent("write_test", description=description[:2000])

    if name == "knowledge":
        action = args.get("action") if args.get("action") in KNOWLEDGE_ACTIONS else "show"
        return intent("knowledge", action=action, target=str(args.get("target") or "").strip())

    if name == "switch_provider":
        wanted = str(args.get("provider") or "").lower().strip()
        configured = [str(p).lower() for p in context.get("providers") or []]
        if wanted not in configured:
            return intent("chat", reply=f"'{wanted or '?'}' isn't configured in .argus/config.yaml. Configured: {', '.join(configured) or 'none'}.")
        return intent("switch_provider", provider=wanted)

    if name == "environment":
        return intent("environment", **_env_args(args))

    if name == "watch":
        return intent("watch", action="stop" if args.get("action") == "stop" else "start")

    return intent("chat", reply=_plain(str(args.get("reply") or "")) or "Type /help to see what Argus can do.")


def _env_args(args: Mapping) -> dict:
    out = {}
    if args.get("environment") in ENVIRONMENTS:
        out["environment"] = args["environment"]
    if args.get("capsule_provider") in CAPSULE_PROVIDERS:
        out["capsule_provider"] = args["capsule_provider"]
        out.setdefault("environment", "capsule")
    if isinstance(args.get("retain"), bool):
        out["retain"] = args["retain"]
    return out


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
