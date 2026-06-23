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

Python 3 only. The deterministic path needs no network or credentials. The
`explain` command additionally needs AWS Bedrock access (`anthropic[bedrock]` +
credentials); without them it simply reports that the AI advisory is unavailable.

## Workflow

```
get internal security issue
        -> patch the fix on mainline locally        (git diff > fix.patch)
        -> ./backport analyze fix.patch             (lists affected / unsure / not affected)
        -> ./backport explain                       (optional: AI justification for the unsure ones)
        -> review and decide
        -> ./backport apply --all-affected          (local backport/<branch>/<id> branches)
        -> push + open PRs for human review          (only when ready / post-embargo)
```

## Commands

```sh
# 1. Bucket every supported branch. Deterministic, no AI, no network.
./backport analyze fix.patch
./backport analyze fix.patch --branches AWS-LC-FIPS-4.0 NetOS    # limit scope
./backport analyze fix.patch --json                             # machine-readable

# 2. AI justification for the UNSURE branches (advisory only; the only command
#    that sends code to Bedrock, and only when you ask).
./backport explain
./backport explain AWS-LC-FIPS-3.0                              # force a branch

# 3. Cherry-pick the patch onto local branches. Reports clean vs conflict.
#    Never pushes / opens a PR / auto-merges.
./backport apply --all-affected
./backport apply --branches AWS-LC-FIPS-3.0 AWS-LC-FIPS-4.0 --yes
```

`analyze` saves the run, so `explain` and `apply` reuse the same patch and base
without re-passing them. Override with `--patch <file>` / `--base <ref>` anytime.

## Buckets

| Bucket | Meaning |
|---|---|
| `AFFECTED` | The introducer is in the branch's history (ancestry or patch-id). Needs a backport. |
| `not affected` | The touched files do not exist on the branch. Confident skip. |
| `UNSURE` | The file is present but history cannot confirm the introducer reached it (typically after a rename or rewrite). This is what `explain` reasons about. |
| `already patched` | The fix is already on the branch under a different SHA. Skip as redundant. |

## Notes

- **Embargo safety:** `analyze` and `apply` are fully local and never touch the
  network. Only `explain` calls Bedrock, so for an embargoed fix you can skip it
  and rely on the deterministic buckets.
- **Drifted mainline:** if the patch will not apply cleanly on `origin/main`,
  add `--3way`, or point at the right base with `--base <ref>`.
- **Results land as local branches** named `backport/<branch>/<shortsha>`.
  Inspect them with `git branch --list 'backport/*'`, then push and open PRs by
  hand when you are ready.
- **Patch path** is resolved relative to your current directory first, then the
  repo root, so `git diff > fix.patch` at the repo root followed by
  `cd tools/backport && ./backport analyze fix.patch` works either way.
