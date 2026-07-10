# AWS-LC Backport Bot

Local, **pre-merge** impact analysis and backporting for AWS-LC release branches.
Given a fix as a **patch** (a `git diff` or `git format-patch`), it decides which
supported branches are affected and can cherry-pick the fix onto local backport
branches for review. Working from a patch means an embargoed security fix can be
assessed before any public commit. **Nothing is ever pushed, merged, or turned
into a PR** — `apply` only creates local `backport/<branch>/<id>` branches.

## Layout

```
backport/
  cli.py       Pre-merge CLI: `analyze` and `apply` subcommands.
  engine.py    Deterministic core: branch resolution, impact analysis
               (is_branch_affected, vulnerable_preimage_present), git helpers.
  ai.py        Advisory AI auditor / tie-breaker (never changes a verdict alone).
  CLAUDE.md    Architecture / maintainer notes.
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
python3 cli.py analyze --repo <aws-lc>

# or an explicit patch from anywhere:
git -C <aws-lc> diff > fix.patch
python3 cli.py analyze fix.patch --repo <aws-lc>

# cherry-pick onto local backport branches (no push, no PR):
python3 cli.py apply --all-affected --repo <aws-lc>
```

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
