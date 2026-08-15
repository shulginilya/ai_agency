"""Typer entrypoint: `agency run`, `agency repl`, `agency doctor`."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel

from agency.agents.team import TEAM
from agency.config import Profile, get_settings
from agency.hooks.delegation import delegations
from agency.obs.cost import RunRecord
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
            # Agent/Task == a delegation. Colour it differently so the shape of a run
            # is legible at a glance: magenta lines are the manager handing off work.
            style = "magenta" if text in ("Agent", "Task") else "cyan"
            console.print(f"[{style}]→ {text}[/{style}]")
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
def serve(
    host: Annotated[str | None, typer.Option("--host", help="Override AGENCY_API_HOST.")] = None,
    port: Annotated[int | None, typer.Option("--port", help="Override AGENCY_API_PORT.")] = None,
    reload: Annotated[bool, typer.Option("--reload", help="Autoreload on code change.")] = False,
) -> None:
    """Serve the HTTP API.

    Needs the optional web dependencies: `pip install -e ".[api]"`.
    """
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    try:
        from agency.api.app import serve as _serve
    except ModuleNotFoundError as exc:  # fastapi / uvicorn not installed
        console.print(
            f"[red]missing dependency:[/red] {exc.name}\n"
            'install the API extra:  [bold]pip install -e ".[api]"[/bold]'
            "  (or [bold]uv sync --extra api[/bold])"
        )
        raise typer.Exit(code=1) from exc

    if host is not None or port is not None:
        settings = settings.model_copy(
            update={
                "api_host": settings.api_host if host is None else host,
                "api_port": settings.api_port if port is None else port,
            }
        )

    console.print(
        Panel(
            f"http://{settings.api_host}:{settings.api_port}\n"
            f"profile: [bold]{settings.profile}[/bold]  |  "
            f"auth: [bold]{'api-key' if settings.api_key else 'open'}[/bold]  |  "
            f"docs: {'/docs' if settings.api_docs_enabled else 'disabled'}",
            title="agency serve",
            expand=False,
        )
    )

    if reload:
        # Reload spawns a fresh worker process, so uvicorn needs an import string it can
        # re-import -- not an app object. That is what `agency.api.asgi` exists for.
        import uvicorn

        uvicorn.run(
            "agency.api.asgi:app",
            host=settings.api_host,
            port=settings.api_port,
            reload=True,
            log_config=None,
            access_log=False,
        )
        return

    _serve(settings)


@app.command()
def doctor() -> None:
    """Print resolved configuration. Useful when a run behaves unexpectedly."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    console.print(Panel.fit(str(settings), title="settings"))
    console.print(f"workspace: {settings.resolved_workspace()}")
    console.print(f"runs:      {settings.resolved_run_dir()}")
    console.print(f".mcp.json: {'found' if Path('.mcp.json').exists() else 'absent'}")
    auth = "api-key" if settings.api_key else "open"
    console.print(f"api:       http://{settings.api_host}:{settings.api_port} ({auth})")



@app.command()
def team(
    prompt: Annotated[str, typer.Argument(help="What the manager should get done.")],
    profile: Annotated[str | None, typer.Option("--profile", "-p")] = None,
    thinking: Annotated[bool, typer.Option("--thinking")] = False,
    interactive: Annotated[
        bool,
        typer.Option("--interactive", "-i", help="Stay in session after the first turn."),
    ] = False,
) -> None:
    """Talk to the manager agent. It delegates to the workers in `agents/team.py`.

    The manager is the main thread -- you are talking to it directly. Workers only
    exist inside Agent tool calls (magenta arrows in the output).
    """
    active = _setup(profile)
    emit = _make_emit(thinking)
    roster = ", ".join(sorted(TEAM))

    async def loop() -> None:
        async with Agent(
            profile=active, project_root=Path.cwd(), emit=emit, team=True
        ) as agent:
            console.print(
                Panel(
                    f"profile: [bold]{active}[/bold]  |  workers: [bold]{roster}[/bold]",
                    title="agency team",
                    expand=False,
                )
            )
            record = await agent.ask(prompt)
            _report(record)
            while interactive:
                try:
                    follow_up = console.input("\n[bold green]>[/bold green] ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not follow_up or follow_up in {"/exit", "/quit"}:
                    break
                _report(await agent.ask(follow_up))

    asyncio.run(loop())


def _report(record: RunRecord) -> None:
    """Print the run summary plus who the manager actually delegated to."""
    used = delegations(record)
    console.print(
        Panel(
            f"{record.summary()}\n"
            f"delegated to: {', '.join(used) if used else '[yellow]nobody[/yellow]'}",
            title=f"run {record.run_id}",
            expand=False,
        )
    )


if __name__ == "__main__":
    app()
