# capabilities/

The local plugin that carries the skills named by capability packs
(`src/agency/capabilities/seed.py`, or the `capability_revision` table).

## Why a plugin and not `.claude/skills/`

`options.build_options` passes `setting_sources=[]` deliberately -- the harness must
behave identically on every machine and in CI. Two consequences follow, and both are
silent:

1. The SDK applies its skills auto-default only when `setting_sources` is `None`. With
   `[]`, `ClaudeAgentOptions.skills=[...]` appends `Skill(name)` allow rules for skills
   the CLI was never told to discover. No error, no warning -- just a skill the model
   cannot see.
2. Even `setting_sources=["project"]` would not help: project-scoped discovery is rooted
   at the CLI's cwd, and `cwd` is the *workspace* directory, not the repo. A
   `.claude/skills/` at the repo root is invisible from there, and `add_dirs` does not
   change it (that widens file access, not settings discovery).

`plugins=[{"type": "local", "path": ...}]` becomes `--plugin-dir <absolute path>`, which
is independent of cwd and of every settings file. That is the only route that leaves the
isolation invariant intact.

**Status: unverified against a live CLI.** See the Phase 0 spike in
`docs/plan_dynamic_capabilities_db.md`.

## Do not put secrets in here

`ClaudeAgentOptions.skills` is a *context filter*, not a sandbox. An unlisted skill is
hidden from the model's listing and rejected by the Skill tool, but the files stay on
disk and are readable by any agent holding `Read` or `Bash`.

## Adding a skill

1. `skills/<name>/SKILL.md` with `name:` and `description:` frontmatter.
2. Reference it from a pack as `agency-caps:<name>` -- the plugin-qualified form.
3. Names may not contain parentheses, commas, or surrounding whitespace; the CLI's
   allow-rule tokeniser cannot carry them. `capabilities.resolve.validate_capability`
   enforces the same rules so a typo fails at resolve time, not at `connect()`.
