"""Command line: ``argus2`` opens the app; ``argus2 run`` runs tests in a terminal."""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path
from typing import List, Optional

from argus2 import __version__
from argus2.engine import Engine
from argus2.settings import Settings
from argus2.spec import SpecError, parse_spec, parse_target
from argus2.store import StoreError, Workspace

_MARK = {"passed": "✓", "failed": "✗", "error": "!", "skipped": "-", "stopped": "■"}


def _printer(kind: str, **data) -> None:
    if kind == "test_start":
        print(f"\n{data['name']}")
    elif kind == "action":
        print(f"      {'·' if data['ok'] else '✗'} {data['label']} → {data['result']}")
    elif kind == "step_end":
        print(f"  {_MARK.get(data['status'], '?')} step {data['index'] + 1}: {data['detail']}")
    elif kind == "finding":
        print(f"  ⚑ [{data['severity']}] {data['title']}")
    elif kind == "log":
        print(f"  {data['text']}")


def cmd_app(args) -> int:
    from argus2.server import App, app_url, make_server
    app = App(workspace=Path(args.workspace) if args.workspace else None)
    server = make_server(app, args.port)
    url = app_url(app, server)
    print(f"Argus {__version__} is running. Workspace: {app.workspace.root}")
    print(f"Open: {url}\nPress Ctrl+C to quit.", flush=True)
    window = None
    if not args.browser and not args.no_open:
        try:
            import webview  # optional: a native window instead of a browser tab
            window = webview
        except ImportError:
            pass
    if window is not None:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            window.create_window("Argus", url, width=1320, height=860, min_size=(900, 600))
            window.start()
            return 0
        except Exception as exc:  # no WebView runtime on this machine: use the browser
            print(f"Could not open a window ({exc}); using your browser instead.")
            webbrowser.open(url)
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                pass
            return 0
    if not args.no_open:
        threading.Timer(0.4, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    return 0


def cmd_run(args) -> int:
    workspace = Workspace(Path(args.workspace or "."))
    files = args.tests or [t["file"] for t in workspace.list_tests()]
    if not files:
        print(f"No tests in {workspace.tests_dir}. Create one in the app (argus2) first.")
        return 2
    settings = Settings()
    worst = 0
    counts = {}
    for file in files:
        name = file if file.endswith(".yaml") else file + ".yaml"
        try:
            spec = parse_spec(workspace.read_test(Path(name).name))
        except (SpecError, StoreError) as exc:
            print(f"\n{name}\n  ! {exc}")
            counts["error"] = counts.get("error", 0) + 1
            worst = 2
            continue
        out_dir = workspace.new_result_dir()
        result = Engine(settings, workspace.root, _printer).run_test(spec, out_dir, file=Path(name).name)
        workspace.save_result(out_dir, result)
        print(f"  → {result['status'].upper()}: {result['summary']}")
        counts[result["status"]] = counts.get(result["status"], 0) + 1
        worst = max(worst, {"passed": 0, "failed": 1}.get(result["status"], 2))
    print("\n" + " · ".join(f"{n} {status}" for status, n in counts.items()))
    return worst


def cmd_explore(args) -> int:
    workspace = Workspace(Path(args.workspace or "."))
    raw = {"type": "web", "url": args.target} if args.target.lower().startswith(("http://", "https://", "file://")) \
        else {"type": "cli", "command": "" if args.shell else args.target}
    target = parse_target(raw)
    out_dir = workspace.new_result_dir()
    goal = args.goal or ("" if not args.shell else f"Test the command-line program: {args.target}")
    result = Engine(Settings(), workspace.root, _printer).explore(target, goal, args.actions, out_dir)
    workspace.save_result(out_dir, result)
    print(f"\n{result['status'].upper()}: {result['summary']}\n{result['end_reason']}")
    return 2 if result["status"] == "error" else 1 if result["findings"] else 0


def cmd_doctor(args) -> int:
    from argus2.targets import browser_status
    settings = Settings()
    missing = settings.model_ready()
    print(f"Settings file: {settings.path}")
    print(f"Model:   {'ready — ' + settings.data['provider'] + ':' + settings.data['model'] if not missing else missing}")
    status = browser_status()
    print(f"Browser: {status['detail']}")
    return 0 if not missing and status["ok"] else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="argus2", description="Argus — test software the way a user would.")
    parser.add_argument("--version", action="version", version=f"argus2 {__version__}")
    sub = parser.add_subparsers(dest="command")

    def add_app(p):
        p.add_argument("--workspace", help="folder that holds your tests (default: the last one used)")
        p.add_argument("--port", type=int, default=0, help="port to listen on (default: any free port)")
        p.add_argument("--browser", action="store_true", help="open in your web browser instead of a window")
        p.add_argument("--no-open", action="store_true", help="do not open anything; just print the link")

    add_app(parser)
    add_app(sub.add_parser("app", help="open the Argus app (the default)"))
    run = sub.add_parser("run", help="run tests in the terminal")
    run.add_argument("tests", nargs="*", help="test file names (default: all)")
    run.add_argument("--workspace", help="workspace folder (default: current folder)")
    explore = sub.add_parser("explore", help="let Argus explore a URL or a command and report defects")
    explore.add_argument("target", help="a URL, or a program to keep open and talk to")
    explore.add_argument("--goal", default="", help="what to focus on")
    explore.add_argument("--actions", type=int, default=25, help="maximum number of actions")
    explore.add_argument("--shell", action="store_true",
                         help="treat TARGET as a command to try out with one-off shell commands")
    explore.add_argument("--workspace", help="workspace folder (default: current folder)")
    sub.add_parser("doctor", help="check that a model and a browser are ready")

    args = parser.parse_args(argv)
    handler = {None: cmd_app, "app": cmd_app, "run": cmd_run, "explore": cmd_explore, "doctor": cmd_doctor}
    try:
        return handler[args.command](args)
    except (StoreError, SpecError) as exc:
        print(f"argus2: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
