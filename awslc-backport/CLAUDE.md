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

The CLI source lives in `src/` and is split by responsibility (mirroring the
small-file layout of the team's other util tools) so no single file is a wall of
code. Docs, the `backport` wrapper, `requirements.txt`, the reference workflow,
and `testing/` sit at the tool root.

- `src/main.py` — entrypoint: the argument parser and subcommand dispatch, plus the
  top-level `BackportError` handler. Also `backport`, a shell wrapper.
- `gitutil.py` — everything that shells out to git: `run`/`git`, throwaway
  `temp_worktree` + persistent `add_worktree`/`remove_worktree`, the
  `cherry_pick_local` primitive (shared by apply + ci; aborts on conflict), the
  `resolve`-flow helpers (`unmerged_files`, `file_has_conflict_markers`,
  `enable_rerere`), `resolve_commit` (commit-ish -> fix_sha/subject, merge-commit
  aware), the two `git diff-tree` parsers, and repo targeting (`resolve_repo_path`
  / `target_repo` / `resolve_patch_path`).
- `patches.py` — `commit_from_patch` (patch -> one temp commit in a worktree;
  always `reset --soft base` + recommit so the fix collapses to its **net diff**,
  even from a multi-commit `git am`), patch-source resolution (`read_patch` /
  `resolve_patch_and_base`, with `--commit` accepting a single ref *or* a range
  `A..B`/`A...B` via `_range_endpoints`), and the test-file confirmation prompt.
- `runstate.py` — the `analyze` -> `apply` cache (`save_run` / `load_run` /
  `delete_patch_artifacts`), stored inside the tool folder.
- `verdicts.py` — `bucket_branches` (deterministic classification) and the two
  advisory AI passes (`resolve_unsure`, `review_suspect_affected`,
  `resolve_inconclusive`).
- `render.py` — the analyze table, backport hint, and JSON output.
- `analyze.py` / `apply.py` / `ci.py` / `resolve.py` — one file per command
  (`cmd_analyze`; `cmd_apply` + `cmd_clear`; `cmd_ci`; `cmd_resolve`).
- `common.py` — shared leaf module: the verdict constants (`AFFECTED` …, `LABEL`)
  and the `BackportError` type everyone can import without a cycle.
- `engine.py` — deterministic core. Repo targeting (`set_repo_path` / `REPO_PATH`
  / `_git`), branch resolution, `find_introducing_commit`, `is_branch_affected`,
  `present_introducers`, `is_already_patched`, `vulnerable_preimage_present`, and
  the git/text helpers.
- `ai.py` — `ai_impact_analysis` (advisory only; never acts alone). Bedrock via
  the `anthropic` SDK; degrades to `None` with no SDK/credentials.

Import DAG (no cycles): `common` <- `gitutil` <- {`patches`, `verdicts`} <-
{`analyze`, `ci`} <- `main`; `resolve` <- {`ci`, `gitutil`, `patches`, `verdicts`,
`render`}; `apply` <- `resolve` (for the on-conflict handoff) <- `main`; `render`
<- `common`; `engine`/`ai` are the leaf impact core.

## Conflict handling: `ci` reports, `resolve` fixes

Cherry-picks are attempted in throwaway worktrees. A **clean** pick lands on a
local `backport/<branch>/<id>` branch; `ci` pushes it and opens a normal PR. A
**conflicting** pick is `git cherry-pick --abort`ed — `cherry_pick_local` returns
`("conflict", None, [{path, kind}, ...])`, leaving nothing behind (no
committed-markers branch, no draft PR). One exception: a conflict confined to
**test/generated files only** is auto-resolved by `_drop_and_continue` (restore
the branch's version of each test file, i.e. drop the fix's test churn, then
`cherry-pick --continue`) and returns `("clean", local_branch, dropped)` where
*dropped* lists the test files — so a trivial test clash becomes a normal PR (noted
in the body) instead of manual resolution. `ci` only *reports* real source
conflicts (the summary cell lists the clashing files and points at `resolve`);
`apply` reports them too.

`resolve` (`resolve.py`) is the interactive fixer. It buckets like `ci`, then for
each AFFECTED branch checks it out in a **persistent** `add_worktree` and runs the
cherry-pick live. A **clean** pick is aborted and skipped (`ci`/`apply` own clean
backports; re-opening them here would clash on the same branch name). A
**conflicting** pick drops the user into an interactive shell (`_edit_in_branch_shell`
-> `subprocess.call([$SHELL], cwd=wt)`) *inside* that branch's worktree, so they
edit the live conflict in place. On exit, `_stage_resolved` stages every unmerged
file that no longer has markers and returns the ones that still do; if any remain
the user is offered a re-enter, otherwise `cherry-pick --continue` runs (unless the
user already continued/aborted it themselves, detected via
`_cherry_pick_in_progress` + a HEAD-vs-base check). Then it creates the local
branch, removes the worktree, and (after a final Y/N) opens one non-draft PR per
**resolved** branch. `git rerere` is enabled (`enable_rerere`, autoupdate **off**
on purpose) so a resolution recorded on one branch auto-applies to a twin branch's
identical conflict but still surfaces marker-free for the user to verify. The
user's own checkout is never touched; unfinished branches leave their worktree in
place and are not PR'd. After opening PRs, if `--pr` was given, `resolve` posts an
updated `_summary_table` comment on the source PR (reusing `ci`'s renderer) with
the resolved branches now shown as opened PRs (`done`/`opened` cell kinds).

`--in-place` swaps each conflicting branch into the user's working repo (detached)
instead of a worktree — same resolve logic (`_resolve_branch_in_place`), guarded by
a clean-tree check, restoring the original branch at the end.

To avoid running the impact analysis twice, `ci` embeds a hidden machine-readable
snapshot in its summary comment (`_plan_marker` -> `<!-- backport-bot-plan:{json} -->`
with each branch's impact/outcome/conflict-files). With `--pr`, `resolve` reads the
latest such marker (`_read_bot_plan`) and targets exactly the `conflict` branches
— no second AI pass, and it seeds the final summary with the branches `ci` already
opened. `--reanalyze` forces the local `bucket_branches`+`resolve_inconclusive`
path instead; that path is also the automatic fallback when no marker is found.

The resolution engine itself is `_run_resolution(args, fix_sha, subject, buckets,
targets, preopened, source_pr)`, shared by two front-ends: `cmd_resolve` (targets
from the PR plan or a local analysis) and `apply` (which, after a local
cherry-pick session conflicts, prompts the user and hands off the just-conflicted
branches directly — no re-analysis, no second bucketing). `_assert_fork_remote`
only gates the push step, so purely-local resolution works regardless of remote.

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
