"""
Robustness and determinism tests for the deterministic impact analysis.

Complements the accuracy suites (test_impact_analysis_v2/v3.py) with checks that
don't fit "did it get the right answer", namely:
  - determinism: identical inputs produce identical outputs across repeated runs
  - edge cases: bad refs, empty introducers, empty changed-file sets
  - the (bool, advisory) contract of is_branch_affected
  - file-existence short-circuit behavior
  - the affected_branches CLI: valid --json, and non-zero exit on a bad ref

No AI / no AWS credentials required (everything here is deterministic git).

Run:  python3 scripts/test_robustness.py
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from backport_bot import (  # noqa: E402
    _get_file_on_branch,
    find_introducing_commit,
    get_changed_files,
    get_supported_branches,
    is_branch_affected,
)
from test_impact_analysis_v3 import GROUND_TRUTH, TEST_BRANCHES  # noqa: E402

passed = 0
failed = []


def check(name, ok, detail=""):
    global passed
    if ok:
        passed += 1
        print(f"  [ok] {name}")
    else:
        failed.append((name, detail))
        print(f"  [XX] {name}  ({detail})")


# ---------------------------------------------------------------------------
# 1. Determinism: same inputs -> same outputs, run twice.
# ---------------------------------------------------------------------------
print("Determinism (run impact analysis twice, expect identical results):")
for tag in GROUND_TRUTH:
    files1 = get_changed_files(tag)
    intro1 = find_introducing_commit(tag, files1)
    files2 = get_changed_files(tag)
    intro2 = find_introducing_commit(tag, files2)
    check(f"{tag}: changed files stable", files1 == files2, f"{files1} vs {files2}")
    check(f"{tag}: introducers stable", intro1 == intro2)
    v1 = {b: is_branch_affected(intro1, b)[0] for b in TEST_BRANCHES}
    v2 = {b: is_branch_affected(intro2, b)[0] for b in TEST_BRANCHES}
    check(f"{tag}: per-branch verdicts stable", v1 == v2, f"{v1} vs {v2}")

# ---------------------------------------------------------------------------
# 2. Edge cases.
# ---------------------------------------------------------------------------
print("\nEdge cases:")
try:
    get_changed_files("definitely-not-a-real-ref-xyz")
    check("nonexistent ref raises", False, "no error raised")
except Exception:
    check("nonexistent ref raises", True)

aff, adv = is_branch_affected(set(), TEST_BRANCHES[0])
check("empty introducers -> not affected, no advisory", aff is False and adv is None)

probe = is_branch_affected(set(), TEST_BRANCHES[0])
check(
    "is_branch_affected returns a 2-tuple",
    isinstance(probe, tuple) and len(probe) == 2,
    repr(probe),
)

# find_introducing_commit on an empty file list must not crash.
check(
    "find_introducing_commit([]) -> empty set",
    find_introducing_commit("cve-buffer", []) == set(),
)

# ---------------------------------------------------------------------------
# 3. File-existence resolution (rename-aware) is consistent with reality.
# ---------------------------------------------------------------------------
print("\nFile-existence / rename resolution:")
# crypto/digest.c was introduced in the 2024 era -> absent on 2020, present on 2024.
absent, _ = _get_file_on_branch(
    "crypto/digest.c", "origin/AWS-LC-FIPS-2020", commit="cve-pure-modification"
)
present, resolved = _get_file_on_branch(
    "crypto/digest.c", "origin/AWS-LC-FIPS-2024", commit="cve-pure-modification"
)
check("digest.c absent on FIPS-2020", absent is None)
check("digest.c present on FIPS-2024", present is not None)
# crypto/handshake.c resolves to its pre-rename path crypto.c on an older branch.
content, rpath = _get_file_on_branch(
    "crypto/handshake.c", "origin/AWS-LC-FIPS-2021", commit="cve-handshake-postrefactor"
)
check(
    "crypto/handshake.c resolves to crypto.c on FIPS-2021",
    content is not None and rpath == "crypto.c",
    str(rpath),
)

# ---------------------------------------------------------------------------
# 4. Supported-branch resolution.
# ---------------------------------------------------------------------------
print("\nBranch resolution:")
branches = get_supported_branches()
check("supported branches resolved", len(branches) >= 1, str(branches))
check(
    "expected fixture branches present",
    "NetOS" in branches and any("AWS-LC-FIPS-" in b for b in branches),
    str(branches),
)

# ---------------------------------------------------------------------------
# 5. affected_branches CLI robustness.
# ---------------------------------------------------------------------------
print("\nCLI (affected_branches.py):")
r = subprocess.run(
    [sys.executable, "scripts/affected_branches.py", "cve-buffer", "--no-ai", "--json"],
    capture_output=True,
    text=True,
)
try:
    data = json.loads(r.stdout)
    ok = "branches" in data and "introducers" in data and len(data["branches"]) >= 1
    check("--json emits valid, structured JSON", ok, r.stdout[:200])
except Exception as e:
    check("--json emits valid, structured JSON", False, str(e))

r2 = subprocess.run(
    [sys.executable, "scripts/affected_branches.py", "no-such-ref-xyz", "--no-ai"],
    capture_output=True,
    text=True,
)
check("bad ref exits non-zero", r2.returncode != 0, f"rc={r2.returncode}")

# ---------------------------------------------------------------------------
print("\n" + "=" * 52)
print(f"{passed} passed, {len(failed)} failed")
if failed:
    for n, d in failed:
        print(f"  FAIL: {n}  {d}")
    sys.exit(1)
print("All robustness checks passed.")
