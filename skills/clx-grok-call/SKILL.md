---
name: clx-grok-call
description: Exclusion overrides an explicit Grok mention. If the requested execution is an infinite loop, self-chain, recursive agent/tool call, or Grok-calling-Grok, do not select or call this skill; refuse that form directly. Otherwise, use when the user explicitly asks to call Grok/Grok 4.5, delegate repetitive/high-token analysis, or get a one-shot second opinion. Execute exactly one clx-grok-call and return the result. Do not silently edit files.
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
2. Do **not** start recursive agent loops or chain further Grok calls by default.
3. Do **not** silently edit files from the response; only report unless the user asked for edits.
4. Prefer `--json` when the host needs a structured contract (`ok`, `output`, `error`, `exit_code`, …).
5. Wrapper disables tools/memory/subagents/auto-update on the upstream `grok` process; it does not rewrite Grok account auth/session behavior.
