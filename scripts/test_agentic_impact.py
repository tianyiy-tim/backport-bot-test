"""
Demo: agentic impact analysis as a second opinion.

Runs the deterministic analyzer first, then for every branch it marks
"not affected" runs the agentic evaluator to see whether it would catch a
false negative the ancestry check missed.

This targets the cve-cross-era scenario, where the deterministic pass produced
false negatives on the older FIPS branches (the fix patched lines an earlier fix
had added, so ancestry pointed only at main).

Run from project root: python3 scripts/test_agentic_impact.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from agentic_impact import evaluate_branch
from backport_bot import (
    find_introducing_commit,
    get_changed_files,
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

# The fix to analyze, plus hints the mock model uses (a real model would infer
# these from the fix diff). cve-cross-era hardens copy_buffer in utils/buffer.c.
COMMIT = "cve-cross-era"
FILE = "utils/buffer.c"
FUNCTION = "copy_buffer"
FIX_GUARD = "dst == NULL"  # the guard the fix adds; absence ⇒ still vulnerable


def main():
    files = get_changed_files(COMMIT)
    introducers = find_introducing_commit(COMMIT, files)

    print(f"Fix under analysis: {COMMIT}")
    print(f"Files changed:      {files}")
    print(f"Deterministic introducers: {sorted(s[:8] for s in introducers)}")
    print()
    print(f"{'branch':<20} {'deterministic':<15} {'agent':<28} {'final'}")
    print(f"{'-' * 20} {'-' * 15} {'-' * 28} {'-' * 8}")

    for branch in TEST_BRANCHES:
        det_affected, _ = is_branch_affected(introducers, branch)

        if det_affected:
            # Deterministic already flagged it; no need to ask the agent.
            print(f"{branch:<20} {'AFFECTED':<15} {'(skipped)':<28} AFFECTED")
            continue

        # Deterministic said "not affected" — ask the agent for a second opinion.
        verdict = evaluate_branch(
            COMMIT, branch, FILE, function=FUNCTION, fix_guard=FIX_GUARD
        )
        v = verdict["verdict"]
        agent_summary = f"{v} ({verdict['confidence']}, {verdict['steps']} steps)"

        if v == "affected":
            final = "AFFECTED (agent caught FN)"
        elif v == "uncertain":
            final = "REVIEW (agent unsure)"
        else:
            final = "not affected"

        print(f"{branch:<20} {'not affected':<15} {agent_summary:<28} {final}")

    print()
    print("Note: the agent only runs on branches the deterministic pass cleared,")
    print("and can only escalate them toward human review. It never overrides an")
    print("AFFECTED finding. The model call is mocked; see agentic_impact.py.")


if __name__ == "__main__":
    main()
