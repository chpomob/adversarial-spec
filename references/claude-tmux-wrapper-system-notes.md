# Claude-tmux Wrapper Usage Notes (v1)

This reference documents one wrapper's observed behavior. It does not prescribe
provider selection, model selection, or fallback ordering for adversarial skills;
those choices remain in external configuration.

## Wrapper entry point
Configure the wrapper entry point externally and refer to it by its portable
command name, `claude-tmux.py` (342 lines in the reviewed v1). Shared reference
assets must not record a user-specific absolute path.

## Key flags

| Flag | Purpose | Note |
|------|---------|------|
| `--timeout N` | Inactivity timeout in seconds (default 300) | Sets how long to wait for Claude to write output. If Claude responds in chat instead of using Write tool, the wrapper waits this long before timing out. |
| `--hard-timeout N` | Wall-clock timeout (default 0 = disabled) | Total max runtime regardless of activity. Useful as safety net. |
| `--cwd DIR` | Working directory for the tmux session | Required for adversarial pipeline — without it, tmux starts in $HOME and models can't find spec.md/plan.md. |
| `--model MODEL` | Model name (default: sonnet) | Do NOT force this from provider config — let the wrapper use its default. If the user wants a specific model, they change it here, not in adversarial configs. |
| `--max-turns N` | Max conversation turns (default 10) | Rarely needs changing for adversarial pipeline (single-turn prompt). |
| `--no-danger` | Skip `--dangerously-skip-permissions` | Default is DANGEROUS (danger=True). Pass `--no-danger` for untrusted input. |

## Does NOT support `--yolo`
The v1 wrapper has `args.danger = True` by default. There is no `--yolo` flag.
Passing `--yolo` causes argparse to exit with code 2 (unknown argument).
Just omit it entirely. Validated 2026-07-16.

## Known failure mode: Claude replies in chat instead of Write tool
The wrapper appends to the prompt:
```
When you are done, write your response to {output_file}
using the Write tool. After the file is written, create an empty
file at {done_sentinel} using the Write tool to signal completion.
```

Sometimes Claude outputs JSON into the chat pane instead of using the Write tool.
The wrapper then waits indefinitely (until --timeout) for output.txt/done.sentinel to appear.

**Workaround:** The findings are visible in the tmux pane. Capture with:
```
tmux capture-pane -t claude-tmux-<PID> -p
```
Then kill the stuck session:
```
tmux kill-session -t claude-tmux-<PID>
```

The adversarial pipeline will get an error exit from the wrapper. If the WRITE phase
already committed, the branch is recoverable from git reflog.

## Model defaults
Default model is `sonnet` (defined in the wrapper's argparse). In the reviewed
environment, the aliases resolved as follows:
- `sonnet` = Claude Sonnet 4 (fast, reliable JSON, no extended thinking)
- `best` = Claude Fable 5 (extended thinking 8-12 min, separate quota from Pro)

Don't hardcode `--model` in provider config. The wrapper's default is fine for most cases.
