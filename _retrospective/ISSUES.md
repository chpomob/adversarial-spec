# adversarial-spec — retrospective issues

This file is only for manually curated post-mortem notes; the pipeline does not
write to it. `scripts/adversarial_spec.py` instead appends failures to the
`ISSUES.md` inside the configured output directory (`<out_dir>/ISSUES.md`), one
entry per failed phase (phase, branch, error, and last stdout).
