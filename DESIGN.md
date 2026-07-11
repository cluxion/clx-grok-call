# clx-grok-call Design

- Version: 0.1
- Date: 2026-07-11
- Purpose: expose the installed Grok CLI as one bounded, stateless call for Codex, Claude Code, and terminals.
- Scale: Small (~8 files), one feature module, no C0.

## Tech stack

| Area | Choice | Why |
|---|---|---|
| Runtime | Python 3.10+ standard library | Reliable subprocess, signal, timeout, JSON, and test behavior without runtime dependencies |
| Upstream | Installed `grok` executable | Reuse the working Grok 4.5 transport; no auth/proxy duplication |
| Packaging | `pyproject.toml` + wheel | One installable CLI with stable aliases |
| Tests | pytest | Existing local plugin convention and subprocess fixture support |
| Hosts | Codex and Claude plugin manifests/skills | Both hosts call the same PATH CLI; no host-specific runtime |

## Folder tree

```text
clx-grok-call/
├── DESIGN.md
├── README.md
├── pyproject.toml
├── src/clx_grok_call.py          # F1: full request lifecycle
├── tests/test_clx_grok_call.py   # contract and lifecycle tests
├── .codex-plugin/plugin.json
├── .claude-plugin/plugin.json
└── skills/clx-grok-call/SKILL.md # shared thin usage skill
```

## Main pipeline

```text
[argv/stdin] → F1 clx-grok-call → [stdout + process exit]
```

| Stage | IN | OUT |
|---|---|---|
| F1 | argv and optional stdin from the invoking process | one `CallResult`, rendered as text or one JSON object |

## F1 — request lifecycle

- parallel-safe: yes; every call owns only its child process and in-memory buffers.
- No worker pool: concurrency is controlled by callers, so one invocation cannot overwrite another invocation's state.

IN (main):

- `argv: list[str]` ← process arguments; command, prompt, model, timeout, output mode.

IN (aux):

- `stdin: TextIO` ← caller; used only when no positional prompt is supplied.
- `env: Mapping[str, str]` ← caller environment; passed to `grok` unchanged.
- `clock: monotonic` ← Python stdlib; wall-time measurement.

OUT:

- `CallResult`
  - `ok: bool`
  - `command: str`
  - `model: str | null`
  - `output: str`
  - `error: {code: str, message: str} | null`
  - `exit_code: int`
  - `timed_out: bool`
  - `duration_ms: int`

Failure semantics:

- usage error: exit 2, no child process.
- upstream executable missing: exit 127.
- timeout: terminate owned process group, bounded grace, kill survivors, exit 124.
- interrupt: terminate owned process group, exit 130.
- upstream failure: preserve its nonzero exit code and stderr.
- JSON mode: stdout contains exactly one JSON object for success and failure; diagnostic noise never joins stdout.

Internal logic:

1. Parse `call`, `models`, `status`, or `doctor` and validate timeout in `1..600` seconds.
2. Resolve exactly one prompt source; positional prompt plus non-empty piped stdin is a usage error.
3. Spawn exactly one `grok` process in a new process group.
4. Capture stdout/stderr in memory with a bounded wait and no automatic retry.
5. Render plain text or a single JSON envelope and return the truthful exit code.

Constraints:

- Default call uses model `grok-4.5`; `--model` overrides it.
- `grok-call` remains a compatibility CLI alias; `clx-grok-call` is canonical.
- No prompt/response/trace/temp/config/cache/session file is written.
- No Hermes, MCP, picker injection, daemon, database, nested agent, recursive call, or cross-asset import.
- `models` and upstream response formats are passed through rather than parsed.

## Dependency table

| Feature | Python stdlib | External executable | Custom asset |
|---|---|---|---|
| F1 | argparse, json, os, signal, subprocess, sys, time | `grok` | none |

## Implementation checklist

- [ ] Scaffold manifests, package metadata, skill, and runtime file.
- [ ] Contract test: argv/stdin ambiguity exits 2 without spawning.
- [ ] Contract test: one invocation spawns exactly one child with `grok-4.5`.
- [ ] Contract test: success/failure JSON is exactly one valid object and exit agrees with `ok`.
- [ ] Lifecycle test: timeout exits 124 within a bounded wall time and leaves no child.
- [ ] Lifecycle test: interrupt exits 130 and leaves no child.
- [ ] Isolation test: parallel calls share no files/state.
- [ ] Command tests: models, status, doctor, missing executable, Korean help.
- [ ] Build wheel, validate manifests/skill, install, and run live Grok 4.5 smoke.

## Design decision log

- [KEEP] One runtime module — request parsing, one subprocess, and rendering have one reason to change and no independent reuse sites.
- [KEEP] JSON/error/process helpers remain private functions — below the module floor.
- [LOCAL] Process execution stays feature-local — one use site, fails C0 rule of three.
- [REJECT] Shell wrapper — weaker timeout/signal/JSON contracts and harder deterministic tests.
- [REJECT] Multi-module adapter/config/runner hierarchy — one implementation and no split-gate evidence.
- [REJECT] Hermes proxy, MCP, picker, daemon, DB, retry loop, trace store, cache — forbidden dependency or unmeasured YAGNI.
- [COMPAT] Runtime import/distribution and legacy CLI aliases may remain stable while all discovery/public names are `clx-*`.

[self-check: 9/9 passed]
