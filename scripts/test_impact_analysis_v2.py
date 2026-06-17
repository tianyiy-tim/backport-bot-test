"""
Stress-test the backport bot's impact analyzer against the v2 fixture.

For each CVE scenario:
1. Run the full impact analysis (get_changed_files + find_introducing_commit)
2. For every release branch, ask is_branch_affected
3. Try a cherry-pick to see if it would apply cleanly
4. Compare bot's verdict to what we believe the GROUND TRUTH should be
5. Print a results table flagging FPs and FNs

Run from the project root: python3 scripts/test_impact_analysis_v2.py
"""

import subprocess
import sys
from pathlib import Path

# Make backport_bot importable
sys.path.insert(0, str(Path(__file__).parent))
from backport_bot import (
    find_introducing_commit,
    get_changed_files,
    is_already_patched,
    is_branch_affected,
)

# All branches we want to evaluate (NOT just AWS-LC-FIPS-* — also NetOS).
TEST_BRANCHES = [
    "AWS-LC-FIPS-2020",
    "AWS-LC-FIPS-2021",
    "AWS-LC-FIPS-2022",
    "AWS-LC-FIPS-2023",
    "AWS-LC-FIPS-2024",
    "AWS-LC-FIPS-2025",
    "NetOS",
]

# Ground truth: which branches we BELIEVE are actually vulnerable to each CVE.
# These are based on what files exist on each branch and whether the bug is
# present, regardless of whether the fix can be cleanly cherry-picked.
GROUND_TRUTH = {
    "cve-buffer": {
        # Touches utils/buffer.c, which exists since the very first commit.
        # Every branch has copy_buffer/buffer_length without null guards.
        "AWS-LC-FIPS-2020",
        "AWS-LC-FIPS-2021",
        "AWS-LC-FIPS-2022",
        "AWS-LC-FIPS-2023",
        "AWS-LC-FIPS-2024",
        "AWS-LC-FIPS-2025",
        "NetOS",
    },
    "cve-handshake-original": {
        # Adds bounds check to process_handshake (originally in crypto.c).
        # 2021+ have crypto.c. 2025 already has the same fix cherry-picked
        # (so still "needs the fix" in name, but applying it would be a noop/conflict).
        # 2020 doesn't have crypto.c → not affected.
        "AWS-LC-FIPS-2021",
        "AWS-LC-FIPS-2022",
        "AWS-LC-FIPS-2023",
        "AWS-LC-FIPS-2024",
        "NetOS",
        # Note: 2025 already has the fix, so it's "not vulnerable" in practice.
    },
    "cve-handshake-postrefactor": {
        # Touches crypto/handshake.c (post-refactor location). The underlying
        # vulnerable function exists on 2021+ (in crypto.c). Bot needs to
        # walk through the rename to find the introducer.
        "AWS-LC-FIPS-2021",
        "AWS-LC-FIPS-2022",
        "AWS-LC-FIPS-2023",
        "AWS-LC-FIPS-2024",
        "AWS-LC-FIPS-2025",
        "NetOS",
    },
    "cve-record-multifile": {
        # Touches tls/record.c + tls/cert.c (both post-rename).
        # tls.c (record): exists on 2022+
        # cert.c: exists on 2023+
        # Branches affected by AT LEAST ONE: 2022+ (have tls.c at minimum).
        "AWS-LC-FIPS-2022",
        "AWS-LC-FIPS-2023",
        "AWS-LC-FIPS-2024",
        "AWS-LC-FIPS-2025",
        "NetOS",
    },
}


def cherry_pick_dry_run(commit, branch):
    """
    Try applying the patch from `commit` against `branch` without committing.
    Returns "clean", "conflict", or "noop" (already applied).
    """
    # Get the patch
    patch = subprocess.run(
        ["git", "format-patch", "-1", "--stdout", commit],
        capture_output=True,
        text=True,
    )
    if patch.returncode != 0:
        return f"patch-fail: {patch.stderr.strip()}"

    # Try applying it to the branch's tree
    check = subprocess.run(
        ["git", "apply", "--check", "--3way"],
        input=patch.stdout,
        capture_output=True,
        text=True,
        env={**__import__("os").environ},
    )
    # Note: --check on --3way isn't perfect; this is just a smoke test.
    # We're calling it from the current branch state, not the target branch's
    # state, so it's only suggestive. A real cherry-pick test would require
    # checking out the branch.
    if check.returncode == 0:
        return "would-apply"
    return "would-conflict"


def run_scenario(tag):
    """Run impact analysis for one CVE tag and return a dict of results."""
    files = get_changed_files(tag)
    introducers = find_introducing_commit(tag, files)

    bot_verdict = {}
    bot_reason = {}
    for branch in TEST_BRANCHES:
        affected, _ = is_branch_affected(introducers, branch)
        if not affected:
            bot_verdict[branch] = False
            bot_reason[branch] = "not_affected"
            continue
        # Branch contains the introducing commit — but check if the fix is
        # already applied via patch-id deduplication.
        if is_already_patched(tag, branch):
            bot_verdict[branch] = False
            bot_reason[branch] = "already_patched"
        else:
            bot_verdict[branch] = True
            bot_reason[branch] = "needs_backport"

    return {
        "files": files,
        "introducers": introducers,
        "verdict": bot_verdict,
        "reason": bot_reason,
    }


def classify(bot_says_affected, ground_truth_affected):
    """Compare bot output to ground truth, return label."""
    if bot_says_affected and ground_truth_affected:
        return "TP"  # true positive
    if bot_says_affected and not ground_truth_affected:
        return "FP"  # false positive
    if not bot_says_affected and ground_truth_affected:
        return "FN"  # false negative — DANGEROUS
    return "TN"  # true negative


def main():
    summary = {"TP": 0, "FP": 0, "FN": 0, "TN": 0}

    for tag, truth in GROUND_TRUTH.items():
        print(f"\n{'=' * 72}")
        print(f"Scenario: {tag}")
        print(f"{'=' * 72}")

        result = run_scenario(tag)

        print(f"  Changed files:  {result['files']}")
        print(f"  Introducers:    {sorted(s[:8] for s in result['introducers'])}")
        print()
        print(f"  {'branch':<22} {'bot':<10} {'reason':<18} {'truth':<10} {'verdict'}")
        print(f"  {'-' * 22} {'-' * 10} {'-' * 18} {'-' * 10} {'-' * 8}")

        for branch in TEST_BRANCHES:
            bot_affected = result["verdict"][branch]
            reason = result["reason"][branch]
            truth_affected = branch in truth
            label = classify(bot_affected, truth_affected)
            summary[label] += 1
            marker = "" if label in ("TP", "TN") else f"   \u2190 {label}!"
            print(
                f"  {branch:<22} {str(bot_affected):<10} {reason:<18} "
                f"{str(truth_affected):<10} {label}{marker}"
            )

    # Final tally
    total = sum(summary.values())
    print(f"\n{'=' * 72}")
    print("Summary")
    print(f"{'=' * 72}")
    print(f"  True positives  (bot=YES, truth=YES):  {summary['TP']}")
    print(f"  True negatives  (bot=NO,  truth=NO):   {summary['TN']}")
    print(f"  False positives (bot=YES, truth=NO):   {summary['FP']}")
    print(
        f"  False negatives (bot=NO,  truth=YES):  {summary['FN']}  "
        f"{'<-- the dangerous ones' if summary['FN'] else ''}"
    )
    print()
    accuracy = (summary["TP"] + summary["TN"]) / total * 100
    print(f"  Accuracy: {accuracy:.1f}% ({summary['TP'] + summary['TN']}/{total})")
    print()
    if summary["FN"] > 0:
        print(
            f"  ** {summary['FN']} false negatives means the bot would silently "
            f"skip {summary['FN']} vulnerable branch(es). **"
        )


if __name__ == "__main__":
    main()
