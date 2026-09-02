"""Typer entrypoint: `agency run`, `agency repl`, `agency doctor`."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel

from agency.agents.export import copy_export, export_roster
from agency.agents.team import TEAMS, get_team
from agency.capabilities.model import Capability, GrantError, Grants
from agency.capabilities.store import CapabilityStore, make_capability_store
from agency.config import AuthSource, Profile, get_settings
from agency.hooks.delegation import delegations
from agency.obs.cost import RunRecord
from agency.obs.logging import configure_logging
from agency.runner import Agent, Emit, resolve_grants, run_once

app = typer.Typer(add_completion=False, help="Agentic development harness.")
console = Console()

AGENT_AUTH_HELP: dict[AuthSource, str] = {
    "auth-token": "ANTHROPIC_AUTH_TOKEN (bearer token, e.g. an LLM gateway)",
    "api-key": "ANTHROPIC_API_KEY -- billed to that key's prepaid Console credits",
    "oauth-token": "CLAUDE_CODE_OAUTH_TOKEN -- billed to your Claude subscription",
    "cli-login": (
        "no credential configured -- falling back to the `claude login` session "
        "cached on this host, which expires"
    ),
}
"""What `doctor` prints for each credential source. Says where the *billing* lands as
well as which variable won, because "which account is this spending" is the question
someone actually has when they run `doctor`."""


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


GrantOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--grant",
        "-g",
        help="Capability pack for the main thread. Repeatable. See `agency capabilities`.",
    ),
]
GrantTeamOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--grant-team",
        help=(
            "Capability pack for every worker. A worker whose ceiling forbids it is "
            "skipped, and the skip is printed. Repeatable."
        ),
    ),
]
GrantAgentOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--grant-agent",
        help="Capability pack for one worker, as agent:pack. Repeatable.",
    ),
]


def _parse_grants(
    lead: list[str] | None, team: list[str] | None, agents: list[str] | None
) -> Grants:
    """Build a `Grants` from repeated options.

    Repeated options rather than one comma-separated string: typer handles `list[str]`
    natively, and a comma-split would invent a delimiter that capability and skill names
    are already forbidden from containing.
    """
    per_agent: dict[str, tuple[str, ...]] = {}
    for pair in agents or []:
        name, _, pack = pair.partition(":")
        if not name or not pack:
            raise GrantError(
                f"--grant-agent expects agent:pack, got {pair!r} "
                "(for example: --grant-agent developer:web-research)"
            )
        per_agent[name] = (*per_agent.get(name, ()), pack)
    return Grants(
        lead=tuple(lead or ()), team=tuple(team or ()), agents=per_agent
    )


async def _resolve(grants: Grants, team: str | None):  # type: ignore[no-untyped-def]
    """Resolve against whatever store this configuration implies (seed, or database)."""
    settings = get_settings()
    return await resolve_grants(
        make_capability_store(settings), grants, team=team, project_root=Path.cwd()
    )


def _report_skips(resolved: object) -> None:
    """Print what a ceiling declined.

    A team grant that silently did not reach a worker is the failure this whole design
    exists to avoid, so it is surfaced at the point of use, not only in a log line.
    """
    for item in getattr(resolved, "skipped", ()):  # pragma: no branch
        console.print(
            f"[yellow]skipped[/yellow] {item.capability} for {item.subject}: "
            f"{', '.join(item.barred_tools)} not permitted"
        )


def _grant_line(resolved: object) -> str:
    """One-line summary of what a run is holding, for the banner."""
    summary = resolved.summary()  # type: ignore[attr-defined]
    return "  ".join(f"{scope}={','.join(packs)}" for scope, packs in sorted(summary.items()))


def _setup(profile: str | None) -> Profile:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    return (profile or settings.profile)  # type: ignore[return-value]


@app.command()
def run(
    prompt: Annotated[str, typer.Argument(help="What the agent should do.")],
    profile: Annotated[str | None, typer.Option("--profile", "-p")] = None,
    thinking: Annotated[bool, typer.Option("--thinking")] = False,
    grant: GrantOpt = None,
) -> None:
    """Run a single prompt and exit."""
    active = _setup(profile)
    try:
        # Solo mode: the "lead" is the agent itself, so --grant is the only scope that
        # means anything here. Resolution happens before the run for the same reason it
        # does in the API -- a typo must fail before a subprocess is spawned.
        resolved = asyncio.run(_resolve(_parse_grants(grant, None, None), None))
    except GrantError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    record = asyncio.run(
        run_once(
            prompt,
            profile=active,
            project_root=Path.cwd(),
            emit=_make_emit(thinking),
            grants=resolved,
        )
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
            f"agent: [bold]{settings.agent_auth_source()}[/bold]  |  "
            f"docs: {'/docs, /reference' if settings.api_docs_enabled else 'disabled'}",
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
    backend = settings.database_backend()
    console.print(
        f"database:  {backend}"
        + (
            "  [dim](capability packs, run history and agent state are in-process)[/dim]"
            if backend == "none"
            else ""
        )
    )
    console.print(
        "cap-admin: "
        + (
            "enabled"
            if settings.admin_api_key
            else "[dim]disabled (AGENCY_ADMIN_API_KEY unset)[/dim]"
        )
    )
    # The agent's own credentials, which are not the API's. "cli-login" is legitimate
    # locally and a landmine on a server: it means every run depends on a `claude login`
    # cached in ~/.claude, and the day it expires every run returns "OAuth session
    # expired and could not be refreshed".
    console.print(f"agent:     {AGENT_AUTH_HELP[settings.agent_auth_source()]}")
    shadowed = settings.agent_auth_conflicts()
    if shadowed:
        # Legal, silent, and nearly always a mistake -- see Settings.agent_auth_conflicts.
        console.print(
            f"[yellow]           warning:[/yellow] {', '.join(shadowed)} also set but "
            f"outranked by {settings.agent_auth_var()}. Unset it if you meant to switch."
        )



@app.command()
def team(
    prompt: Annotated[str, typer.Argument(help="What the manager should get done.")],
    team_name: Annotated[
        str | None,
        typer.Option(
            "--team",
            "-t",
            help="Which team to run as manager+workers. See `agency teams`.",
        ),
    ] = None,
    profile: Annotated[str | None, typer.Option("--profile", "-p")] = None,
    thinking: Annotated[bool, typer.Option("--thinking")] = False,
    interactive: Annotated[
        bool,
        typer.Option("--interactive", "-i", help="Stay in session after the first turn."),
    ] = False,
    grant: GrantOpt = None,
    grant_team: GrantTeamOpt = None,
    grant_agent: GrantAgentOpt = None,
) -> None:
    """Talk to a manager agent. It delegates to the workers of the selected team.

    The manager is the main thread -- you are talking to it directly. Workers only
    exist inside Agent tool calls (magenta arrows in the output). Defaults to the
    server's configured default team (`AGENCY_DEFAULT_TEAM`); run `agency teams` to
    see what is available.
    """
    settings = get_settings()
    active = _setup(profile)
    selected = team_name or settings.default_team
    try:
        team_def = get_team(selected)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    emit = _make_emit(thinking)
    roster = ", ".join(sorted(team_def.workers))
    try:
        resolved = asyncio.run(
            _resolve(_parse_grants(grant, grant_team, grant_agent), selected)
        )
    except GrantError as exc:
        # Exit 2, matching the unknown-team path above: a typo is a usage error, and an
        # unhandled traceback is a worse experience than the one this command ships.
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    _report_skips(resolved)

    async def loop() -> None:
        async with Agent(
            profile=active, project_root=Path.cwd(), emit=emit, team=selected,
            grants=resolved,
        ) as agent:
            console.print(
                Panel(
                    f"profile: [bold]{active}[/bold]  |  team: [bold]{selected}[/bold]  |  "
                    f"workers: [bold]{roster}[/bold]"
                    + (
                        f"\ngrants: [bold]{_grant_line(resolved)}[/bold]"
                        if not resolved.is_empty()
                        else ""
                    ),
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


@app.command()
def capabilities(
    seed: Annotated[
        bool,
        typer.Option("--seed", help="Write the in-code seed registry into the database."),
    ] = False,
) -> None:
    """List capability packs: what each grants, and which agents may not hold it.

    The `barred` column is the point. A ceiling nobody can discover before they hit it
    reads as an arbitrary refusal, so it is printed next to the pack rather than left to
    surface as a 422 at request time.
    """
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    store = make_capability_store(settings)

    if seed:
        if settings.resolved_database_url() is None:
            console.print(
                "[red]no database configured[/red] -- packs come from "
                "agency/capabilities/seed.py and there is nowhere to seed them to. "
                "Set AGENCY_DATABASE_URL first."
            )
            raise typer.Exit(code=2)
        from agency.capabilities.seed import CAPABILITIES as SEED

        written = asyncio.run(_seed(store, SEED))  # noqa: F821
        console.print(
            f"seeded: [bold]{', '.join(written) if written else 'nothing new'}[/bold] "
            f"({len(SEED)} packs in the seed registry)"
        )
        return

    registry = asyncio.run(store.registry())
    source = "database" if settings.resolved_database_url() else "in-code seed"
    console.print(f"[dim]source: {source}[/dim]")
    for name in sorted(registry):
        capability = registry[name]
        barred = sorted(_barred_agents(capability))
        body = f"{capability.description}\n"
        if capability.tools:
            body += f"\ntools:  {', '.join(capability.tools)}"
        if capability.skills:
            body += f"\nskills: {', '.join(capability.skills)}"
        if capability.mcp_servers:
            body += f"\nmcp:    {', '.join(capability.mcp_servers)}"
        if barred:
            body += f"\n[yellow]barred from:[/yellow] {', '.join(barred)}"
        title = name + (f"  [dim](rev {capability.revision})[/dim]" if capability.revision else "")
        console.print(Panel(body, title=title, expand=False))
    if not registry:
        console.print("[yellow]no capabilities configured[/yellow]")


def _barred_agents(capability: object) -> list[str]:
    from agency.agents.definitions import AGENTS
    from agency.agents.team import TEAMS
    from agency.capabilities.resolve import barred_tools

    names = set(AGENTS)
    for definition in TEAMS.values():
        names |= set(definition.workers)
    return [name for name in names if barred_tools(name, capability)]  # type: ignore[arg-type]


async def _seed(store: CapabilityStore, packs: Mapping[str, Capability]) -> list[str]:
    """Unwrap the TTL cache and write the seed registry through the database store.

    The cache is a read-path decorator with no write methods, so seeding has to reach the
    store it wraps. Typed as a hard failure rather than a silent no-op: this is only ever
    called after the caller has checked that a database is configured.
    """
    from agency.capabilities.db_store import DbCapabilityStore, seed_registry
    from agency.capabilities.store import CachedCapabilityStore

    inner = store._inner if isinstance(store, CachedCapabilityStore) else store  # noqa: SLF001
    if not isinstance(inner, DbCapabilityStore):
        raise RuntimeError(f"cannot seed a {type(inner).__name__}: no database configured")
    return await seed_registry(inner, packs)


@app.command()
def migrate(
    revision: Annotated[str, typer.Argument(help="Target revision.")] = "head",
) -> None:
    """Run database migrations.

    A deploy step, never something the app does at startup: with `uvicorn --workers N`
    that would be N processes racing to migrate one schema.
    """
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    if settings.resolved_database_url() is None:
        console.print("[red]AGENCY_DATABASE_URL is not set.[/red] Nothing to migrate.")
        raise typer.Exit(code=2)
    try:
        from alembic import command
        from alembic.config import Config
    except ModuleNotFoundError as exc:
        console.print(
            'missing dependency: install the DB extra -- '
            '[bold]pip install -e ".[db]"[/bold]'
        )
        raise typer.Exit(code=1) from exc

    config = Config("alembic.ini")
    command.upgrade(config, revision)
    console.print(f"migrated to [bold]{revision}[/bold] ({settings.database_backend()})")


@app.command()
def teams() -> None:
    """List the teams a manager can run as, and each one's workers."""
    default = get_settings().default_team
    for name, definition in sorted(TEAMS.items()):
        roster = ", ".join(sorted(definition.workers)) or "[dim]none[/dim]"
        label = f"team: {name}" + ("  [dim](default)[/dim]" if name == default else "")
        console.print(Panel(f"workers: {roster}", title=label, expand=False))


@app.command()
def export_agents(
    out: Annotated[
        Path,
        typer.Option(
            "--out",
            help="Directory to write into. Regenerated in place; stale files are removed.",
        ),
    ] = Path("exports"),
    copy_to: Annotated[
        Path | None,
        typer.Option(
            "--copy-to",
            help=(
                "Also copy the generated tree into this directory, creating it if needed. "
                "Additive: existing files of the same name are overwritten, nothing else "
                "is removed."
            ),
        ),
    ] = None,
) -> None:
    """Write every agent in the roster to a markdown file under OUT.

    One-way. The Python definitions stay the source of truth and nothing reads these files
    back, so edit `agents/definitions.py` or `agents/team.py` and re-run rather than
    editing the output. `OUT/agents/` is the part shaped for `.claude/agents/`; the
    manager prompts sit beside it, deliberately outside that directory.

    `--copy-to DIR` mirrors the same tree into a second place -- another repo, a plugin,
    somewhere synced -- so a consumer does not go a roster behind. OUT is still generated
    and pruned as usual; the copy is additive and never deletes, because that directory
    may hold files this exporter did not write.
    """
    written, removed = export_roster(out)
    listing = "\n".join(str(path) for path in written)
    console.print(Panel(listing, title=f"wrote {len(written)} files", expand=False))
    for path in removed:
        console.print(f"[dim]removed stale {path}[/dim]")

    if copy_to is not None:
        try:
            copied = copy_export(written, out, copy_to)
        except (OSError, ValueError) as exc:
            console.print(f"[red]could not copy to {copy_to}:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        console.print(
            Panel(
                "\n".join(str(path) for path in copied),
                title=f"copied {len(copied)} files to {copy_to}",
                expand=False,
            )
        )


if __name__ == "__main__":
    app()
