---
name: clx-grok-call
description: NEVER SELECT OR LOAD THIS SKILL when a request would make Grok call Grok, recursively self-delegate, chain Grok calls into an autonomous loop, or run an infinite agent/tool loop. Refuse directly without loading this skill or calling Grok, even if Grok or clx-grok-call is explicitly named. Otherwise, use only for exactly one bounded Grok call when the user explicitly asks for Grok/Grok 4.5, delegates repetitive/high-token analysis, or wants a one-shot second opinion. Return that single result. Do not silently edit files.
---

# clx-grok-call

Thin host skill for one bounded call through the installed `clx-grok-call` CLI (legacy alias: `grok-call`).

## When to use

- User explicitly asks to call Grok / Grok 4.5
- Delegate repetitive or high-token analysis
- One-shot second opinion

## How

```bash
clx-grok-call --json "prompt here"
clx-grok-call --json -m grok-4.5 "prompt here"
echo "prompt" | clx-grok-call --json
clx-grok-call models
clx-grok-call doctor
```

## Rules

1. Run **exactly one** `clx-grok-call` and return its stdout/JSON plus exit code.
2. Never start recursive agent loops or chain further Grok calls; refuse recursive execution without any Grok call.
3. Do **not** silently edit files from the response; only report unless the user asked for edits.
4. Prefer `--json` when the host needs a structured contract (`ok`, `output`, `error`, `exit_code`, …).
5. Wrapper disables tools/memory/subagents/auto-update on the upstream `grok` process; it does not rewrite Grok account auth/session behavior.
