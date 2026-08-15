#!/usr/bin/env python3
"""Cross-platform task runner.

    python tasks.py check      # Windows, macOS, Linux -- no make required
    make check                 # same thing, on systems that have make

This is the single source of truth for project commands. The Makefile is a thin shim
that forwards to it, so Unix users can keep typing `make check` and Windows users get
the same behaviour without needing GNU Make.

Uses only the standard library so it runs before dependencies are installed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

ROOT = Path(__file__).parent.resolve()


def _uv_available() -> bool:
    return shutil.which("uv") is not None


def run(*argv: str, check: bool = True) -> int:
    """Run a command through uv when available, otherwise in the current interpreter."""
    if _uv_available():
        cmd = ["uv", "run", *argv]
    elif argv[0] == "python":
        cmd = [sys.executable, *argv[1:]]
    else:
        # Fall back to the tool as installed in the active environment.
        resolved = shutil.which(argv[0])
        if resolved is None:
            print(
                f"error: '{argv[0]}' not found and uv is not installed.\n"
                "Install uv (https://docs.astral.sh/uv/) or activate a venv with the "
                "dev extras: pip install -e \".[dev]\"",
                file=sys.stderr,
            )
            return 127
        cmd = [resolved, *argv[1:]]

    print(f"$ {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, cwd=ROOT)  # noqa: S603
    if check and result.returncode != 0:
        sys.exit(result.returncode)
    return result.returncode


# --- tasks -------------------------------------------------------------------------


def install(_rest: Sequence[str]) -> None:
    """Sync dependencies including dev and api extras."""
    # Both extras are named explicitly even though `dev` already depends on
    # `agency[api]`: self-referential extras resolve differently across pip versions,
    # and a missing FastAPI would only surface as a skipped test file.
    if _uv_available():
        subprocess.run(  # noqa: S603
            ["uv", "sync", "--extra", "dev", "--extra", "api"], cwd=ROOT, check=True  # noqa: S607
        )
    else:
        subprocess.run(  # noqa: S603
            [sys.executable, "-m", "pip", "install", "-e", ".[dev,api]"], cwd=ROOT, check=True
        )


def agent_run(rest: Sequence[str]) -> None:
    """Run one prompt and exit.  task run "your prompt here" """
    if not rest:
        print('usage: task run "your prompt"', file=sys.stderr)
        sys.exit(2)
    run("agency", "run", *rest)


def repl(rest: Sequence[str]) -> None:
    """Interactive multi-turn session."""
    run("agency", "repl", *rest)


def team(rest: Sequence[str]) -> None:
    """Talk to the manager agent.  task team "your prompt here" """
    if not rest:
        print('usage: task team "your prompt"', file=sys.stderr)
        sys.exit(2)
    run("agency", "team", *rest)


def serve(rest: Sequence[str]) -> None:
    """Serve the HTTP API.  task serve [--port 8000] [--reload] """
    run("agency", "serve", *rest)


def doctor(rest: Sequence[str]) -> None:
    """Print resolved configuration."""
    run("agency", "doctor", *rest)


def lint(rest: Sequence[str]) -> None:
    """Lint with ruff."""
    run("ruff", "check", ".", *rest)


def fmt(_rest: Sequence[str]) -> None:
    """Format and autofix."""
    run("ruff", "format", ".")
    run("ruff", "check", "--fix", ".")


def typecheck(rest: Sequence[str]) -> None:
    """Typecheck with mypy."""
    run("mypy", "src", "evals", *rest)


def test(rest: Sequence[str]) -> None:
    """Run unit tests."""
    run("python", "-m", "pytest", "-q", *rest)


def evals(rest: Sequence[str]) -> None:
    """Run the eval suite (hits the API, costs money)."""
    run("python", "-m", "evals.run_eval", *rest)


def check(_rest: Sequence[str]) -> None:
    """lint + typecheck + test. The pre-commit gate."""
    lint([])
    typecheck([])
    test([])


def docker_build(_rest: Sequence[str]) -> None:
    """Build the sandbox image."""
    subprocess.run(["docker", "build", "-t", "agency:latest", "."], cwd=ROOT, check=True)  # noqa: S603, S607


def docker_run(rest: Sequence[str]) -> None:
    """Run a prompt inside the sandbox container."""
    subprocess.run(  # noqa: S603
        ["docker", "compose", "run", "--rm", "agent", "run", *rest],  # noqa: S607
        cwd=ROOT,
        check=True,
    )


def clean(_rest: Sequence[str]) -> None:
    """Remove caches and build artefacts."""
    targets = [".mypy_cache", ".ruff_cache", ".pytest_cache", "dist", "build"]
    for name in targets:
        path = ROOT / name
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            print(f"removed {name}")
    for pycache in ROOT.rglob("__pycache__"):
        shutil.rmtree(pycache, ignore_errors=True)
    print("clean")


TASKS: dict[str, Callable[[Sequence[str]], None]] = {
    "install": install,
    "run": agent_run,
    "repl": repl,
    "team": team,
    "serve": serve,
    "doctor": doctor,
    "lint": lint,
    "fmt": fmt,
    "typecheck": typecheck,
    "test": test,
    "eval": evals,
    "check": check,
    "docker-build": docker_build,
    "docker-run": docker_run,
    "clean": clean,
}


def main() -> None:
    listing = "\n  ".join(
        f"{name:<14}{(fn.__doc__ or '').strip()}" for name, fn in TASKS.items()
    )
    parser = argparse.ArgumentParser(
        prog="task",
        description="Project tasks. Works identically on Windows, macOS and Linux.",
        epilog=f"tasks:\n  {listing}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("task", nargs="?", choices=list(TASKS), help="task to run")
    parser.add_argument("rest", nargs=argparse.REMAINDER, help="arguments for the task")
    args = parser.parse_args()

    if not args.task:
        parser.print_help()
        sys.exit(0)

    os.chdir(ROOT)
    TASKS[args.task](args.rest)


if __name__ == "__main__":
    main()
