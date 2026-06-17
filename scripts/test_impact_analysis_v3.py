"""
Comprehensive stress-test for the backport bot.

Extends test_impact_analysis_v2.py with:
1. Cherry-pick mechanics testing — for each (CVE × branch) where the bot
   says "needs backport", actually attempt a cherry-pick to a temp branch.
   Records: clean / conflict / empty / error.
2. Three new edge-case CVE scenarios (pure modification, pure deletion,
   cross-era multi-file).
3. Per-phase timing.
4. Markdown summary report.

Run from project root: python3 scripts/test_impact_analysis_v3.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from backport_bot import (
    find_introducing_commit,
    get_changed_files,
    is_already_patched,
    is_branch_affected,
)

TEST_BRANCHES = [
    "AWS-LC-FIPS-2020",
    "AWS-LC-FIPS-2021",
    "AWS-LC-FIPS-2022",
    "AWS-LC-FIPS-2023",
    "AWS-LC-FIPS-2024",
    "AWS-LC-FIPS-2025",
    "NetOS",
]


# Ground truth: which branches genuinely need the patch.
# "Needs the patch" = the underlying buggy code is still present and unfixed.
# This is what an experienced human reviewer would say after reading each branch.
GROUND_TRUTH = {
    # --- v2 scenarios ---
    "cve-buffer": {
        # Touches utils/buffer.c (oldest code). Every branch has it unguarded.
        *TEST_BRANCHES,
    },
    "cve-handshake-original": {
        # Bounds check on process_handshake. FIPS-2020 lacks crypto.c.
        # FIPS-2025 already has the same fix cherry-picked.
        "AWS-LC-FIPS-2021",
        "AWS-LC-FIPS-2022",
        "AWS-LC-FIPS-2023",
        "AWS-LC-FIPS-2021",
        "AWS-LC-FIPS-2022",
        "AWS-LC-FIPS-2023",
        "AWS-LC-FIPS-2024",
        "NetOS",
    },
    "cve-handshake-postrefactor": {
        # Null check on (post-rename) handshake. 2021+ have buggy form.
        "AWS-LC-FIPS-2021",
        "AWS-LC-FIPS-2022",
        "AWS-LC-FIPS-2023",
        "AWS-LC-FIPS-2024",
        "AWS-LC-FIPS-2025",
        "NetOS",
    },
    "cve-record-multifile": {
        # tls/record.c since 2022, tls/cert.c since 2023.
        "AWS-LC-FIPS-2022",
        "AWS-LC-FIPS-2023",
        "AWS-LC-FIPS-2024",
        "AWS-LC-FIPS-2025",
        "NetOS",
    },
    # --- v3 extensions ---
    "cve-pure-modification": {
        # Constant-time hash_compare. crypto/digest.c since 2024.
        "AWS-LC-FIPS-2024",
        "AWS-LC-FIPS-2025",
        "NetOS",
    },
    "cve-pure-deletion": {
        # Remove vulnerable verify_signature. crypto.c since 2021.
        "AWS-LC-FIPS-2021",
        "AWS-LC-FIPS-2022",
        "AWS-LC-FIPS-2023",
        "AWS-LC-FIPS-2024",
        "AWS-LC-FIPS-2025",
        "NetOS",
    },
    "cve-cross-era": {
        # Multi-file: utils/buffer.c (universal) + crypto/digest.c (2024+).
        # Universal because every branch has buggy buffer.c.
        *TEST_BRANCHES,
    },
}


def simulate_cherry_pick(commit, branch):
    """
    Attempt a cherry-pick of `commit` onto `origin/<branch>` and report the
    outcome without pushing. Returns (status, detail) where status is one of:
    'clean', 'conflict', 'empty', 'error'.

    Runs entirely inside a throwaway `git worktree` (detached at origin/<branch>),
    so it never checks out, stashes, or otherwise disturbs the caller's working
    tree. This makes the simulation safe to run with uncommitted local changes.
    """
    parent = tempfile.mkdtemp(prefix="v3-cp-")
    worktree = os.path.join(parent, "wt")

    try:
        add = subprocess.run(
            ["git", "worktree", "add", "--detach", worktree, f"origin/{branch}"],
            capture_output=True,
            text=True,
        )
        if add.returncode != 0:
            return ("error", f"worktree add failed: {add.stderr.strip()}")

        pick = subprocess.run(
            ["git", "-C", worktree, "cherry-pick", commit],
            capture_output=True,
            text=True,
        )
        if pick.returncode == 0:
            return ("clean", None)

        # Non-zero: distinguish an empty (already-applied) pick from a conflict.
        combined = (pick.stdout + pick.stderr).lower()
        if "empty" in combined or "nothing to commit" in combined:
            detail = (pick.stdout + pick.stderr).strip().splitlines()
            return ("empty", detail[0] if detail else "empty")
        detail = (pick.stdout + pick.stderr).strip().splitlines()
        return ("conflict", detail[0] if detail else "conflict")

    finally:
        # Abort any in-progress pick, then tear the worktree down completely.
        subprocess.run(
            ["git", "-C", worktree, "cherry-pick", "--abort"],
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "worktree", "remove", "--force", worktree],
            capture_output=True,
            text=True,
        )
        shutil.rmtree(parent, ignore_errors=True)
        # Drop any administrative leftovers (safe no-op if already clean).
        subprocess.run(
            ["git", "worktree", "prune"],
            capture_output=True,
            text=True,
        )


def classify(bot_says_affected, ground_truth_affected):
    if bot_says_affected and ground_truth_affected:
        return "TP"
    if bot_says_affected and not ground_truth_affected:
        return "FP"
    if not bot_says_affected and ground_truth_affected:
        return "FN"
    return "TN"


def run_scenario(tag):
    """Full pipeline + cherry-pick simulation. Returns timed results."""
    times = {}

    t0 = time.perf_counter()
    files = get_changed_files(tag)
    times["get_changed_files"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    introducers = find_introducing_commit(tag, files)
    times["find_introducing_commit"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    branch_data = {}
    for branch in TEST_BRANCHES:
        affected, _ = is_branch_affected(introducers, branch)
        if not affected:
            branch_data[branch] = {
                "verdict": False,
                "reason": "not_affected",
                "cherry_pick": None,
            }
            continue
        if is_already_patched(tag, branch):
            branch_data[branch] = {
                "verdict": False,
                "reason": "already_patched",
                "cherry_pick": None,
            }
            continue
        branch_data[branch] = {
            "verdict": True,
            "reason": "needs_backport",
            "cherry_pick": None,  # filled in below
        }
    times["impact_per_branch"] = time.perf_counter() - t0

    # Cherry-pick simulation only for branches the bot would actually backport to
    t0 = time.perf_counter()
    for branch, data in branch_data.items():
        if data["reason"] == "needs_backport":
            status, detail = simulate_cherry_pick(tag, branch)
            data["cherry_pick"] = {"status": status, "detail": detail}
    times["cherry_pick_simulation"] = time.perf_counter() - t0

    return {
        "files": files,
        "introducers": introducers,
        "branches": branch_data,
        "times": times,
    }


def main():
    # Contract guard: is_branch_affected must return (affected, advisory).
    # If this ever drifts back to a bare bool, the `affected, _ = ...` call
    # sites below would silently treat every branch as affected.
    _probe = is_branch_affected(set(), TEST_BRANCHES[0])
    assert isinstance(_probe, tuple) and len(_probe) == 2, (
        "is_branch_affected must return a 2-tuple (affected, advisory); "
        f"got {_probe!r}. Update the call sites if the contract changed."
    )

    summary = {"TP": 0, "FP": 0, "FN": 0, "TN": 0}
    cp_summary = {"clean": 0, "conflict": 0, "empty": 0, "error": 0}
    total_time = 0.0
    scenario_results = []

    for tag, truth in GROUND_TRUTH.items():
        print(f"\n{'=' * 88}")
        print(f"Scenario: {tag}")
        print(f"{'=' * 88}")

        result = run_scenario(tag)
        scenario_results.append((tag, truth, result))
        total_time += sum(result["times"].values())

        print(f"  Changed files:  {result['files']}")
        print(f"  Introducers:    {sorted(s[:8] for s in result['introducers'])}")
        print(
            f"  Timing (s):     "
            f"diff={result['times']['get_changed_files']:.2f} "
            f"blame={result['times']['find_introducing_commit']:.2f} "
            f"impact={result['times']['impact_per_branch']:.2f} "
            f"cherry={result['times']['cherry_pick_simulation']:.2f}"
        )
        print()
        print(
            f"  {'branch':<22} {'bot':<10} {'reason':<18} {'cherry-pick':<12} "
            f"{'truth':<10} {'verdict'}"
        )
        print(f"  {'-' * 22} {'-' * 10} {'-' * 18} {'-' * 12} {'-' * 10} {'-' * 8}")

        for branch in TEST_BRANCHES:
            data = result["branches"][branch]
            bot_affected = data["verdict"]
            truth_affected = branch in truth
            label = classify(bot_affected, truth_affected)
            summary[label] += 1

            cp_status = ""
            if data["cherry_pick"]:
                cp_status = data["cherry_pick"]["status"]
                cp_summary[cp_status] = cp_summary.get(cp_status, 0) + 1

            marker = "" if label in ("TP", "TN") else f"   \u2190 {label}!"
            print(
                f"  {branch:<22} {str(bot_affected):<10} {data['reason']:<18} "
                f"{cp_status:<12} {str(truth_affected):<10} {label}{marker}"
            )

    # ===== Summary =====
    total = sum(summary.values())
    accuracy = (summary["TP"] + summary["TN"]) / total * 100 if total else 0

    print(f"\n{'=' * 88}")
    print("Overall summary")
    print(f"{'=' * 88}")
    print(f"  Scenarios: {len(GROUND_TRUTH)}")
    print(f"  (branch x CVE) decisions: {total}")
    print()
    print(f"  Impact-analysis accuracy:")
    print(f"    True positives:   {summary['TP']:3d}")
    print(f"    True negatives:   {summary['TN']:3d}")
    print(
        f"    False positives:  {summary['FP']:3d}  "
        f"{'(unnecessary backport PRs)' if summary['FP'] else ''}"
    )
    print(
        f"    False negatives:  {summary['FN']:3d}  "
        f"{'<-- DANGEROUS, missed backports' if summary['FN'] else ''}"
    )
    print(f"    Accuracy:         {accuracy:.1f}%")
    print()
    print(f'  Cherry-pick outcomes (only attempted on "needs_backport"):')
    print(f"    Clean apply:      {cp_summary['clean']:3d}")
    print(f"    Conflict:         {cp_summary['conflict']:3d}")
    print(f"    Empty (no-op):    {cp_summary['empty']:3d}")
    print(f"    Error:            {cp_summary['error']:3d}")
    print()
    print(
        f"  Total runtime: {total_time:.2f}s "
        f"({total_time / len(GROUND_TRUTH):.2f}s avg per scenario)"
    )

    # ===== Per-scenario breakdown =====
    print()
    print(f"{'=' * 88}")
    print("Per-scenario breakdown")
    print(f"{'=' * 88}")
    print(
        f"  {'scenario':<32} {'TP':<4} {'TN':<4} {'FP':<4} {'FN':<4} "
        f"{'clean':<6} {'conflict':<9} {'empty':<6}"
    )
    print(
        f"  {'-' * 32} {'-' * 4} {'-' * 4} {'-' * 4} {'-' * 4} "
        f"{'-' * 6} {'-' * 9} {'-' * 6}"
    )
    for tag, truth, result in scenario_results:
        s = {"TP": 0, "TN": 0, "FP": 0, "FN": 0}
        c = {"clean": 0, "conflict": 0, "empty": 0, "error": 0}
        for branch in TEST_BRANCHES:
            data = result["branches"][branch]
            label = classify(data["verdict"], branch in truth)
            s[label] += 1
            if data["cherry_pick"]:
                c[data["cherry_pick"]["status"]] += 1
        print(
            f"  {tag:<32} {s['TP']:<4} {s['TN']:<4} {s['FP']:<4} {s['FN']:<4} "
            f"{c['clean']:<6} {c['conflict']:<9} {c['empty']:<6}"
        )


if __name__ == "__main__":
    main()
