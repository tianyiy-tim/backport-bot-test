# backport (local CLI)

Local, patch-driven backport tool for AWS-LC. It runs the same impact engine as
`scripts/backport_bot.py`, but from a **patch** instead of a merged commit, so an
embargoed security fix can be assessed (and backported to local branches) before
any public code change. Nothing is pushed, no PR is opened, and nothing is ever
auto-merged.

## Requirements

Run it from inside an AWS-LC clone that has the release branches fetched:

```sh
git fetch origin          # need origin/main, origin/AWS-LC-FIPS-*, origin/NetOS
```

Python 3 only. The deterministic check needs no network or credentials. By
default `analyze` consults AWS Bedrock (`anthropic[bedrock]` + credentials) to
decide the branches the deterministic check cannot confirm; pass `--no-ai` to
stay fully local (those branches are then flagged AFFECTED for review).

## Workflow

```
get internal security issue
        -> patch the fix on mainline locally        (git diff > fix.patch)
        -> ./backport analyze fix.patch             (affected / not affected for every branch)
        -> ./backport explain                       (optional: see the AI reasoning)
        -> review and decide
        -> ./backport apply --all-affected          (local backport/<branch>/<id> branches)
        -> push + open PRs for human review          (only when ready / post-embargo)
```

## Commands

```sh
# 1. Verdict for every supported branch: affected or not affected. The
#    deterministic check decides what it can; the AI is consulted automatically
#    on the branches it cannot confirm.
./backport analyze fix.patch
./backport analyze fix.patch --no-ai                            # deterministic only
./backport analyze fix.patch --branches AWS-LC-FIPS-4.0 NetOS    # limit scope
./backport analyze fix.patch --json                             # machine-readable

# 2. See the AI's full reasoning for the branches the deterministic check could
#    not confirm (or force a specific branch).
./backport explain
./backport explain AWS-LC-FIPS-3.0

# 3. Cherry-pick the patch onto local branches. Reports clean vs conflict.
#    Never pushes / opens a PR / auto-merges.
./backport apply --all-affected
./backport apply --branches AWS-LC-FIPS-3.0 AWS-LC-FIPS-4.0 --yes
```

`analyze` saves the run, so `explain` and `apply` reuse the same patch and base
without re-passing them. Override with `--patch <file>` / `--base <ref>` anytime.

## How a verdict is reached

`analyze` reports `AFFECTED`, `not affected`, or `already patched` for every
branch, plus a `basis` column saying how it was decided:

| Verdict | How it is reached |
|---|---|
| `AFFECTED` (deterministic) | The introducer is in the branch's history (ancestry or patch-id). |
| `AFFECTED` (AI / flagged) | History could not confirm it but the file is present; the AI judged it likely affected, or the AI was uncertain/unavailable so it is flagged for review (never silently dropped). |
| `not affected` | The changed code is confidently absent from the branch, or the AI judged it not affected. |
| `already patched` | The fix is already on the branch under a different SHA. Skip as redundant. |

The key safety rule: a branch only becomes `not affected` when we are confident
(the code is absent, or the AI judged it not affected). Anything the
deterministic check cannot confirm goes to the AI, and anything the AI cannot
confirm is flagged `AFFECTED`, so an affected branch is never silently missed.

## Notes

- **Embargo safety:** `analyze --no-ai` and `apply` are fully local and never
  touch the network. Default `analyze` sends the diff and branch source to your
  Bedrock account only for the inconclusive branches; use `--no-ai` to avoid
  that entirely for an embargoed fix.
- **Drifted mainline:** if the patch will not apply cleanly on `origin/main`,
  add `--3way`, or point at the right base with `--base <ref>`.
- **Results land as local branches** named `backport/<branch>/<shortsha>`.
  Inspect them with `git branch --list 'backport/*'`, then push and open PRs by
  hand when you are ready.
- **Patch path** is resolved relative to your current directory first, then the
  repo root, so `git diff > fix.patch` at the repo root followed by
  `cd tools/backport && ./backport analyze fix.patch` works either way.
