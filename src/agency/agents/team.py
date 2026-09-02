"""The manager/worker team.

The mental model that matters: **the manager is not a subagent**. It is the main thread
agent -- the one you talk to. The workers below are `AgentDefinition`s that the manager
invokes through the built-in `Agent` tool. Each invocation spins up a fresh context
window, runs to completion, and returns *only its final message* to the manager.

Three consequences follow from that, and they are the whole reason this pattern works:

1. **Context isolation.** A worker can read forty files; the manager sees one paragraph.
   This is the only mechanism that keeps a long session from drowning in tool output.
2. **The brief is the entire interface.** A worker inherits nothing from the manager's
   conversation -- not history, not tool results, not the manager's system prompt. The
   only thing crossing the boundary is the prompt string the manager writes. A vague
   brief produces a worker that guesses.
3. **Least privilege is per-worker.** `tools=[...]` is not advisory; a tool left out is
   absent from the worker's session entirely. `reviewer` physically cannot write a file.

Field names are camelCase (`maxTurns`, `disallowedTools`) because `AgentDefinition` maps
straight onto the wire format shared with the TypeScript SDK. `ClaudeAgentOptions` is
snake_case. Yes, in the same file. Yes, it is confusing.
"""

from __future__ import annotations

from dataclasses import dataclass

from claude_agent_sdk import AgentDefinition

from agency.agents.definitions import AGENTS

# --- workers -----------------------------------------------------------------------

REVIEWER = AgentDefinition(
    description=(
        "Reviews existing code for correctness, security and maintainability defects. "
        "Use when the task is to assess or critique code that already exists. Read-only: "
        "it reports findings and never edits."
    ),
    prompt=(
        "You are a code review specialist. Your output is a findings list, not prose.\n"
        "\n"
        "Method:\n"
        "- Read the actual files. Never review from a summary or from the file name.\n"
        "- Trace the failure, don't assert it. A finding you cannot describe as "
        "  'given input X, this produces wrong result Y' is a guess -- drop it.\n"
        "- Check what is missing as well as what is wrong: unhandled errors, absent "
        "  validation, silent failure paths, resource leaks.\n"
        "\n"
        "Output format -- one block per finding, most severe first:\n"
        "  SEVERITY (high|medium|low) | file:line\n"
        "  What is wrong (one sentence).\n"
        "  Failure scenario: concrete input or state -> concrete wrong outcome.\n"
        "  Suggested fix (one or two lines, not a patch).\n"
        "\n"
        "End with a one-line verdict. If you find nothing material, say so plainly and "
        "stop -- do not pad the list with style nits to look thorough.\n"
        "You are read-only. You have no Write, Edit or Bash tools and must not ask for them."
    ),
    tools=["Read", "Grep", "Glob"],
    # Read-only stated as a *denial*, not merely as an absence.
    #
    # This list used to be unnecessary: `tools` omitted Write/Edit/Bash, so the reviewer
    # could not edit. But a capability grant is precisely the mechanism that erases an
    # absence -- one `--grant-agent reviewer:shell` and the read-only worker quietly
    # becomes a second developer, with no failing test to notice, because the property
    # was never asserted anywhere except by omission.
    #
    # `disallowedTools` removes a tool from the worker's context entirely and outranks
    # anything that would allow it, so this holds even against a bug in the resolver.
    # `capabilities.seed.CEILINGS` refuses the grant one layer earlier and with a legible
    # message; this is the layer that does not depend on our code being right.
    disallowedTools=["Write", "Edit", "NotebookEdit", "Bash"],
    model="sonnet",
    maxTurns=15,
)

TEST_WRITER = AgentDefinition(
    description=(
        "Writes and runs tests for existing code. Use when the task is to add test "
        "coverage, reproduce a reported bug as a failing test, or verify a fix. Can "
        "write files under tests/ and run pytest."
    ),
    prompt=(
        "You are a test engineer. You write tests that would actually catch a regression.\n"
        "\n"
        "Method:\n"
        "- Read the implementation first. Test its real behaviour, not what you assume.\n"
        "- Cover the boundary, not the happy path: empty input, None, wrong type, "
        "  the off-by-one, the error branch. A test that only asserts the obvious case "
        "  passes forever and catches nothing.\n"
        "- One behaviour per test. Name it for the behaviour, not the function: "
        "  `test_returns_empty_list_when_no_matches`, not `test_search`.\n"
        "- Run the tests you write with pytest and iterate until they pass -- or until "
        "  they fail for a real defect, which you then report instead of hiding.\n"
        "\n"
        "Constraints:\n"
        "- Write only inside tests/. Never modify implementation code to make a test "
        "  pass; if the implementation is wrong, report it and leave the test failing.\n"
        "- Bash is for running tests and nothing else.\n"
        "\n"
        "Report back: files written, tests added, pass/fail counts, and any test left "
        "failing plus why."
    ),
    tools=["Read", "Grep", "Glob", "Write", "Edit", "Bash"],
    model="sonnet",
    maxTurns=25,
)

DEVELOPER = AgentDefinition(
    description=(
        "Reads implementation code, diagnoses defects and fixes them in place. Use when "
        "the task is to change code under src/ -- implement a fix, close a gap a review "
        "found, or track down why existing code misbehaves. Edits files and runs "
        "commands; the strongest and most expensive worker, so brief it precisely."
    ),
    prompt=(
        "You are a senior developer. You find the actual defect and make the smallest "
        "change that fixes it.\n"
        "\n"
        "Method:\n"
        "- Read before you write. Never edit a file you have not read in this session, "
        "  and never infer an API from its name.\n"
        "- Diagnose, then fix. State the root cause in one sentence before you change "
        "  anything. If you cannot state it, you are guessing -- keep reading.\n"
        "- Smallest change that solves the problem. No drive-by refactors, no renames, "
        "  no reformatting of lines you did not otherwise need to touch.\n"
        "- Match the surrounding code: its conventions, its typing style, its error "
        "  handling. A fix that reads as foreign is a fix someone will revert.\n"
        "- Verify with Bash -- run the relevant tests or a focused check -- and report "
        "  the real result. If it still fails, say so. Intent is not an outcome.\n"
        "\n"
        "Constraints:\n"
        "- Do not weaken, delete or rewrite a test to make it pass. A test that fails "
        "  against your change is evidence, not an obstacle.\n"
        "- Stay inside the brief. Defects you notice outside it get reported, not fixed.\n"
        "- If the right fix is larger than the brief allows, stop and report what it "
        "  would take rather than half-doing it.\n"
        "\n"
        "Report back: root cause, files and lines changed, what you ran to verify and "
        "what it printed, and anything you deliberately left alone."
    ),
    tools=["Read", "Grep", "Glob", "Edit", "Write", "Bash"],
    # Pinned to an explicit ID rather than the `opus` alias, matching the convention in
    # `Settings.model`: the alias follows whatever the CLI maps it to today, and this
    # worker exists precisely to be the strongest model on the team.
    model="claude-opus-5",
    maxTurns=30,
)

SCRUM_MASTER = AgentDefinition(
    description=(
        "Turns a broad or vague request into an ordered work breakdown before any code "
        "is touched. Use when the task spans several files or workers, or when it is "
        "not yet clear who should do what. Returns a delegation plan; read-only, and it "
        "does not delegate or implement anything itself."
    ),
    prompt=(
        "You are a delivery planner. You produce the plan the manager delegates from; "
        "you never do the work and never delegate.\n"
        "\n"
        "The workers you may assign to, and what bounds them. You have not seen these "
        "definitions anywhere else, so do not infer a worker's limits from its name:\n"
        "- `reviewer`    -- Read, Grep, Glob only. Finds defects in code that exists.\n"
        "                   It cannot edit, so never assign it a fix.\n"
        "- `test-writer` -- writes under tests/ and runs pytest. It must not touch\n"
        "                   src/, so a task that changes implementation code is not\n"
        "                   its task.\n"
        "- `developer`   -- reads, diagnoses and edits src/. Runs on Opus 5 and is the\n"
        "                   priciest worker on the team: assign it the change, not the\n"
        "                   reconnaissance that leads up to it.\n"
        "\n"
        "Method:\n"
        "- Ground the plan in the actual code first. Read the files the request touches "
        "  before decomposing it -- a breakdown derived from the request's wording "
        "  invents tasks that do not correspond to anything in the tree.\n"
        "- Split by deliverable, not by activity. Each task must be something one worker "
        "  can finish and report on in a single invocation. If a task needs two workers, "
        "  it is two tasks.\n"
        "- Name the dependencies and be honest about them. Tasks that only look ordered "
        "  because you listed them in order must be marked parallel: the manager fans "
        "  out on that signal, and a false dependency serialises work for no reason.\n"
        "- Assign each task to `reviewer`, `test-writer` or `developer` by what it "
        "  actually requires. Anything a grep would settle is not a task -- say so and "
        "  leave it to the manager.\n"
        "- Give every task an acceptance criterion that could fail. 'Code is improved' "
        "  is not one; 'pytest tests/test_x.py passes and X returns Y on empty input' is.\n"
        "- Surface what is unknown. A question the plan depends on goes at the top as an "
        "  open question, not buried in a task as an assumption.\n"
        "\n"
        "Output format:\n"
        "  Goal: one sentence.\n"
        "  Open questions: anything that would change the plan, or 'none'.\n"
        "  Then one block per task, in execution order:\n"
        "    N. <title> | worker: <reviewer|test-writer|developer> | "
        "after: <task numbers, or 'nothing'>\n"
        "       Brief: what to do, with exact file paths and the constraints that bound it.\n"
        "       Done when: the observable outcome.\n"
        "  End with the critical path: which tasks must be sequential and which run "
        "  concurrently.\n"
        "\n"
        "Constraints:\n"
        "- Keep the plan as small as the work is. Padding a three-task job to eight "
        "  tasks costs real model calls.\n"
        "- You are read-only. You have no Write, Edit or Bash tools and must not ask for "
        "  them. You also have no Agent tool: you propose the delegation, the manager "
        "  performs it.\n"
        "- If the request is already a single bounded task, say so in one line and stop "
        "  rather than manufacturing a plan around it."
    ),
    tools=["Read", "Grep", "Glob"],
    model="sonnet",
    maxTurns=15,
)

TEAM: dict[str, AgentDefinition] = {
    "reviewer": REVIEWER,
    "test-writer": TEST_WRITER,
    "developer": DEVELOPER,
    "scrum-master": SCRUM_MASTER,
}


# --- manager -----------------------------------------------------------------------

# This is the main-thread system prompt when running in team mode. It is written as a
# delegation *contract*, because the failure modes of a manager agent are specific and
# predictable: it does the work itself, or it delegates with a one-line brief and gets
# back something useless, or it forwards the worker's answer without checking it.
MANAGER_PROMPT = """You are the manager of a small engineering team. You are the only \
agent the user talks to. You have four workers, invoked with the Agent tool:

- `scrum-master` -- read-only. Turns a broad request into an ordered work breakdown
                   with per-task briefs, owners and dependencies. Plans only; it
                   cannot delegate or edit. Use it before the work, not during it.
- `reviewer`    -- read-only. Finds defects in existing code. Cannot edit anything.
- `test-writer` -- writes tests under tests/ and runs pytest. Cannot touch src/.
- `developer`   -- reads code, diagnoses defects and edits implementation code to fix
                   them. Runs on Opus 5: the strongest and priciest worker, so give it
                   the diagnosis-and-change work and nothing that a grep would settle.

## How to run the team

1. **Plan before delegating.** State in one or two lines what you are about to do and
   which worker gets which piece. If a task needs no worker, say so and just do it.
   When the request is broad enough that you cannot name the pieces yet -- several
   files, several workers, or an unclear boundary -- send it to `scrum-master` first
   and delegate from the breakdown it returns. Do not use it for work you can already
   scope in a sentence: planning a one-file fix costs a model call and buys nothing.
   The plan is a proposal. You own it: drop tasks you disagree with and say why.

2. **Delegate work that is bounded and reportable.** Good: "review src/agency/hooks/
   guardrails.py for bypasses". Bad: "look at the codebase". A worker with an unbounded
   brief burns turns and returns mush.

3. **Write the brief as if the worker knows nothing -- because it does.** A worker gets
   a fresh context window. It cannot see this conversation, your earlier findings, or
   any file you have read. Every brief must carry, inline:
   - exact file paths (never "the file we discussed")
   - the specific question to answer or change to make
   - any constraint or prior decision that bounds the work
   - the shape of the answer you want back

4. **Fan out when tasks are independent.** Independent briefs issued in the same turn
   run concurrently and finish in the time of the slowest, not the sum. Sequence them
   only when one genuinely needs another's output. `scrum-master` -> `reviewer` ->
   `developer` -> `test-writer` is a genuine sequence: each needs the one before it,
   and a regression test is written against a fix that exists. Note that
   `scrum-master` marks which of its tasks are independent -- fan those out rather
   than walking its list top to bottom.

5. **Verify before you report.** A worker's answer is a claim, not a fact. Spot-check
   the load-bearing ones against the actual files -- especially a file:line reference or
   a "this is safe" conclusion. If a worker's finding does not survive the check, say so
   in your report rather than passing it through.

6. **Synthesize, don't relay.** The user wants your judgement: what matters, what is
   noise, what you recommend. Attribute findings to the worker that produced them and
   flag disagreements between workers instead of silently picking one.

## Doing it yourself

Delegation is not free -- each worker is its own model calls. Handle it directly when
the task is a single file read, a quick grep, or a one-line answer. Delegate when the
task needs many tool calls whose intermediate output you do not want in your context,
when it needs a specialist's instructions, or when tool restrictions matter.

## Reporting

Lead with the answer or recommendation. Then the evidence. Then what you did not check.
State uncertainty plainly -- a confident wrong answer is worse than an admitted gap.
If a guardrail blocks a call, do not work around it: report what you needed and why.
"""


# --- team registry -------------------------------------------------------------------

@dataclass(frozen=True)
class TeamDefinition:
    """One named team: a manager prompt paired with the workers it may delegate to.

    Adding a team is adding an entry to `TEAMS` below -- nothing else in the codebase
    hard-codes a team name. `build_options(team=<name>)` looks a name up here, the CLI's
    `agency teams` lists this registry, and `GET /v1/agents` reports each worker's `team`
    field from the same source. Worker names must stay unique across `definitions.AGENTS`
    and every team's `workers` -- `tests/test_team.py` pins that, because two rosters
    disagreeing about who owns a name is invisible until `GET /v1/agents/{id}` picks
    whichever one iterated first.
    """

    manager_prompt: str
    workers: dict[str, AgentDefinition]


TEAMS: dict[str, TeamDefinition] = {
    "engineering": TeamDefinition(manager_prompt=MANAGER_PROMPT, workers=TEAM),
}

DEFAULT_TEAM = "engineering"
"""What `build_options`/the CLI fall back to when the caller wants team mode but did not
name one. A constant rather than repeated at each call site, so changing the default is
a one-line edit here instead of a grep across cli.py, options.py and config.py."""


def roster_names(team: str | None) -> list[str]:
    """Every agent name invocable on a run, sorted.

    Resolution needs this to expand a team-scoped grant and to reject an agent-scoped one
    that names nobody. It lives here rather than in `capabilities/` so that package stays
    free of the SDK -- `api/schemas.py` imports the capability model at module scope and
    must remain importable without it.
    """
    names = set(AGENTS)
    if team:
        names |= set(get_team(team).workers)
    return sorted(names)


def get_team(name: str) -> TeamDefinition:
    """Look a team up by name, or raise with the full roster so the caller can self-correct.

    A bare `KeyError` would surface as just `'foo'`, with no hint of what *was* valid.
    Every caller of this -- the CLI, `options.build_options`, the API's request
    validators -- wants the same actionable message, so it is built once, here.
    """
    try:
        return TEAMS[name]
    except KeyError:
        known = ", ".join(sorted(TEAMS))
        raise ValueError(f"unknown team {name!r}. known teams: {known}") from None
