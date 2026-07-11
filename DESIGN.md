# clx-grok-call Design

- Version: 0.1.2
- Date: 2026-07-11
- Purpose: expose the installed Grok CLI as one bounded call for Codex, Claude Code, and terminals.
- Scale: Small (~9 packaging/docs files + one runtime module), no C0 split.

## Tech stack

| Area | Choice | Why |
|---|---|---|
| Runtime | Python 3.10+ standard library | Subprocess, signal, timeout, JSON without runtime deps |
| Upstream | Installed `grok` executable | Reuse working Grok 4.5 transport; no auth/proxy duplication |
| Packaging | setuptools `py-modules` + wheel | Single top-level module `src/clx_grok_call.py` |
| Tests | pytest | Contract and lifecycle coverage |
| Hosts | Codex + Claude plugin manifests/skills | Same PATH CLI; no host-specific runtime |

## Folder tree

```text
clx-grok-call/
├── DESIGN.md
├── README.md
├── LICENSE
├── pyproject.toml
├── .gitignore
├── src/clx_grok_call.py
├── tests/test_clx_grok_call.py
├── .codex-plugin/plugin.json
├── .claude-plugin/plugin.json
└── skills/clx-grok-call/
    ├── SKILL.md
    └── agents/openai.yaml
```

## Main pipeline

```text
[argv/stdin] → F1 clx-grok-call → [stdout + process exit]
```

| Stage | IN | OUT |
|---|---|---|
| F1 | argv and optional stdin from the invoking process | one CallResult, plain text or one JSON object |

## F1 — request lifecycle

- parallel-safe: yes; each call owns only its child process group and in-memory buffers.
- No worker pool: callers control concurrency.

IN (main):

- `argv: list[str]` ← process arguments; command, prompt, model, timeout, output mode.

IN (aux):

- `stdin: TextIO` ← used **only** when there is no positional prompt token.
- `env: Mapping[str, str]` ← passed through to `grok` when provided.
- `clock: monotonic` ← duration measurement.

OUT — CallResult JSON fields:

- `ok: bool`
- `command: str` — `"call"` | `"models"` | `"doctor"`
- `model: str | null`
- `output: str`
- `error: {code: str, message: str} | null`
- `exit_code: int`
- `timed_out: bool`
- `duration_ms: int`

Commands (no `status`):

| Command | Behavior |
|---|---|
| `call` (default) | One `grok -p … -m …` with isolation flags |
| `models` | Pass-through `grok models` |
| `doctor` | Bounded probe `grok --version --no-auto-update` (no prompt) |

Call argv contract:

```text
grok -p <prompt> -m <model> --output-format plain --tools "" \
  --no-memory --no-subagents --no-auto-update
```

- tools disabled (`--tools ""`)
- memory off, subagents off, auto-update off (`--no-auto-update`)
- no automatic retries

Exit codes:

| Code | Meaning |
|---|---|
| 0 | success |
| 2 | usage (empty prompt, bad args, argparse error) |
| 124 | timeout after TERM → grace → KILL of process group |
| 126 | spawn OSError (e.g. not executable) |
| 127 | `grok` missing from PATH |
| 130 | KeyboardInterrupt / SIGINT path |
| 141 | broken pipe |
| 143 | wrapper received SIGTERM (kills owned group) |
| other | upstream nonzero preserved |

Failure / JSON semantics:

- usage: exit 2, no child.
- missing executable: exit 127.
- timeout: terminate owned process group, bounded grace, kill survivors, exit 124.
- interrupt / SIGTERM: clean owned group, exit 130 / 143.
- upstream failure: preserve nonzero exit and stderr (plain mode).
- `--json`: stdout is exactly one JSON object for success and failure; exit matches `exit_code`.

Internal logic:

1. Parse argv; subcommands are only `models` and `doctor` (not `status`).
2. Validate timeout in `1..600` seconds (default 120).
3. Resolve prompt: positional tokens join as prompt and **never** read stdin; stdin-only when no positional tokens and not a TTY; empty → usage 2.
4. Spawn exactly one `grok` in a new process group (`start_new_session=True`).
5. Bounded `communicate`; on timeout SIGTERM group → grace → SIGKILL; no retry.
6. Render plain text or a single JSON envelope; return truthful exit code.

## Wrapper state guarantee (not full-system “stateless”)

**Wrapper-only:**

- Does not write prompt/response/trace/temp/config/cache/session files of its own.
- No Hermes, MCP, picker injection, daemon, database, nested agent, recursive call, or cross-asset import.
- Isolation flags force tools-off / memory-off / subagents-off / no-auto-update on the child argv.

**Not claimed:**

- Upstream Grok auth, account login, provider session, or host Grok client state is outside this package. Do not market the product as fully stateless end-to-end.

Constraints:

- Default model `grok-4.5`; `--model` overrides.
- Canonical CLI `clx-grok-call`; legacy alias `grok-call`.
- `models` and upstream response text are passed through, not schema-parsed.

## Dependency table

| Feature | Python stdlib | External executable | Custom asset |
|---|---|---|---|
| F1 | argparse, json, os, signal, subprocess, sys, time | `grok` | none |

## Implementation checklist

- [x] Scaffold manifests, package metadata, skill, and runtime file.
- [x] Contract test: empty prompt → exit 2 without spawn; positional prompt never reads stdin.
- [x] Contract test: one invocation spawns exactly one child with `grok-4.5`.
- [x] Contract test: success/failure JSON is one valid object; exit agrees with `ok`.
- [x] Lifecycle test: timeout exits 124 within bounded wall time; no orphan children.
- [x] Lifecycle test: interrupt / SIGTERM paths clean process group.
- [x] Isolation flags: `--tools ""`, `--no-memory`, `--no-subagents`, `--no-auto-update`.
- [x] Command tests: models, doctor (no status), missing executable, Korean help.
- [x] Package metadata: setuptools py-modules, dual console scripts, plugin manifests, skill.

## Design decision log

- [KEEP] One runtime module — parse, one subprocess, render share one change reason.
- [KEEP] JSON/error/process helpers private — below module floor.
- [LOCAL] Process execution feature-local — one use site.
- [REJECT] Shell wrapper — weaker timeout/signal/JSON contracts.
- [REJECT] Multi-module adapter/config/runner hierarchy — no split-gate evidence.
- [REJECT] Hermes proxy, MCP, picker, daemon, DB, retry loop, trace store, cache — forbidden or YAGNI.
- [REJECT] `status` subcommand — not implemented; surface is `call` / `models` / `doctor` only.
- [KEEP] Wrapper-only no-write guarantee; refuse false full-stateless marketing.
- [COMPAT] Legacy CLI alias `grok-call` may remain while public name is `clx-grok-call`.

[self-check: 9/9 passed]
