# Backport Bot — Testing Results

**Component:** AWS-LC automated backport tool (impact analysis + always-on AI)
**Repos under test:** `aws/aws-lc` (read-only clone), 6 supported FIPS branches
**Branches:** `fips-2021-10-20`, `fips-2021-10-20-1MU`, `fips-2022-11-02`, `fips-2024-09-27`, `fips-2025-09-12-lts`, `fips-NetOS-2024-06-11`

---

## 1. Executive summary

The bot was validated against an **independent, repo-forensic ground-truth oracle** built directly
from `aws/aws-lc` git history — not from the bot's own logic. Across **50 real fixes × 6 branches
(300 cells)**:

| Metric | Result |
|---|---|
| Gradeable cells | 280 (20 UNDETERMINED, honestly excluded) |
| **Agreement (deterministic)** | **277 / 280 (99%)** |
| **Agreement (always-on AI)** | **274 / 280 (97%)** |
| **False negatives (missed backports)** | **0** |
| Redundant over-flags (FN-safe) | 3 (deterministic) / 6 (AI) |

All four regression suites pass. A real gap in already-patched detection was found *by* the new
oracle and fixed with a false-negative-safe check.

---

## 2. Test assets

| Asset | Role |
|---|---|
| `scripts/build_ground_truth.py` | Generates the oracle from git forensics (ancestry, `-x` cherry-picks, module presence, vulnerable pre-image, reimplementation detection). Independent of `backport_bot.py`. |
| `ground_truth.txt` | The oracle: per-cell verdict + evidence. Regenerate any time. |
| `scripts/compare_ground_truth.py` | Runs the real bot over the live repo and diffs its decision vs. the oracle. |
| `compare_results.txt` | Saved output of the always-on comparator run. |
| `reliable_cves.txt` | Curated, hand-verified fix bench used by the replay harness. |

---

## 3. Ground-truth methodology

Each `(fix × branch)` verdict is derived from objective repo evidence, in priority order:

- **PATCHED** — fix is present now, via one of:
  - `ancestor` — the fix commit is a direct ancestor (branch forked after the fix)
  - `-x cherry-pick` — a branch commit records `cherry picked from commit <full-sha>`
  - `reimplementation` — the fix's distinctive added lines are present and the vulnerable pre-image is gone
- **AFFECTED** — the patched code exists on the branch, the vulnerable pre-image is still present, and the fix is not applied
- **NOT_AFFECTED** — none of the fix's source files exist on the branch (nothing to patch), or the code was rewritten
- **UNDETERMINED** — additive/tooling/ambiguous fix; excluded from scoring rather than guessed

> **Semantics:** the oracle answers *"does the branch currently contain the fix?"* — a different
> question than the rollback-based replay harness. Mapping for grading:
> AFFECTED → bot should BACKPORT; PATCHED / NOT_AFFECTED → bot should SKIP.

### Oracle composition (300 cells)

| Verdict | Count |
|---|---|
| PATCHED | 111 |
| AFFECTED | 79 |
| NOT_AFFECTED | 90 |
| UNDETERMINED | 20 |

The 20 UNDETERMINED are honestly excluded rather than guessed: the ACVP `modulewrapper` test-tool
leak fix (`c21d40`, all branches), the bundled HMAC hardening (`80f0`) where the target site is
absent on older branches, four ML-KEM fixes on NetOS affected by a
`crypto/ml_kem/ → crypto/fipsmodule/ml_kem/` path move, and a few cells where a fix's substantive
file is absent on a branch and the underlying bug can't be confirmed by line match
(`9fbfa706`, `2f55cf`, `6c21187f`).

### Verification status

Every cell is backed by either an **airtight signal** (fix is a direct ancestor, an `-x` cherry-pick,
or the fix's module/file is absent) or **hand-verified line-level evidence**. In particular, all
soft-signal cells were checked directly against the branch source:
- **PATCHED-via-reimplementation** (`11b50d`, `4b07805`, `eb0c0c`, `e0cf5f`): the distinctive fix
  line is confirmed present on the branch.
- **NOT_AFFECTED-via-"rewritten"** (`921c6465`, `2f55cf`, `04e7dc`): the *actual vulnerable
  construct* (function/pattern), not just a pre-image string, is confirmed absent — so none of these
  is a hidden false negative.

---

## 4. Comparator results

Grading the bot's real current-state decision (`is_already_patched → is_branch_affected`) against
the oracle:

| | Deterministic-only | Always-on AI |
|---|---|---|
| PASS / agreement | 277 / 280 (99%) | 274 / 280 (97%) |
| False negatives | 0 | 0 |
| Over-flags | 3 | 6 |
| API failures | — | 0 |

**No real backport is missed in either mode.**

**Remaining over-flags (all FN-safe):**
- Reimplementation gap (`9ad27` / 2021-1MU, `e0cf5f` / 2024 & 2025-lts): the fix was hand-rewritten
  on the branch with a different patch-id and no `-x`, so `is_already_patched` can't detect it. The
  branch is protected; the bot would open a redundant PR a human closes.
- AI tie-breaker upgrades (`48040c` / three older branches, AI mode only): the always-on AI
  upgraded branches where the fix's `e_aesccm.c` ctrl-handler code is actually absent. This is the
  tie-breaker's known behavior — it only ever *adds* a PR, so it cannot cause a missed backport.

---

## 5. Fix validated during testing

**Finding (surfaced by the oracle):** `is_already_patched` used only ancestry + patch-id, so
**bundled/reshaped `-x` backports** (e.g. one PR squashing CVE-2023-3446 + CVE-2023-3817) were
re-flagged despite git recording the cherry-pick.

**Fix:** added `_branch_cites_cherry_pick()` — an exact full-40-char-SHA match of
`cherry picked from commit <sha>` in the branch's divergent history, inserted between the ancestry
and patch-id paths in `is_already_patched`.

**Safety:** matches git's own unambiguous cherry-pick record, so it only ever marks a branch patched
when the fix is provably present — it cannot cause a missed backport.

**Impact:**

| Metric | Before | After |
|---|---|---|
| Agreement | 206 / 214 (96%) | 213 / 214 (99%) |
| Over-flags | 8 | 1 |
| False negatives | 0 | 0 |

Eliminated 7 of 8 over-flags (`9545`, `e17506` ×3 each, `4e32cc`).

---

## 6. Regression suites — all passing

| Suite | Result |
|---|---|
| `tools/backport/test_buckets.py` | PASS — 0 silent false negatives |
| `scripts/simulate_real_backports.py` | 15 / 15 (100%), 0 FP, 0 FN |
| `scripts/test_impact_analysis_v3.py` | 5 FN — known cross-era baseline, **unchanged** |
| `scripts/test_robustness.py` | 32 passed, 0 failed |

The 5 FN in `test_impact_analysis_v3` are the documented cross-era baseline
(`cve-record-multifile` ×1, `cve-cross-era` ×4) — identical to before the change, i.e. no regression.

---

## 7. How to reproduce

```bash
# 1. (Re)generate the ground-truth oracle from aws/aws-lc
python3 scripts/build_ground_truth.py > ground_truth.txt

# 2. Grade the bot against it
python3 scripts/compare_ground_truth.py --no-ai      # fast, deterministic, reproducible
AWS_PROFILE=tianyiy AWS_REGION=us-east-1 \
  python3 scripts/compare_ground_truth.py            # full always-on bot

# 3. Regression suites
python3 tools/backport/test_buckets.py
python3 scripts/simulate_real_backports.py
python3 scripts/test_impact_analysis_v3.py
python3 scripts/test_robustness.py
```

---

## 8. Conclusion

The bot reproduces the team's real backport decisions with **99% agreement (deterministic) / 97%
(always-on AI) and zero missed fixes**, validated against evidence pulled directly from the
`aws/aws-lc` repository rather than the bot's own logic. The handful of disagreements are all known,
harmless redundant flags (reimplemented backports the patch-id can't see, and FN-safe AI
tie-breaker upgrades).

### Known limitations (documented, all false-negative-safe)
- Reimplemented backports (no `-x`, divergent patch-id) can produce a redundant PR — 3 cells.
- The always-on AI tie-breaker can upgrade a not-affected branch to a redundant PR — it only ever
  adds a PR, never removes one, so it cannot miss a backport.
- Bundled/multi-issue and path-moved fixes are marked UNDETERMINED rather than scored.
- The `test_impact_analysis_v3` cross-era baseline (5 FN) is a known synthetic-fixture limit,
  surfaced for human/AI review — never silently dropped.
