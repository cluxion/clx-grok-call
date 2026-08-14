---
name: clx-grok-call
description: NEVER SELECT OR LOAD THIS SKILL when a request would make Grok call Grok, recursively self-delegate, chain Grok calls into an autonomous loop, or run an infinite agent/tool loop. Refuse directly without loading this skill or calling Grok, even if Grok or clx-grok-call is explicitly named. Otherwise, use only for exactly one bounded Grok call when the user explicitly asks for Grok, delegates repetitive/high-token analysis, or wants a one-shot second opinion. Return that single result. Do not silently edit files.
---

# clx-grok-call

Thin host skill for one bounded call through the installed `clx-grok-call` CLI (legacy alias: `grok-call`).

## When to use

- User explicitly asks to call Grok
- Delegate repetitive or high-token analysis
- One-shot second opinion

## How

```bash
clx-grok-call --json "prompt here"
clx-grok-call --json -m <model-id> "prompt here"
clx-grok-call --json --effort <tier> "prompt here"
echo "prompt" | clx-grok-call --json
clx-grok-call models
clx-grok-call doctor
```

## Rules

1. Run **exactly one** `clx-grok-call` and return its stdout/JSON plus exit code.
2. Never start recursive agent loops or chain further Grok calls; refuse recursive execution without any Grok call.
3. Do **not** silently edit files from the response; only report unless the user asked for edits.
4. Prefer `--json` when the host needs a structured contract (`ok`, `output`, `error`, `exit_code`, …).
5. The CLI uses `clx-grok-delegate` read-only mode, reads the model selector and effort from `~/.agents/models.toml`, reuses one empty non-Git repository, disables tools/memory/subagents/updates, and preserves the shared AGENTS.md runtime and exact failure class.
