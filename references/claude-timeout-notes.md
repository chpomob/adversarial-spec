# Claude Fable 5 Timeout Notes

When using Claude Fable 5 as spec-challenger or plan-challenger:

- Extended thinking takes 8-12 min per response
- The default adversarial-spec/adversarial-plan timeout of 600s is often insufficient
- Increase `--timeout` to at least 1200 when Claude is the `--review-cmd`
- Pair with `--hard-timeout 1800` inside the claude-tmux command
- If Claude exits code 3 (REJECT) due to non-parseable JSON, the output artifact
  may contain conversation text instead of JSON — retry with DeepSeek or GLM-5.2
  as fallback

Validated: 2026-07-10, adversarial-spec with Claude Fable 5 succeeded at 1200s timeout.
