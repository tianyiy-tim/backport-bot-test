# CLAUDE.md — backport bot architecture & maintainer notes

## What this is

A **pre-merge**, patch-driven backport tool for AWS-LC. Given a fix as a patch
(`git diff` or `git format-patch`), it decides which supported release branches
are affected and can cherry-pick it onto local branches for review — before any
public commit, so embargoed security fixes can be assessed early.

Two hard safety rules:
- **Nothing is pushed, merged, or turned into a PR.** `apply` only creates local
  `backport/<branch>/<id>` branches.
- **A branch is never a silent NOT AFFECTED.** Anything the deterministic check
  can't confirm goes to the AI; if the AI is uncertain/unavailable the branch is
  flagged AFFECTED for review.

## Files

The CLI is split by responsibility (mirroring the small-file layout of the team's
other util tools) so no single file is a wall of code:

- `main.py` — entrypoint: the argument parser and subcommand dispatch, plus the
  top-level `BackportError` handler. Also `backport`, a shell wrapper.
- `gitutil.py` — everything that shells out to git: `run`/`git`, throwaway
  `temp_worktree`, the `cherry_pick_local` primitive (shared by apply + ci), the
  two `git diff-tree` parsers, and repo targeting (`resolve_repo_path` /
  `target_repo` / `resolve_patch_path`).
- `patches.py` — `commit_from_patch` (patch -> temp commit in a worktree),
  patch-source resolution (`read_patch` / `resolve_patch_and_base`), and the
  test-file confirmation prompt.
- `runstate.py` — the `analyze` -> `apply` cache (`save_run` / `load_run` /
  `delete_patch_artifacts`), stored inside the tool folder.
- `verdicts.py` — `bucket_branches` (deterministic classification) and the two
  advisory AI passes (`resolve_unsure`, `review_suspect_affected`,
  `resolve_inconclusive`).
- `render.py` — the analyze table, backport hint, and JSON output.
- `analyze.py` / `apply.py` / `ci.py` — one file per command (`cmd_analyze`;
  `cmd_apply` + `cmd_clear`; `cmd_ci`).
- `common.py` — shared leaf module: the verdict constants (`AFFECTED` …, `LABEL`)
  and the `BackportError` type everyone can import without a cycle.
- `engine.py` — deterministic core. Repo targeting (`set_repo_path` / `REPO_PATH`
  / `_git`), branch resolution, `find_introducing_commit`, `is_branch_affected`,
  `present_introducers`, `is_already_patched`, `vulnerable_preimage_present`, and
  the git/text helpers.
- `ai.py` — `ai_impact_analysis` (advisory only; never acts alone). Bedrock via
  the `anthropic` SDK; degrades to `None` with no SDK/credentials.

Import DAG (no cycles): `common` <- `gitutil` <- {`patches`, `verdicts`} <-
{`analyze`, `apply`, `ci`} <- `main`; `render` <- `common`; `engine`/`ai` are the
leaf impact core.

## Bucketing (`verdicts.bucket_branches`)

Per branch, deterministically (no AI):
1. `is_branch_affected(introducers, branch)` (ancestry + patch-id) → AFFECTED
   (or ALREADY if `is_already_patched`).
2. `vulnerable_preimage_present` True (the exact lines the fix removes are still
   on the branch) → AFFECTED. Catches a branch-specific introducer that ancestry
   / patch-id miss.
3. else file present (rename-aware, plus a same-basename guard) → UNSURE.
4. else → NOT_AFFECTED.

Then the CLI resolves UNSURE with the AI (`resolve_unsure`) — likely-affected →
AFFECTED, likely-not → NOT_AFFECTED, uncertain/unavailable → AFFECTED (flagged).
A second pass (`review_suspect_affected`) attaches a false-positive review note to
AFFECTED branches that match only part of the fix's lineage (the newest introducer
absent — the classic old-shared-code over-flag). That pass is **advisory only**;
it never changes a verdict, so it can reduce noise but never cause a miss.

## Impact analysis internals (`engine.is_branch_affected`)

Called with just `(introducers, branch)` it does ancestry (Path 1) + patch-id
(Path 2) and returns `(affected, None)` — which is what bucketing uses. Called
with `commit` + `changed_files` it additionally runs the pre-image paths and the
AI auditor/tie-breaker (used by the replay harness). Key points:
- `find_introducing_commit` takes the OLDEST introducer per changed line range;
  comment/blank/punctuation-only hunks and test/generated files are skipped so a
  stale comment can't trace to an ancient import and over-flag.
- `vulnerable_preimage_present` compares the fix's removed lines (comment- and
  whitespace-normalized, boilerplate/test lines filtered) against the branch:
  True = still vulnerable, False = provably absent, None = pure addition.

## Testing

- `testing/test_engine.py` — fast, repo-free unit tests of the pure helpers.
  Run: `python3 -m unittest testing.test_engine`.
- `testing/replay_real_cve.py` — the characterization test. Rolls a throwaway
  sandbox back to before each fix (real objects borrowed read-only via git
  alternates; the real repo is never mutated), runs the engine, and grades it
  against a hand-verified answer key or auto-discovered git ground truth.
  Bench status: 210 (fix × branch) cells, **0 deterministic over-flags**, 0 false
  negatives with AI on. Run the bench before and after any engine change and
  confirm the scorecard is unchanged.

## Known design constraints

- The oldest-introducer heuristic over-flags rather than misses — the safer
  direction. The suspect-over-flag review and the AI trim those; they never
  downgrade a verdict on their own.
- `vulnerable_preimage_present` is precise but is only a positive/negative signal,
  not a substitute for ancestry; it is filtered to distinctive, non-boilerplate,
  non-test/non-generated lines.
- The engine's git calls use the process working directory; the CLI chdirs to the
  resolved repo (and sets `REPO_PATH`). Throwaway worktrees always pass an
  explicit cwd, so they are unaffected.
