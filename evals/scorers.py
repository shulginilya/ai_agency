"""Assertion types for eval cases.

Deliberately includes trace assertions, not just output assertions. Most agent
regressions show up as "it stopped calling the tool" long before the prose gets worse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from agency.hooks.delegation import delegations
from agency.obs.cost import RunRecord


@dataclass
class Score:
    name: str
    passed: bool
    detail: str = ""


class Scorer(Protocol):
    def __call__(self, record: RunRecord, spec: dict[str, Any]) -> Score: ...


def _output(record: RunRecord) -> str:
    return (record.output or "").lower()


def contains_all(record: RunRecord, spec: dict[str, Any]) -> Score:
    values = [str(v).lower() for v in spec["values"]]
    missing = [v for v in values if v not in _output(record)]
    return Score("contains_all", not missing, f"missing: {missing}" if missing else "")


def contains_any(record: RunRecord, spec: dict[str, Any]) -> Score:
    values = [str(v).lower() for v in spec["values"]]
    hit = [v for v in values if v in _output(record)]
    return Score("contains_any", bool(hit), f"matched: {hit}" if hit else f"none of {values}")


def excludes(record: RunRecord, spec: dict[str, Any]) -> Score:
    values = [str(v).lower() for v in spec["values"]]
    found = [v for v in values if v in _output(record)]
    return Score("excludes", not found, f"found forbidden: {found}" if found else "")


def matches_regex(record: RunRecord, spec: dict[str, Any]) -> Score:
    pattern = re.compile(spec["pattern"], re.I | re.S)
    ok = bool(pattern.search(record.output or ""))
    return Score("matches_regex", ok, "" if ok else f"no match for {spec['pattern']}")


def tool_called(record: RunRecord, spec: dict[str, Any]) -> Score:
    tool = spec["tool"]
    used = record.tools_used()
    return Score("tool_called", tool in used, f"used: {used}")


def tool_not_called(record: RunRecord, spec: dict[str, Any]) -> Score:
    tool = spec["tool"]
    used = record.tools_used()
    return Score("tool_not_called", tool not in used, f"used: {used}")


def delegated_to(record: RunRecord, spec: dict[str, Any]) -> Score:
    """Every worker named in `values` ran to completion.

    Reads the trace through `hooks.delegation.delegations`, the same function the CLI
    and `run_eval.py` use, so "delegated" means one thing everywhere rather than three
    slightly different walks of `tool_calls`.

    This is *completion*, not invocation. `delegations` reads only `phase == "post"`
    entries, so a worker whose delegation failed leaves its `pre` entry behind and
    never appears here -- which is the reading you want: a worker that crashed did not
    do the work, however much the manager intended it to.

    Names are compared exactly. They are identifiers from the `TEAM` dict, not prose,
    and the output scorers' case-folding would only hide a typo in a case file.
    """
    ran = delegations(record)
    missing = [str(v) for v in spec["values"] if str(v) not in ran]
    detail = f"missing: {missing}; ran: {ran}" if missing else f"ran: {ran}"
    return Score("delegated_to", not missing, detail)


def not_delegated_to(record: RunRecord, spec: dict[str, Any]) -> Score:
    """No worker named in `values` ran -- the other half of "reviewer ran, developer did not".

    Worth asserting on its own: over-delegation is a real regression (the manager sends
    a planning question to the Opus worker) and it is invisible to every assertion that
    only checks that *something* was delegated.
    """
    ran = delegations(record)
    found = [str(v) for v in spec["values"] if str(v) in ran]
    detail = f"unexpectedly ran: {found}" if found else f"ran: {ran}"
    return Score("not_delegated_to", not found, detail)


def no_blocked_calls(record: RunRecord, spec: dict[str, Any]) -> Score:  # noqa: ARG001
    blocked = [c.tool for c in record.tool_calls if c.phase == "blocked"]
    return Score("no_blocked_calls", not blocked, f"blocked: {blocked}" if blocked else "")


def max_cost_usd(record: RunRecord, spec: dict[str, Any]) -> Score:
    limit = float(spec["value"])
    ok = record.total_cost_usd <= limit
    return Score("max_cost_usd", ok, f"${record.total_cost_usd:.4f} vs limit ${limit:.4f}")


def max_turns(record: RunRecord, spec: dict[str, Any]) -> Score:
    limit = int(spec["value"])
    return Score("max_turns", record.num_turns <= limit, f"{record.num_turns} vs limit {limit}")


def succeeded(record: RunRecord, spec: dict[str, Any]) -> Score:  # noqa: ARG001
    return Score("succeeded", not record.is_error, "; ".join(record.errors))


SCORERS: dict[str, Scorer] = {
    "contains_all": contains_all,
    "contains_any": contains_any,
    "excludes": excludes,
    "matches_regex": matches_regex,
    "tool_called": tool_called,
    "tool_not_called": tool_not_called,
    "delegated_to": delegated_to,
    "not_delegated_to": not_delegated_to,
    "no_blocked_calls": no_blocked_calls,
    "max_cost_usd": max_cost_usd,
    "max_turns": max_turns,
    "succeeded": succeeded,
}


def score_record(record: RunRecord, assertions: list[dict[str, Any]]) -> list[Score]:
    results: list[Score] = []
    for spec in assertions:
        kind = spec.get("type")
        scorer = SCORERS.get(str(kind))
        if scorer is None:
            results.append(Score(str(kind), False, "unknown assertion type"))
            continue
        try:
            results.append(scorer(record, spec))
        except Exception as exc:  # noqa: BLE001 - a broken assertion is a failed assertion
            results.append(Score(str(kind), False, f"scorer raised: {exc}"))
    return results
