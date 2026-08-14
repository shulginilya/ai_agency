"""Batch eval runner.

    uv run python -m evals.run_eval                 # all cases
    uv run python -m evals.run_eval --case summarise_readme
    uv run python -m evals.run_eval --repeat 3      # variance check

Writes a JSON report to evals/reports/ so cost and pass-rate can be diffed across
prompt or model changes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from agency.config import get_settings
from agency.hooks.delegation import delegations
from agency.obs.logging import configure_logging, get_logger
from agency.runner import run_once
from evals.scorers import Score, score_record

log = get_logger(__name__)

CASES_DIR = Path(__file__).parent / "cases"
REPORTS_DIR = Path(__file__).parent / "reports"


def load_cases(name: str | None = None) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for path in sorted(CASES_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not data:
            continue
        data.setdefault("name", path.stem)
        if name is None or data["name"] == name:
            cases.append(data)
    return cases


async def run_case(case: dict[str, Any], attempt: int) -> dict[str, Any]:
    record = await run_once(
        case["prompt"],
        profile=case.get("profile", "eval"),
        project_root=Path.cwd(),
        team=bool(case.get("team", False)),
    )
    scores: list[Score] = score_record(record, case.get("assertions", []))
    passed = all(s.passed for s in scores)
    delegated = delegations(record)
    log.info(
        "eval.case",
        case=case["name"],
        attempt=attempt,
        passed=passed,
        delegated_to=delegated,
        cost_usd=record.total_cost_usd,
    )
    return {
        "case": case["name"],
        "attempt": attempt,
        "passed": passed,
        "run_id": record.run_id,
        "cost_usd": record.total_cost_usd,
        "num_turns": record.num_turns,
        "duration_ms": record.duration_ms,
        "tools_used": record.tools_used(),
        # Which workers ran is the load-bearing signal for team cases: a manager that
        # stops delegating still produces plausible prose.
        "delegated_to": delegated,
        "output": record.output,
        "scores": [asdict(s) for s in scores],
    }


async def main_async(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    cases = load_cases(args.case)
    if not cases:
        log.error("eval.no_cases", requested=args.case, dir=str(CASES_DIR))
        return 2

    results: list[dict[str, Any]] = []
    for case in cases:
        for attempt in range(1, args.repeat + 1):
            results.append(await run_case(case, attempt))

    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    cost = sum(r["cost_usd"] for r in results)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": settings.model,
        "total": total,
        "passed": passed,
        "pass_rate": round(passed / total, 3) if total else 0.0,
        "total_cost_usd": round(cost, 4),
        "results": results,
    }

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = REPORTS_DIR / f"eval-{stamp}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"\n{passed}/{total} passed  |  ${cost:.4f}  |  {out}")
    for r in results:
        if not r["passed"]:
            failed = [s for s in r["scores"] if not s["passed"]]
            print(f"  FAIL {r['case']} (attempt {r['attempt']})")
            for s in failed:
                print(f"       {s['name']}: {s['detail']}")

    return 0 if passed == total else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the agent eval suite.")
    parser.add_argument("--case", help="Run only this case name.")
    parser.add_argument(
        "--repeat", type=int, default=1, help="Runs per case; >1 surfaces variance."
    )
    raise SystemExit(asyncio.run(main_async(parser.parse_args())))


if __name__ == "__main__":
    main()
