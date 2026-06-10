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


# ---------------------------------------------------------------- init ----


@main.command()
def init() -> None:
    """Create .argus/ with a starter config and example test."""
    argus_dir = init_project()
    console.print(f"[green]✓[/green] initialized [bold]{argus_dir}[/bold]")
    console.print("  edit [cyan].argus/config.yaml[/cyan] to pick your provider/model")
    console.print("  example test: [cyan].argus/notepad.test.yaml[/cyan]")
    console.print("  then: [bold]argus run[/bold]")


# ----------------------------------------------------------------- run ----


@main.command()
@click.argument("test", required=False)
@click.option("--minutes", type=float, default=None, help="Time budget in minutes.")
@click.option("--max-tokens", type=int, default=None,
              help="Token budget (ignored for ollama — it is local and free).")
def run(test: Optional[str], minutes: Optional[float], max_tokens: Optional[int]) -> None:
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

        result = run_test(
            spec, provider, adapter, budget,
            on_step=_print_step,
            warn=lambda msg: console.print(f"[yellow]![/yellow] {msg}"),
        )
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


# ---------------------------------------------------------------- roam ----


@main.command()
@click.argument("target")
@click.option("--minutes", type=float, default=None,
              help="Time budget in minutes (default: budgets.time_minutes from config).")
@click.option("--max-tokens", type=int, default=None,
              help="Token budget (ignored for ollama — it is local and free).")
@click.option("--no-regressions", is_flag=True,
              help="Skip generating regression test stubs for findings.")
def roam(target: str, minutes: Optional[float], max_tokens: Optional[int],
         no_regressions: bool) -> None:
    """Let the LLM free-roam TARGET to find bugs and write a report.

    Example:  argus roam "notepad.exe" --minutes 5
    """
    cfg = load_config()
    tracker = TokenTracker()
    try:
        provider = cfg.make_provider(tracker)
    except ProviderError as exc:
        _die(str(exc))

    from argus.adapters import AdapterError, create_adapter

    try:
        adapter = create_adapter("desktop-gui")
    except AdapterError as exc:
        _die(str(exc))

    budget = cfg.make_budget(tracker, minutes, max_tokens)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    session_dir = cfg.argus_dir / "roam" / stamp

    console.print(
        f"[dim]Argus v{__version__} · provider:[/dim] [cyan]{provider.describe()}[/cyan] "
        f"[dim]· budget:[/dim] {budget.describe()}"
    )
    if sys.platform == "win32":
        console.print(
            "[yellow]![/yellow] roaming drives the real desktop on Windows — "
            "avoid using the mouse/keyboard while it runs."
        )

    from argus.engine.roam import roam as run_roam

    session = run_roam(
        target=target,
        provider=provider,
        adapter=adapter,
        budget=budget,
        session_dir=session_dir,
        on_event=lambda line: console.print(f"  [dim]{line}[/dim]"),
        generate_regressions=not no_regressions,
    )
    tracker.persist(cfg.project_dir)

    console.print(
        f"\n[bold]{len(session.findings)} finding(s)[/bold] · "
        f"{len(session.actions)} actions · "
        f"{session.tokens.get('total_tokens', 0)} tokens"
    )
    console.print(f"report: [cyan]{session_dir / 'report.md'}[/cyan]")
    sys.exit(0 if not session.findings else 1)


# ----------------------------------------------------------- providers ----


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


# --------------------------------------------------------------- tokens ----


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


# --------------------------------------------------------------- report ----


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


# ------------------------------------------------------------------ gui ----


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


if __name__ == "__main__":
    main()
