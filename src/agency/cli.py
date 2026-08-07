"""Typer entrypoint: `agency run`, `agency repl`, `agency doctor`."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel

from agency.config import Profile, get_settings
from agency.obs.logging import configure_logging
from agency.runner import Agent, Emit, run_once

app = typer.Typer(add_completion=False, help="Agentic development harness.")
console = Console()


def _make_emit(show_thinking: bool) -> Emit:
    def emit(kind: str, text: str) -> None:
        if kind == "text":
            console.print(text)
        elif kind == "thinking" and show_thinking:
            console.print(f"[dim italic]{text}[/dim italic]")
        elif kind == "tool":
            console.print(f"[cyan]→ {text}[/cyan]")
        elif kind == "result":
            console.print(f"[dim]{text}[/dim]")

    return emit


def _setup(profile: str | None) -> Profile:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    return (profile or settings.profile)  # type: ignore[return-value]


@app.command()
def run(
    prompt: Annotated[str, typer.Argument(help="What the agent should do.")],
    profile: Annotated[str | None, typer.Option("--profile", "-p")] = None,
    thinking: Annotated[bool, typer.Option("--thinking")] = False,
) -> None:
    """Run a single prompt and exit."""
    active = _setup(profile)
    record = asyncio.run(
        run_once(prompt, profile=active, project_root=Path.cwd(), emit=_make_emit(thinking))
    )
    console.print(Panel(record.summary(), title=f"run {record.run_id}", expand=False))
    raise typer.Exit(code=1 if record.is_error else 0)


@app.command()
def repl(
    profile: Annotated[str | None, typer.Option("--profile", "-p")] = None,
    thinking: Annotated[bool, typer.Option("--thinking")] = False,
) -> None:
    """Interactive multi-turn session. Context is retained between prompts."""
    active = _setup(profile)
    emit = _make_emit(thinking)

    async def loop() -> None:
        async with Agent(profile=active, project_root=Path.cwd(), emit=emit) as agent:
            console.print(
                Panel(
                    f"profile: [bold]{active}[/bold]  |  "
                    "type your prompt, or /exit to quit",
                    title="agency repl",
                    expand=False,
                )
            )
            while True:
                try:
                    prompt = console.input("\n[bold green]>[/bold green] ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not prompt:
                    continue
                if prompt in {"/exit", "/quit"}:
                    break
                record = await agent.ask(prompt)
                console.print(f"[dim]{record.summary()}[/dim]")

    asyncio.run(loop())
    console.print("[dim]session ended[/dim]")


@app.command()
def doctor() -> None:
    """Print resolved configuration. Useful when a run behaves unexpectedly."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    console.print(Panel.fit(str(settings), title="settings"))
    console.print(f"workspace: {settings.resolved_workspace()}")
    console.print(f"runs:      {settings.resolved_run_dir()}")
    console.print(f".mcp.json: {'found' if Path('.mcp.json').exists() else 'absent'}")


if __name__ == "__main__":
    app()
