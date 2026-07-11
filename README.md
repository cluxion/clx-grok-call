# clx-grok-call

One bounded call to the installed [Grok](https://x.ai) CLI (`grok`). Runtime is **Python 3.10+ stdlib only**. The wrapper spawns exactly one child, applies a hard timeout, never retries, and returns a truthful process exit code.

**State caveat:** this wrapper does **not** write its own prompt/response/trace/cache/session files. It is **not** a claim that upstream Grok is fully stateless — Grok account auth, provider session, or other host-side Grok state may still apply outside this package.

## Install

Requires `grok` on `PATH` and a working Grok login/session for live calls.

```bash
pip install -e ".[dev]"
# or
pip install .
```

Console scripts after install:

- `clx-grok-call` — canonical
- `grok-call` — legacy alias

## Use

```bash
# Positional prompt (default model: grok-4.5)
clx-grok-call "Summarize this approach in three bullets"

# Stdin prompt (only when no positional tokens)
echo "Review this stack trace" | clx-grok-call

# Model override
clx-grok-call -m grok-4.5 "Second opinion on this design"

# JSON envelope on stdout (success and failure)
clx-grok-call --json "What are the risks?"

# Timeout (seconds, 1..600; default 120)
clx-grok-call -t 60 "Quick check"

# List models (upstream passthrough)
clx-grok-call models

# Probe installed grok (version; no prompt)
clx-grok-call doctor
```

### Call flags sent to `grok`

Every `call` invocation runs approximately:

```text
grok -p <prompt> -m <model> --output-format plain --tools "" \
  --no-memory --no-subagents --no-auto-update
```

- **tools disabled** (`--tools ""`)
- **no memory** / **no subagents** / **no auto-update**
- **no automatic retries**

`doctor` probes with `grok --version --no-auto-update` (bounded timeout, no prompt).

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 2 | Usage / empty prompt / bad args |
| 124 | Timeout (process group terminated) |
| 126 | Spawn failure (e.g. not executable) |
| 127 | `grok` not found on PATH |
| 130 | Interrupt (SIGINT / KeyboardInterrupt) |
| 141 | Broken pipe |
| 143 | Wrapper received SIGTERM |
| other | Upstream `grok` exit code preserved |

## JSON contract

With `--json`, stdout is **exactly one** JSON object:

```json
{
  "ok": true,
  "command": "call",
  "model": "grok-4.5",
  "output": "...",
  "error": null,
  "exit_code": 0,
  "timed_out": false,
  "duration_ms": 1234
}
```

On failure, `ok` is false, `error` is `{ "code": "...", "message": "..." }`, and process exit matches `exit_code`.

## Plugin hosts

Codex / Claude plugin manifests live at:

- `.codex-plugin/plugin.json`
- `.claude-plugin/plugin.json`

Both expose skill `skills/clx-grok-call/`. Install the CLI on host PATH first; the skill only documents how to invoke it.

## License

Apache-2.0
