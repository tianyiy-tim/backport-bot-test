# CLAUDE.md

Guidance for anyone (human or AI) working in this repository.

## What this repo is

Two things live here:

1. **The backport tool** — `awslc-backport/`. A local, **pre-merge** impact-analysis
   and backport helper for AWS-LC. Given a fix as a patch (or a commit/working-tree
   diff), it decides which supported release branches are affected and can
   cherry-pick it onto local branches for review. This is the current, maintained
   tool; see `awslc-backport/README.md` (usage) and `awslc-backport/CLAUDE.md`
   (architecture) for everything.

2. **A POC test environment** — a synthetic multi-branch history
   (`AWS-LC-FIPS-2020` … `AWS-LC-FIPS-2025`, `NetOS`) with C fixture files
   (`app.c`, `crypto/`, `tls/`, `utils/`) that exercise rename tracking,
   cherry-pick conflicts, and impact-analysis edge cases. `scripts/` holds the
   shell scripts that build this fixture.

## Layout

```
awslc-backport/     the tool: cli.py (analyze/apply), engine.py, ai.py, testing/
                    (see its own README.md + CLAUDE.md)
scripts/            setup_test_fixture_*.sh, setup_oidc_role.sh (build the fixture)
crypto/ tls/ utils/ app.c timeline.tsv    the synthetic C fixture
docs/               historical design + testing-report docs
.github/            legacy GitHub Actions automation (see "Legacy" below)
TESTING_OBSTACLES.md   problems hit during development and how they were solved
```

## Using the tool

Run from `awslc-backport/`, pointing at an AWS-LC checkout (`--repo`, or
`$BACKPORT_REPO_PATH`, else cwd) that has the release branches fetched:

```bash
cd awslc-backport
# analyze a fix (a commit, a patch file, or the repo's uncommitted diff):
python3 cli.py analyze --commit <sha> --repo <aws-lc>
# cherry-pick onto local backport branches (never pushes / opens a PR):
python3 cli.py apply --all-affected --repo <aws-lc>
# remove the cached run state:
python3 cli.py clear --repo <aws-lc>
```

Add `--no-ai` for the deterministic-only path; otherwise the advisory AI layer
(Amazon Bedrock) refines the inconclusive branches. See the tool's README for the
full flag list and the credentials setup.

## Testing

Both test layers live under `awslc-backport/testing/`:

```bash
cd awslc-backport
python3 -m unittest testing.test_engine          # fast unit tests (no repo/creds)
python3 testing/replay_real_cve.py --file testing/reliable_cves.txt \
    --answers testing/answer_key.txt --no-ai     # replay bench vs a hand-verified key
```

The bench rolls a throwaway sandbox back to before each fix and grades the
engine; the curated cases are `reliable_cves.txt` + `answer_key.txt`. See
`TESTING_OBSTACLES.md` for the history behind the current design.

## Legacy

`.github/workflows/backport.yml` and `scripts/setup_oidc_role.sh` are the earlier
**post-merge, GitHub-Actions** design (a merged PR with a `needs-backport` label
triggered the bot to cherry-pick and open PRs). The tool has since moved to the
local pre-merge CLI in `awslc-backport/`; treat the workflow as historical unless
it is reworked for the new model.
