"""Argus CLI — ``argus init / run / roam / providers / tokens / gui``."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

from argus import __version__
from argus.config import init_project, load_config
from argus.engine.results import RunResult, StepResult, load_runs
from argus.engine.spec import SpecError, discover_tests, load_spec
from argus.providers.base import ProviderError
from argus.tokens import TokenTracker

console = Console(highlight=False)

_GLYPH = {"pass": "[green]✓[/green]", "fail": "[red]✗[/red]",
          "error": "[orange3]✗[/orange3]", "skipped": "[dim]⊘[/dim]"}


@click.group()
@click.version_option(__version__, prog_name="argus")
def main() -> None:
    """Argus — universal application testing driven by multimodal LLMs."""


@main.command()
def init() -> None:
    """Create .argus/ with a starter config and example test."""
    argus_dir = init_project()
    console.print(f"[green]✓[/green] initialized [bold]{argus_dir}[/bold]")
    console.print("  edit [cyan].argus/config.yaml[/cyan] to pick your provider/model")
    console.print("  example test: [cyan].argus/notepad.test.yaml[/cyan]")
    console.print("  then: [bold]argus run[/bold]")


@main.command()
@click.argument("test", required=False)
@click.option("--minutes", type=float, default=None, help="Time budget in minutes.")
@click.option("--max-tokens", type=int, default=None,
              help="Token budget (ignored for ollama — it is local and free).")
@click.option("--dry-run", is_flag=True, help="Parse specs and show steps without running them.")
def run(test: Optional[str], minutes: Optional[float], max_tokens: Optional[int],
        dry_run: bool = False) -> None:
    """Run one test file, or every .argus/*.test.yaml when TEST is omitted."""
    cfg = load_config()
    paths = _resolve_tests(test, cfg.project_dir)

    tracker = TokenTracker()
    try:
        provider = cfg.make_provider(tracker)
    except ProviderError as exc:
        _die(str(exc))

    console.print(
        f"[dim]Argus v{__version__} · provider:[/dim] [cyan]{provider.describe()}[/cyan]"
    )

    exit_code = 0
    for path in paths:
        try:
            spec = load_spec(path)
        except SpecError as exc:
            console.print(f"[red]✗[/red] {path.name}: {exc}")
            exit_code = max(exit_code, 2)
            continue

        if dry_run:
            console.print(f"\n[bold]{path.name}[/bold] ({len(spec.steps)} steps, adapter: {spec.adapter})")
            for i, step in enumerate(spec.steps):
                from argus.engine.spec import AssertStep
                kind = step.kind
                text = step.describe() if isinstance(step, AssertStep) else step.text
                console.print(f"  {i+1}. [{kind}] {text}")
            continue

        from argus.adapters import AdapterError, create_adapter

        try:
            adapter = create_adapter(spec.adapter)
        except AdapterError as exc:
            console.print(f"[red]✗[/red] {path.name}: {exc}")
            exit_code = max(exit_code, 2)
            continue

        console.print(f"\n[dim]Running[/dim] [bold]{path.name}[/bold][dim]…[/dim]")
        budget = cfg.make_budget(tracker, minutes, max_tokens)

        from argus.engine.runner import run_test

        import time as _time
        stamp = _time.strftime("%Y%m%d-%H%M%S")
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in path.name)
        shots_dir = cfg.argus_dir / "runs" / f"{stamp}-{safe}" / "shots"
        ks = cfg.make_knowledge_store()
        result = run_test(
            spec, provider, adapter, budget,
            on_step=_print_step,
            warn=lambda msg: console.print(f"[yellow]![/yellow] {msg}"),
            knowledge_store=ks,
            shots_dir=shots_dir,
            project_dir=cfg.project_dir,
        )
        if ks is not None:
            ks.close()
        if result.error:
            console.print(f"[orange3]✗ {result.error}[/orange3]")
        result.save(cfg.project_dir)
        _print_summary(result)
        exit_code = max(exit_code, result.exit_code)

    tracker.persist(cfg.project_dir)
    sys.exit(exit_code)


def _print_step(sr: StepResult) -> None:
    glyph = _GLYPH.get(sr.status, "[dim]·[/dim]")
    dur = f"[dim]{sr.duration_s:.2f}s[/dim]" if sr.duration_s else ""
    console.print(f"  {glyph}  {sr.text}  {dur}")
    for act in sr.actions:
        console.print(f"     [dim]↳ {act}[/dim]")
    if sr.status == "fail" and sr.expected:
        console.print(f"       [dim]exp[/dim]  {sr.expected}")
        console.print(f"       [red]act[/red]  {sr.actual}")
    elif sr.note and sr.status != "pass":
        console.print(f"       [dim]{sr.note}[/dim]")


def _print_summary(result: RunResult) -> None:
    tokens = result.tokens.get("total_tokens", 0)
    console.print(
        f"\n[green]{result.passed} passed[/green] · "
        f"[red]{result.failed} failed[/red] · "
        f"[dim]{result.skipped} skipped[/dim] · {result.duration_s:.1f}s · "
        f"{tokens} tokens · exit {result.exit_code}"
    )


def _resolve_tests(test: Optional[str], project_dir: Path) -> list:
    if test:
        path = Path(test)
        if not path.exists():
            path = project_dir / ".argus" / test
        if not path.exists():
            _die(f"test file not found: {test}")
        return [path]
    paths = discover_tests(project_dir)
    if not paths:
        _die("no tests found — `argus init` to get started.")
    return paths


@main.command()
@click.argument("target")
@click.option("--minutes", type=float, default=None,
              help="Time budget in minutes (default: budgets.time_minutes from config).")
@click.option("--max-tokens", type=int, default=None,
              help="Token budget (ignored for ollama — it is local and free).")
@click.option("--no-regressions", is_flag=True,
              help="Skip generating regression test stubs for findings.")
@click.option("--adapter", "adapter_type", default="desktop-gui", show_default=True,
              help="Adapter to use: desktop-gui | cli | browser.")
@click.option("--memory/--no-memory", default=True, show_default=True,
              help="Persist explored paths across sessions for this target.")
def roam(target: str, minutes: Optional[float], max_tokens: Optional[int],
         no_regressions: bool, adapter_type: str, memory: bool) -> None:
    """Let the LLM free-roam TARGET to find bugs and write a report."""
    cfg = load_config()
    tracker = TokenTracker()
    try:
        provider = cfg.make_provider(tracker)
    except ProviderError as exc:
        _die(str(exc))

    from argus.adapters import AdapterError, create_adapter

    try:
        adapter = create_adapter(adapter_type)
    except AdapterError as exc:
        _die(str(exc))

    budget = cfg.make_budget(tracker, minutes, max_tokens)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    session_dir = cfg.argus_dir / "roam" / stamp
    memory_dir = cfg.argus_dir / "roam" / "memory" if memory else None

    console.print(
        f"[dim]Argus v{__version__} · provider:[/dim] [cyan]{provider.describe()}[/cyan] "
        f"[dim]· adapter:[/dim] {adapter_type} [dim]· budget:[/dim] {budget.describe()}"
    )
    if sys.platform == "win32" and adapter_type == "desktop-gui":
        inner = getattr(adapter, "inner", adapter)
        if inner.__class__.__name__ == "SafeWindowsGUIAdapter":
            console.print(
                "[green]✓[/green] safe semantic Windows input active — "
                "physical mouse/keyboard injection is disabled."
            )
        else:
            console.print(
                "[yellow]![/yellow] physical/legacy Windows input is active — roaming may "
                "move the host mouse, inject keyboard input, and change foreground focus."
            )

    from argus.engine.roam import roam as run_roam

    ks = cfg.make_knowledge_store()

    session = run_roam(
        target=target,
        provider=provider,
        adapter=adapter,
        budget=budget,
        session_dir=session_dir,
        on_event=lambda line: console.print(f"  [dim]{line}[/dim]"),
        generate_regressions=not no_regressions,
        memory_dir=memory_dir,
        knowledge_store=ks,
    )
    if ks is not None:
        ks.close()
    tracker.persist(cfg.project_dir)

    console.print(
        f"\n[bold]{len(session.findings)} finding(s)[/bold] · "
        f"{len(session.actions)} actions · "
        f"{session.tokens.get('total_tokens', 0)} tokens"
    )
    console.print(f"report: [cyan]{session_dir / 'report.md'}[/cyan]")
    sys.exit(0 if not session.findings else 1)


@main.command()
@click.argument("test", required=False)
@click.option("--minutes", type=float, default=None)
@click.option("--max-tokens", type=int, default=None)
def watch(test: Optional[str], minutes: Optional[float], max_tokens: Optional[int]) -> None:
    """Re-run tests automatically whenever a .test.yaml file changes."""
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        _die(
            "watchdog is required for `argus watch` — "
            "install with: pip install argus-app-testing[watch]"
        )

    cfg = load_config()

    class _Handler(FileSystemEventHandler):
        def on_modified(self, event):
            if event.src_path.endswith(".test.yaml"):
                console.print(f"\n[yellow]![/yellow] {event.src_path} changed — re-running…")
                _run_all(test, cfg, minutes, max_tokens)

    console.print(f"[dim]Watching[/dim] [bold]{cfg.argus_dir}[/bold] for changes…")
    console.print("[dim](press Ctrl-C to stop)[/dim]")
    _run_all(test, cfg, minutes, max_tokens)

    observer = Observer()
    observer.schedule(_Handler(), str(cfg.argus_dir), recursive=False)
    observer.start()
    try:
        while observer.is_alive():
            observer.join(timeout=1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


def _run_all(test: Optional[str], cfg, minutes, max_tokens) -> None:
    from argus.adapters import AdapterError, create_adapter
    from argus.engine.runner import run_test

    paths = _resolve_tests(test, cfg.project_dir)
    tracker = TokenTracker()
    try:
        provider = cfg.make_provider(tracker)
    except ProviderError as exc:
        console.print(f"[red]✗[/red] {exc}")
        return
    for path in paths:
        try:
            spec = load_spec(path)
        except SpecError as exc:
            console.print(f"[red]✗[/red] {path.name}: {exc}")
            continue
        try:
            adapter = create_adapter(spec.adapter)
        except AdapterError as exc:
            console.print(f"[red]✗[/red] {path.name}: {exc}")
            continue
        budget = cfg.make_budget(tracker, minutes, max_tokens)
        result = run_test(
            spec,
            provider,
            adapter,
            budget,
            on_step=_print_step,
            warn=lambda m: console.print(f"[yellow]![/yellow] {m}"),
            project_dir=cfg.project_dir,
        )
        result.save(cfg.project_dir)
        _print_summary(result)


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=5000, show_default=True)
@click.option("--debug", is_flag=True)
def serve(host: str, port: int, debug: bool) -> None:
    """Start the Argus web dashboard (requires Flask)."""
    try:
        from argus.serve.app import create_app
    except ImportError:
        _die(
            "Flask is required for `argus serve` — "
            "install with: pip install argus-app-testing[serve]"
        )
    cfg = load_config()
    app = create_app(cfg)
    console.print(
        f"[green]✓[/green] Argus dashboard at [cyan]http://{host}:{port}[/cyan]"
    )
    app.run(host=host, port=port, debug=debug)


@main.command()
def providers() -> None:
    """Show the configured provider and check the connection + vision support."""
    cfg = load_config()
    tracker = TokenTracker()
    console.print(f"[dim]active provider:[/dim] [cyan]{cfg.provider.type}[/cyan] "
                  f"[dim]model:[/dim] [cyan]{cfg.provider.model}[/cyan]")
    try:
        provider = cfg.make_provider(tracker)
    except ProviderError as exc:
        _die(str(exc))
    with console.status("checking connection…"):
        status = provider.check_connection()
    glyph = "[green]✓[/green]" if status["ok"] else "[red]✗[/red]"
    console.print(f"{glyph} {status['detail']}")
    if status.get("ok") and not status.get("vision", True):
        console.print(
            "[yellow]![/yellow] this model cannot do any vision-related testing — "
            "Argus will use the accessibility tree only. "
            "For vision, pull a multimodal model (e.g. `ollama pull gemma3:9b`)."
        )
    sys.exit(0 if status["ok"] else 1)


@main.command()
def tokens() -> None:
    """Show cumulative token usage for this project."""
    cfg = load_config()
    data = TokenTracker.load_persisted(cfg.project_dir)
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_row("prompt tokens", f"[bold]{data['prompt_tokens']:,}[/bold]")
    table.add_row("completion tokens", f"[bold]{data['completion_tokens']:,}[/bold]")
    table.add_row("total tokens", f"[bold]{data['total_tokens']:,}[/bold]")
    table.add_row("LLM calls", f"[bold]{data['calls']:,}[/bold]")
    console.print(table)


@main.command()
@click.option("--limit", type=int, default=10, help="How many recent runs to show.")
def report(limit: int) -> None:
    """Show recent run history."""
    cfg = load_config()
    runs = load_runs(cfg.project_dir, limit)
    if not runs:
        console.print("[dim]No runs yet — `argus run` to get started.[/dim]")
        return
    table = Table(header_style="dim")
    table.add_column("test")
    table.add_column("status")
    table.add_column("steps", justify="right")
    table.add_column("duration", justify="right")
    table.add_column("tokens", justify="right")
    table.add_column("provider")
    for r in runs:
        status = r.get("status", "?")
        color = {"pass": "green", "fail": "red", "error": "orange3"}.get(status, "dim")
        steps = r.get("steps", [])
        passed = sum(1 for s in steps if s.get("status") == "pass")
        table.add_row(
            r.get("test_file", "?"),
            f"[{color}]{status}[/{color}]",
            f"{passed}/{len(steps)}",
            f"{r.get('duration_s', 0):.1f}s",
            str(r.get("tokens", {}).get("total_tokens", 0)),
            r.get("provider", "?"),
        )
    console.print(table)


@main.command()
def gui() -> None:
    """Open the Argus desktop app."""
    try:
        from argus.gui.app import run_gui
    except ImportError:
        _die(
            "pywebview is required for the desktop app — "
            "install with: pip install argus-app-testing[gui]"
        )
    run_gui()


def _die(message: str) -> None:
    console.print(f"[red]✗[/red] {message}")
    sys.exit(2)


@main.group()
def knowledge() -> None:
    """Manage the persistent knowledge graph and vector store."""


@knowledge.command("stats")
@click.option("--target", default=None, help="Filter to a specific target name.")
def knowledge_stats(target: Optional[str]) -> None:
    """Show knowledge graph statistics (states, transitions, bug nodes)."""
    cfg = load_config()
    ks = cfg.make_knowledge_store()
    if ks is None:
        console.print("[yellow]![/yellow] Knowledge store is disabled or unavailable.")
        return
    stats = ks.get_stats(target)
    ks.close()
    if not stats:
        console.print("[dim]No knowledge recorded yet.[/dim]")
        return
    table = Table(header_style="dim")
    table.add_column("target")
    table.add_column("states", justify="right")
    table.add_column("transitions", justify="right")
    table.add_column("bug nodes", justify="right")
    for tgt, s in stats.items():
        table.add_row(
            tgt,
            str(s.get("states", 0)),
            str(s.get("transitions", 0)),
            f"[red]{s.get('bug_nodes', 0)}[/red]" if s.get("bug_nodes") else "0",
        )
    console.print(table)


@knowledge.command("reset")
@click.argument("target")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt.")
def knowledge_reset(target: str, yes: bool) -> None:
    """Delete all stored knowledge for TARGET."""
    if not yes:
        click.confirm(f"Delete all knowledge for '{target}'?", abort=True)
    cfg = load_config()
    ks = cfg.make_knowledge_store()
    if ks is None:
        console.print("[yellow]![/yellow] Knowledge store is disabled or unavailable.")
        return
    ks.clear_target(target)
    ks.close()
    console.print(f"[green]✓[/green] Knowledge cleared for '{target}'.")


@knowledge.command("export")
@click.argument("target")
@click.option("--out", default=None, help="Output file path (default: <target>.graph.json).")
def knowledge_export(target: str, out: Optional[str]) -> None:
    """Export the state graph for TARGET as JSON."""
    cfg = load_config()
    persist_dir = cfg.argus_dir / "knowledge"
    from argus.knowledge.fingerprint import target_key
    key = target_key(target)
    graph_path = persist_dir / f"{key}.graph.json"
    if not graph_path.exists():
        console.print(f"[red]✗[/red] No graph found for '{target}' at {graph_path}")
        sys.exit(1)
    dest = Path(out) if out else Path(f"{key}.graph.json")
    import shutil
    shutil.copy2(graph_path, dest)
    console.print(f"[green]✓[/green] Exported to {dest}")


@knowledge.group("docker")
def knowledge_docker() -> None:
    """Manage Docker-backed knowledge services (Qdrant)."""


@knowledge_docker.command("up")
def knowledge_docker_up() -> None:
    """Start argus-qdrant Docker container."""
    from argus.knowledge.docker_manager import DockerManager
    cfg = load_config()
    mgr = DockerManager(cfg.argus_dir)
    if not mgr.available():
        console.print("[red]✗[/red] Docker is not available on this system.")
        sys.exit(1)
    with console.status("Starting argus-qdrant…"):
        url = mgr.ensure_qdrant()
    if url:
        console.print(f"[green]✓[/green] Qdrant running at {url}")
    else:
        console.print("[red]✗[/red] Failed to start Qdrant container.")
        sys.exit(1)


@knowledge_docker.command("down")
def knowledge_docker_down() -> None:
    """Stop argus-qdrant Docker container."""
    from argus.knowledge.docker_manager import DockerManager
    cfg = load_config()
    mgr = DockerManager(cfg.argus_dir)
    mgr.stop()
    console.print("[green]✓[/green] Knowledge Docker services stopped.")


@knowledge_docker.command("status")
def knowledge_docker_status() -> None:
    """Show running state of knowledge Docker containers."""
    from argus.knowledge.docker_manager import DockerManager
    cfg = load_config()
    mgr = DockerManager(cfg.argus_dir)
    s = mgr.status()
    for name, running in s.items():
        glyph = "[green]●[/green]" if running else "[dim]○[/dim]"
        state = "running" if running else "stopped"
        console.print(f"  {glyph}  {name}: {state}")


if __name__ == "__main__":
    main()
