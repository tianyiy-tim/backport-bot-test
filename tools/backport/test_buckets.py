"""
False-negative safety test for the CLI's bucketing.

The deterministic engine answers affected / not affected. The CLI splits the
"not affected" answer into two buckets:

  not affected  - none of the changed files exist on the branch (confident skip)
  UNSURE        - ancestry/patch-id did not match, but the file IS present

The whole point of UNSURE is to catch false negatives: a branch that is really
still vulnerable has the file present, so it must surface as UNSURE (sent to the
human / AI), never as a confident "not affected".

THE SAFETY PROPERTY UNDER TEST:
    For every (scenario x branch) that ground truth says is affected, the
    deterministic bucket must be AFFECTED or UNSURE -- NEVER "not affected".
    A truly-affected branch landing in "not affected" is a SILENT false
    negative (the dangerous failure). This test fails loudly if any exist.

It reuses the real CLI bucketing (backport_cli.bucket_branches) and the same
ground truth and fixture as scripts/test_impact_analysis_v3.py. Deterministic
only; no AI, no credentials.

Run from anywhere:  python3 tools/backport/test_buckets.py
"""

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_REPO / "scripts"))

import backport_cli as cli  # noqa: E402
from test_impact_analysis_v3 import GROUND_TRUTH, TEST_BRANCHES  # noqa: E402

# The engine runs git relative to cwd, and uses repo-root-relative paths.
os.chdir(_REPO)


def main():
    rows = []
    silent_fn = []  # truly affected, but bucketed "not affected" -> DANGEROUS
    surfaced_fn = []  # truly affected, deterministic missed, but bucketed UNSURE
    counts = {cli.AFFECTED: 0, cli.UNSURE: 0, cli.NOT_AFFECTED: 0, cli.ALREADY: 0}

    for tag, truth in GROUND_TRUTH.items():
        _files, _introducers, buckets = cli.bucket_branches(tag, TEST_BRANCHES)
        for branch in TEST_BRANCHES:
            state = buckets[branch]
            counts[state] += 1
            truly_affected = branch in truth

            # Deterministic "decisive yes" = AFFECTED. ALREADY counts as handled
            # (fix already present), so treat it as a correct non-backport.
            det_says_backport = state == cli.AFFECTED
            if truly_affected and not det_says_backport:
                if state == cli.NOT_AFFECTED:
                    silent_fn.append((tag, branch))
                elif state == cli.UNSURE:
                    surfaced_fn.append((tag, branch))
            rows.append((tag, branch, state, truly_affected))

    # ---- report ----
    print("=" * 84)
    print("Bucketing vs. ground truth (deterministic only)")
    print("=" * 84)
    print(f"  {'scenario':<28} {'branch':<20} {'bucket':<14} truth")
    print(f"  {'-' * 28} {'-' * 20} {'-' * 14} {'-' * 8}")
    for tag, branch, state, truth in rows:
        flag = ""
        if truth and state == cli.NOT_AFFECTED:
            flag = "  <-- SILENT FALSE NEGATIVE"
        elif truth and state == cli.UNSURE:
            flag = "  (miss, but surfaced as UNSURE)"
        print(
            f"  {tag:<28} {branch:<20} {cli._LABEL[state]:<14} "
            f"{'affected' if truth else 'not affected'}{flag}"
        )

    print()
    print("Bucket totals:", {cli._LABEL[k]: v for k, v in counts.items()})
    print(
        f"Truly-affected branches the deterministic check MISSED: "
        f"{len(silent_fn) + len(surfaced_fn)}"
    )
    print(f"  - surfaced as UNSURE (caught, sent to human/AI): {len(surfaced_fn)}")
    for tag, b in surfaced_fn:
        print(f"      + {tag} / {b}")
    print(f"  - SILENT in 'not affected' (dangerous): {len(silent_fn)}")
    for tag, b in silent_fn:
        print(f"      ! {tag} / {b}")

    print()
    print("=" * 84)
    if silent_fn:
        print(
            f"FAIL: {len(silent_fn)} truly-affected branch(es) were bucketed "
            f"'not affected' and would be silently dropped."
        )
        return 1
    print(
        "PASS: every truly-affected branch the deterministic check could not "
        "confirm landed in UNSURE, not in 'not affected'. No silent false "
        "negatives: every miss is surfaced for human/AI review."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
