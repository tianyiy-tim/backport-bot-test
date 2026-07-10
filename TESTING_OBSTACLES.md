# Testing Obstacles & How We Solved Them

A running record of the problems hit while building and validating the AWS-LC
backport bot, and the fix for each. Written so a future maintainer (or a reviewer
deciding whether to trust the tool) can see *why* the design looks the way it
does. Newest lessons are grouped by theme, not strictly chronological.

---

## 1. Impact-analysis accuracy (the core problem)

### Over-flagging from the oldest-introducer heuristic
- **Problem:** To decide if a branch is affected we trace the *oldest* commit that
  wrote the lines a fix changes (`git log -L --reverse`). That commit is often an
  ancient, shared one — e.g. the initial BoringSSL import — that predates every
  release branch. So nearly every branch matched by ancestry and got flagged
  AFFECTED, even when the real vulnerability wasn't there.
- **Fix:** Corroborate ancestry with the **vulnerable pre-image**
  (`vulnerable_preimage_present`): check whether the exact lines the fix
  removes/changes are still on the branch. If ancestry matched but those lines are
  *absent*, it's an over-flag → downgrade (Path 4). The oldest-introducer bias is
  kept deliberately (it errs toward over-flag, never a silent miss) and trimmed by
  the pre-image check + AI.

### Comments caused false positives
- **Problem:** A stale comment the fix happened to touch traced back through
  history to an ancient commit, re-triggering the over-flag. Renames/whitespace
  had the same effect.
- **Fix:** Ignore non-code lines in impact analysis (`_is_noise_line`): `//` line
  comments, `/* */` block comments, and `#` comments in non-C files (scripts,
  CMake, YAML). Crucially, `#` in C/C++ is a *preprocessor directive* (real code)
  and is kept, or a fix guarded by `#if` would be missed. Boilerplate lines (bare
  `return;`, `#include`, lone string literals) are also filtered so a match has to
  be on distinctive code.

### Variable / small renames shouldn't force a backport
- **Problem:** A cosmetic rename or reformat isn't a security change, but it looked
  like one to a line-level matcher.
- **Fix:** The AI auditor is explicitly told to call out purely cosmetic changes;
  the deterministic pre-image check is whitespace-normalized.

### Branch-specific introducers were missed (false negatives)
- **Problem:** Ancestry/patch-id key on the *mainline* introducer SHA. When a
  branch got the vulnerable code via its own separate commit (different SHA and
  patch-id), the deterministic check missed it.
- **Fix:** Path 2b — a *positive* pre-image match. If the fix's exact removed lines
  are present on the branch, it's AFFECTED even without an ancestry/patch-id hit.

### Reshaped backports remain the one hard case
- **Problem:** Fix `#1294` (RNG use-after-free) genuinely affects older branches,
  but the code was reshaped enough that the exact removed lines don't match →
  Path 4 downgrades it → deterministic false negative.
- **Resolution:** This is the documented trade-off of Path 4 (not false-negative
  safe on its own). It's paired with the **AI tie-breaker**, which re-flags the
  reshaped-but-vulnerable branch. With AI on there are 0 false negatives; the
  `--no-ai` bench surfaces these 3 so the trade-off stays measured and visible.

### Post-quantum path moves (ML-KEM / ML-DSA / Kyber)
- **Problem:** These modules were moved (`crypto/ml_kem/` →
  `crypto/fipsmodule/ml_kem/`, etc.). A fix touches the *new* path; older branches
  that forked before the move have the code at the *old* path, so a naive
  file-existence check said "absent → not affected" and misclassified them.
- **Fix:** Rename-aware file lookup (`_get_file_on_branch` follows `git log
  --follow` history). Also learned to distinguish the experimental *drafts* (IPD
  ML-KEM, R3 Dilithium) that some branches carry from the final-standard code the
  fix targets — when the team shipped no backport and the branch only has the
  distinct draft, it is genuinely NOT affected, not a bug.

---

## 2. Ground truth was the biggest time sink

Testing is only meaningful if the "answer key" is right, and ours was repeatedly
wrong before we hardened it.

- **Auto-discovery missed real backports.** Early ground truth only detected a
  backport via a `-x` "cherry picked from commit" annotation or a matching
  patch-id. AWS-LC frequently backports as a **separate PR** with a new SHA, a new
  PR number, and (because the older branch's context differs) a different patch-id.
  Those were scored as "not backported," which turned real backports into fake
  false positives.
  - **Fix:** Added two more signals to discovery — **`pr-ref`** (a divergent commit
    citing the fix's original PR number with a cherry-pick/backport keyword) and
    **`same-title`** (a divergent commit whose subject matches the fix's, modulo the
    trailing `(#NNNN)`). E.g. `#3108` backports `#3107` with the same title but a
    new number — only `same-title` catches it.
- **"Affected but not shipped" ≠ tool error.** Some fixes simply hadn't been
  backported yet (recency / severity / product call). Flagging those looked like a
  false positive but wasn't a tool bug.
  - **Fix:** The scorecard breaks false positives into *true over-flags* (vulnerable
    code provably absent — real errors) vs *affected-but-unshipped* vs
    *pure-addition/undetermined*, so noise doesn't read as tool failure.
- **"How can 0 branches be affected?" wasn't always a hallucination.** For PQ
  final-standard fixes, every pre-fork branch only had a distinct experimental
  draft, so genuinely no pre-fork branch needed the backport. Understanding
  **forked-after (ancestor)** — a branch that inherited the fix by forking after it
  landed, needing no backport — vs an actual backport removed a lot of confusion.
- **Resolution:** We stopped trusting auto-discovery blindly and **hand-verified
  the answer key against aws/aws-lc on GitHub**, commit by commit, recording it in
  `answer_key.txt`. The replay harness scores against that hand-verified set.

---

## 3. AI integration

- **AI-as-a-fallback couldn't catch over-flags.** Originally the AI only ran when
  the deterministic check was *inconclusive*. But over-flags happen on the
  *confident* ancestry path, which short-circuited before any AI call — so a
  fallback structurally could never catch them.
  - **Fix:** Made AI **always-on**, in one of two roles chosen by the deterministic
    verdict: **auditor** (deterministic said affected → look for a false positive)
    and **tie-breaker** (inconclusive → second opinion).
- **Safety of the AI's influence.** The AI must never cause a *missed* security
  backport.
  - **Fix:** Gated by direction. The tie-breaker can only *add* a backport. The
    auditor can only suppress on HIGH-confidence "not affected" **and** deterministic
    corroboration (pre-image provably absent). In the pre-merge CLI the auditor is
    purely advisory (a review note) and never changes the verdict.

---

## 4. Environment & tooling gotchas

- **`python: command not found`** → use `python3`.
- **AWS creds `403 ... security token expired`** → refresh with `mwinit -o`. Note:
  `ada credentials update` did **not** work (managed account). Set
  `AWS_PROFILE=tianyiy AWS_REGION=us-east-1`.
- **Short SHAs** → standardized the test list on 10–12 char SHAs.
- **"GitHub shows 10 changed files but the tool sees 2."** The tool analyzes the
  squash-merge commit, which contains the net change; the PR page lists every file
  touched across all its commits. Analyze the merge/commit, not the PR file list.
- **Testing was slow** → the replay harness borrows the real repo's objects
  read-only via **git alternates** into a throwaway sandbox (never mutates the real
  repo) and analyzes branches concurrently.
- **`black` crashed with `AF_UNIX path too long`** (its parallel worker socket) →
  run `black` one file at a time. All files are `black`-clean.

---

## 5. Testing a fix that isn't merged yet

- **"The SHA doesn't work."** A PR that is still **open** has no merge commit on
  `main`, and its commit lives on the contributor's fork — so it isn't in a plain
  `aws/aws-lc` clone.
  - **Fix:** Fetch the PR head, which GitHub exposes at `refs/pull/<N>/head`:
    ```
    git fetch origin pull/<N>/head:pr-<N>
    python3 cli.py analyze --commit pr-<N> --repo <aws-lc>
    ```
- **Repo out of date** → `git fetch origin --prune` updates the `origin/*` refs the
  tool reads (no need to touch your working tree).

---

## 6. Architecture / refactor snags

- **Pre-merge vs post-merge.** The first CLI was post-merge (triggered by a merged
  commit + PR number, opened PRs). The real need is **pre-merge**: assess a fix
  from a *patch* before any public commit (so embargoed fixes can be triaged).
  Rebuilt the CLI around `analyze` / `apply` working from a patch, a working-tree
  diff, or `--commit <ref>`. Nothing is pushed or turned into a PR.
- **Circular import (`ai` ↔ `engine`).** `engine.is_branch_affected` calls the AI,
  and `ai` imports helpers from `engine`.
  - **Fix:** `engine` imports `ai_impact_analysis` **lazily** inside the function
    (not at module top), and the CLI imports it from `ai` directly.
- **Branch ordering kept drifting.** Output came out alphabetical (NetOS last, new
  snapshot branch mid-list).
  - **Fix:** A single `engine.sort_branches()` (keyed on the `YYYY-MM-DD` in each
    branch name) is the one source of truth; every listing routes through it.
    Final order is **newest → oldest**, with AFFECTED grouped at the top of the
    analyze table for readability.
- **Run-state left in the repo.** `analyze` writes `.backport-runs/` into the
  target checkout. → `apply` removes it on a clean run, and a `clear` subcommand
  wipes it on demand.

---

## Current status

- Replay bench: **210 (fix × branch) cells, 0 deterministic over-flags**, 0 false
  negatives with AI on (3 in `--no-ai`, the reshaped `#1294` case AI recovers).
- The engine errs toward over-flagging (safe for security); the pre-image checks
  and the AI trim the noise without ever silently dropping a branch.
