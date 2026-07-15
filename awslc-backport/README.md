# AWS-LC Backport Bot

Local, **pre-merge** impact analysis and backporting for AWS-LC release branches.
Given a fix as a **patch** (a `git diff` or `git format-patch`), it decides which
supported branches are affected and can cherry-pick the fix onto local backport
branches for review. Working from a patch means an embargoed security fix can be
assessed before any public commit. **Nothing is ever pushed, merged, or turned
into a PR** — `apply` only creates local `backport/<branch>/<id>` branches.

## Layout

```
awslc-backport/
  backport          Wrapper script (bridges AWS_REGION, runs src/main.py).
  backport-bot.yml  Reference GitHub Actions workflow (copy into .github/workflows/).
  requirements.txt  Runtime deps for the AI layer (anthropic, boto3).
  README.md         This file.
  CLAUDE.md         Architecture / maintainer notes.
  src/
    main.py         Entrypoint: argument parser + subcommand dispatch.
    gitutil.py      Git plumbing, throwaway worktrees, cherry-pick, repo targeting.
    patches.py      Patch -> temp commit, patch-source resolution, test-file prompt.
    runstate.py     The analyze -> apply run-state cache.
    verdicts.py     Deterministic bucketing + the advisory AI passes.
    render.py       The analyze table / JSON output.
    analyze.py      The `analyze` command.
    apply.py        The `apply` and `clear` commands.
    ci.py           The `ci` command (post-merge PR automation).
    resolve.py      The `resolve` command (interactive local conflict fixing).
    engine.py       Deterministic core: branch resolution, impact analysis
                    (is_branch_affected, vulnerable_preimage_present), git helpers.
    ai.py           Advisory AI auditor / tie-breaker (never changes a verdict alone).
    common.py       Shared verdict constants + the BackportError type.
  testing/
    replay_real_cve.py        Real replays: roll a sandbox back to before a fix
                              and grade the engine against what the team shipped.
    reliable_cves.txt         Curated, hand-verified test bench.
    answer_key.txt            Per-fix hand-verified AFFECTED branch sets.
    fips_versions.aws-lc.json Support-window manifest (from VERSIONING.md).
    test_engine.py            Fast unit tests for the pure engine helpers.
```

## Usage

Point the tool at an AWS-LC checkout with `--repo <path>` (or `$BACKPORT_REPO_PATH`,
else the current directory). The checkout must have the release branches fetched
(`origin/fips-*`, `origin/NetOS`, `origin/main`).

```bash
# analyze the repo's current uncommitted fix (git diff HEAD):
./backport analyze --repo <aws-lc>

# or an explicit patch from anywhere:
git -C <aws-lc> diff > fix.patch
./backport analyze fix.patch --repo <aws-lc>

# cherry-pick onto local backport branches (no push, no PR):
./backport apply --all-affected --repo <aws-lc>

# interactively resolve any conflicts and open one PR per affected branch:
./backport resolve --pr <number> --repo <aws-lc>     # or --commit <sha>
```

(`./backport` is the wrapper; equivalently `python3 src/main.py <cmd>`.)

## Post-merge automation (GitHub Actions)

The `ci` subcommand is the automated, post-merge counterpart to the local flow:
given a **merged** commit it analyzes every supported branch (AI layer on) and
opens a backport PR for each AFFECTED branch. Clean cherry-picks become PRs into
the release branch (**never auto-merged**). Conflicting branches are **reported
only** — the summary lists the clashing files per branch and points to `resolve`;
nothing is modified and no draft PR is opened (an in-progress conflict can only be
resolved live, not from a committed-markers branch).

```bash
# what CI runs (open PRs on the fork for a merged commit):
./backport ci --commit <merged-sha> --pr <source-pr-number>
./backport ci --commit <merged-sha> --dry-run   # analyze + cherry-pick, no push/PR
```

Safety: `ci` **refuses to run against upstream `aws/aws-lc`** — it only ever
pushes branches and opens PRs on a fork (`--remote`, default `origin`).

## Resolving conflicts (`resolve`)

When `ci` (or `apply`) reports a conflict, `resolve` fixes it locally with a
human in the loop. Given a fix (`--pr <number>` or `--commit <sha>`) it finds the
AFFECTED branches and, for each one that conflicts, checks it out in a real `git
worktree` and walks you through the conflicted files **one at a time**:

```
crypto/fipsmodule/dh/dh.c requires conflict resolution, has the conflict been resolved? [Y/N]
```

Edit the file in the printed worktree path, answer `Y`, and it re-scans for
leftover `<<<<<<<` / `>>>>>>>` markers (refusing to stage a half-fixed file) before
moving to the next file, then the next branch. Clean cherry-picks are skipped
(`ci`/`apply` open those). `git rerere` is enabled, so a resolution recorded on
one branch is auto-applied to identical conflicts on sibling branches (e.g. the
FIPS twins) — you still confirm each one. When the conflicts are resolved it asks
whether to open PRs, then pushes and opens **one normal (non-draft) PR per
resolved branch**, titled `[backport <branch>] <fix subject>`.

```bash
./backport resolve --pr 3337 --repo <aws-lc>
./backport resolve --commit <sha> --no-ai --repo <aws-lc>
```

Like `ci`, `resolve` targets a fork only. It is interactive, so run it in a
terminal (not a pipe/CI).

To wire it up, copy `backport-bot.yml` into the fork's `.github/workflows/`. It
triggers when a PR is merged carrying the `needs-backport` label, and needs a
`BEDROCK_ROLE_ARN` secret (OIDC role for the AI layer). Without it the tool still
runs deterministically and flags anything it cannot confirm as AFFECTED.

`analyze` gives every supported branch a definite verdict — AFFECTED / not
affected / already patched. The deterministic check (ancestry + patch-id +
pre-image + file presence) decides the clear branches; anything it cannot confirm
is sent to the AI advisory, and if the AI is uncertain or unavailable the branch
is flagged AFFECTED for review — **never silently dropped**. `--no-ai` runs
deterministic-only (inconclusive branches are flagged AFFECTED). The run is saved
so a later `apply` reuses it.

### AWS credentials (for the AI layer)

The advisory layer uses Amazon Bedrock via the `anthropic` SDK and the boto3
default credential chain. If the SDK/credentials are unavailable, the AI path
skips and the deterministic engine runs alone. `BACKPORT_DISABLE_AI=1` forces it off.

## Testing

```bash
# Unit tests (no repo, creds, or network):
python3 -m unittest testing.test_engine

# Real replays (needs a local aws-lc clone; set AWS_LC_REPO or pass --repo):
python3 testing/replay_real_cve.py --file testing/reliable_cves.txt \
    --answers testing/answer_key.txt --no-ai
python3 testing/replay_real_cve.py 3107 --no-ai      # a single fix
```

## Key environment variables

| Name | Purpose |
|---|---|
| `BACKPORT_REPO_PATH` | Default AWS-LC checkout (else `--repo`, else cwd). |
| `AWS_LC_REPO` | Repo used by the replay harness. |
| `BACKPORT_VERSIONS_MANIFEST` | FIPS branch manifest path (default `fips_versions.json`). |
| `BACKPORT_BRANCH_PREFIXES` | Supported-branch prefixes when no manifest is present. |
| `BACKPORT_MAINLINE_REF` | Mainline ref (default `origin/main`). |
| `BACKPORT_GENERATED_PATHS` | Generated-file prefixes excluded from patch-id matching (default `generated-src`). |
| `BEDROCK_MODEL_ID` | Bedrock model / inference profile. |
| `BACKPORT_DISABLE_AI` | `1` forces the deterministic-only path. |

See `CLAUDE.md` for the architecture and the rationale behind each analysis path.
