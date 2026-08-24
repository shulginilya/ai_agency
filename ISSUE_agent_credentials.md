## Problem

Every run through `POST /v1/runs` and `POST /v1/sessions/{id}/messages` returned **200** with `is_error: true` and the body:

```
Failed to authenticate: OAuth session expired and could not be refreshed
```

— while a valid `sk-ant-…` key sat in `.env`. Nothing was misconfigured in any way the operator could see. The chain:

1. `Settings` has `env_prefix="AGENCY_"` and `extra="ignore"`, so the unprefixed `ANTHROPIC_API_KEY` line was read out of `.env` and then **dropped**.
2. Nothing else loads `.env` — there is no `load_dotenv` in the repo, and `uv run` does not read it without `--env-file`.
3. `build_options` passed `env=dict(SUBAGENT_LIMITS)`. The Python SDK *merges* that into the inherited environment, which had no key.
4. The CLI subprocess, finding no credential, **does not fail** — it falls back to the OAuth login cached in `~/.claude`. That login had expired.

Docker masked it entirely: `compose.yaml` injects `ANTHROPIC_API_KEY` explicitly, so this only ever reproduced on native `python tasks.py serve` / `run` — the development path.

The second half of the problem is diagnosis. Even once you know, the API gave a client no way to tell: the SDK reports "no usable credentials" in exactly the shape it reports "I could not do that" — a `ResultMessage` with `is_error` and prose — so a server-side configuration failure arrived in the frontend as a chat bubble in which the agent appears to be talking about the server's own credentials.

## Proposal

Four credential settings, declared with explicit `validation_alias` because they belong to the CLI subprocess rather than to us, and forwarded to it by `options._subprocess_env`:

| Variable | Billed to | Source |
|---|---|---|
| `ANTHROPIC_AUTH_TOKEN` | whatever the gateway bills | an LLM gateway; pair with `ANTHROPIC_BASE_URL` |
| `ANTHROPIC_API_KEY` | prepaid Console credits | platform.claude.com → Billing |
| `CLAUDE_CODE_OAUTH_TOKEN` | a Claude subscription | `claude setup-token` (one-year token) |
| *(none set)* | a Claude subscription | the `claude login` session in `~/.claude` |

Plus: which source is active is now visible (`api.startup` log, `agency serve` banner, `agency doctor`), a shadowed credential is warned about rather than silently ignored, and a credential failure is a **502** instead of a 200.

## Design decisions worth reviewing

One fact drives most of these: **the CLI subprocess authenticates from its own environment, and when it finds nothing it falls back rather than failing.** Every silent-failure decision below is a consequence.

- **Explicit `validation_alias` per variable, not `load_dotenv()`.** Calling `load_dotenv` would fix the symptom while breaking the rule in `CLAUDE.md` that all config flows through `Settings`. This keeps one source of truth and makes the credential set enumerable, which is what `doctor` and the precedence logic are built on.
- **All configured credentials are forwarded; `config.AUTH_PRECEDENCE` mirrors the CLI's own order rather than picking a winner here.** *This is the call I am least sure about.* The argument for it: if `agency` chose a different credential than a bare `claude` on the same machine, the harness would stop being a faithful reproduction of the CLI, which is the entire premise of the options chokepoint. The argument against: we now depend on an ordering defined elsewhere, which can drift silently if the CLI changes it, and we have no test that would notice.
- **Two credentials set is a warning, not an error.** It is legal and has defined behaviour, so refusing to start would be us inventing policy. But it is silent and nearly always a mistake — the live case is a subscription token set next to a stale API key, where every run keeps billing the account the operator was trying to leave, and the failure surfaces as a credit error naming that account, which reads as "the switch didn't work". Reasonable to argue this should be a hard failure instead.
- **Blank is treated as absent, not as an empty credential.** An exported `ANTHROPIC_API_KEY=""` is a credential the CLI tries and fails with; an absent one is a credential it falls past to the login session. The distinction is the difference between the two supported modes.
- **`SecretStr` for all three secrets.** `agency doctor` prints `str(settings)`; a plain `str` field would put a live credential on the console. Note the server's *own* `api_key` is still a plain `str` and is still printed — see follow-ups.
- **A credential failure is a 502, not a 200.** This is a deliberate exception to the documented "`is_error=True` is a 200" contract, and the only contract change here. The rule that actually governs is not "is this auth" but "did the harness fail in a way the caller cannot fix": nothing the caller sent was wrong and no retry helps. `AgentAuthError` subclasses `UpstreamError` so existing 502 branches keep working, with its own `type` URI so a client can distinguish it.
- **An auth failure does *not* close the session, and does not count as a turn.** Unlike a timeout or transport error — where the SDK client is left mid-stream and must be discarded — the turn here never reached the model, so the context is intact and the session works again as soon as the server has credentials. Discarding it would cost the user their conversation over a problem that is entirely ours. Implemented by raising inside the `sessions.turn(...)` block, which passes `APIError` through untouched.
- **Detection is substring matching on the result text, gated on `is_error`.** Fragile by nature, and worth challenging. It is the only signal available — the SDK does not type these — and the `is_error` gate is what stops an agent that merely *writes about* an invalid API key from being misclassified. If the CLI rewords its messages, this silently stops firing and we are back to 200s.
- **Billing failures are deliberately *excluded* from that marker list, and I now think that was wrong.** `Credit balance is too low` currently still returns a 200. It was left out as "billing, not auth", but by the rule stated two bullets up it belongs with the 502s. See follow-ups.

## Config / migration

New optional variables in `.env` (all default to unset): `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`. `.env.example` documents the precedence.

**Behaviour change worth naming:** a key sitting in `.env` now actually takes effect. Anyone who had a stale `ANTHROPIC_API_KEY` in `.env` while relying on their `claude login` session has been silently running on the login — after this change they flip to the key. `agency doctor` reports which one is live.

**Contract change**, `POST /v1/runs` and `POST /v1/sessions/{id}/messages`:

| | Before | After |
|---|---|---|
| Credential failure | `200`, `is_error: true`, message in `output` | `502`, problem+json, `type: …/problems/agent-auth-failed` |
| Session after such a failure | n/a | still open, `turns` not incremented |
| Everything else | unchanged | unchanged |

## Acceptance criteria

- [x] A key in `.env` reaches the CLI subprocess and is used
- [x] With no credential configured, nothing is exported and the CLI falls back to the `claude login` session
- [x] A whitespace-only credential is treated as absent, not as an empty credential
- [x] The active credential source is reported by `agency doctor`, the `serve` banner, and the `api.startup` log
- [x] Setting two credentials logs `options.auth_shadowed` and prints a `doctor` warning naming the winner and the ignored ones
- [x] No credential value appears in `doctor` output or in any log line
- [x] A credential failure returns `502` with `type: …/problems/agent-auth-failed` from both endpoints
- [x] A session survives a credential failure and does not count the turn
- [x] An agent that succeeds while writing about an invalid API key is still a `200`
- [ ] **A run actually reaches a live model.** Not verified — see below.

## Out of scope / follow-ups

- [ ] **Nothing here has been exercised against a live model.** Every test uses a fake runner/conversation. The credential path is verified only to the point of "the right variable, with the right value, is in the options object". This is the most useful thing a reviewer could check.
- [ ] `Credit balance is too low` should get sibling treatment — a `BILLING_FAILURE_MARKERS` list and an `AgentBillingError` (`…/problems/agent-billing`), so a client can tell "top up credits" from "fix credentials". Currently still a 200.
- [ ] `max_budget_usd` is likely inert under subscription auth — there is no per-request cost to report, so `total_cost_usd` is probably always `0` and the cap silently never trips. Unverified; needs one live run to confirm.
- [ ] No test pins `AUTH_PRECEDENCE` against the CLI's actual behaviour, so upstream reordering would go unnoticed.
- [ ] `/readyz` does not check credentials. Deliberate — a `cli-login` setup is legitimate and unverifiable without spending a request, so gating readiness on it would report every dev machine as degraded. Revisit if credentials become verifiable cheaply.
- [ ] `Settings.api_key` (this API's own auth, unrelated to the above) is still a plain `str` and is printed in full by `agency doctor`.
- [ ] `eval_model` remains defined but unused — pre-existing, unrelated, noted because it is adjacent.

## Notes for the reviewer

**Touched:** `config.py` (credential fields + precedence helpers), `options.py` (`_subprocess_env`), `obs/cost.py` (`AUTH_FAILURE_MARKERS`, `RunRecord.auth_failure`), `api/errors.py` (`AgentAuthError`, `raise_for_auth_failure`), `api/routers/{runs,sessions}.py`, `api/app.py`, `cli.py`, plus `.env.example` / `README.md` / `CLAUDE.md` and tests. 14 files, ~550 insertions.

**How it was tested:** the repo `.venv` is a Windows venv and could not be executed from the Linux environment this work was done in, so verification ran in a rebuilt env with `starlette==1.4.1` / `fastapi==0.141.1` pinned to match `uv.lock`. `pytest -q --ignore=tests/test_scorers.py` → **170 passed** (`test_scorers.py` excluded: it imports `evals`, which was not staged). `ruff check src/agency tests/` clean. `mypy src` — the `tasks.py typecheck` gate — clean.

**On that number:** 170 is observed. The pre-change baseline of 161 comes from the project's own notes, not from a run in this session — the suite was never run before the change, so treat the delta as approximate. Separately, `mypy --strict` on `tests/test_options.py` reports two `unused-ignore` warnings on the profile-loop tests; both are pre-existing and outside the gate.

**Odd thing about the approach:** `api/errors.py` imports `RunRecord` from `obs.cost`. That is a deliberate downward dependency — `obs.cost` is SDK-free and HTTP-free — chosen so both routers map credential failures through one function rather than each growing its own copy.
