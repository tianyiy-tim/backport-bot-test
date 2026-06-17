"""
Local visualizer for the bot's AI impact analysis.

Runs the AI advisory (`ai_impact_analysis`) against the synthetic fixture
branches for one or more CVE-fix scenarios, and prints — per branch — the
deterministic verdict, the AI's verdict + confidence, the AI's full reasoning,
and the human ground truth, so you can eyeball how well the model analyzes
impact. Ends with an AI-vs-truth accuracy tally.

This talks to real Bedrock, so it needs local AWS access:

    # 1. authenticate AWS in your shell (Bedrock-Access role is enough):
    aws sts get-caller-identity            # must print your account, not an error
    # 2. point at the region with your model access (+ optional model override):
    export AWS_REGION=us-east-2
    export BEDROCK_MODEL_ID=us.anthropic.claude-opus-4-8   # optional
    # 3. install the SDK once:
    pip install "anthropic[bedrock]"

Usage:
    python scripts/visualize_ai_impact.py                      # default scenario, all branches
    python scripts/visualize_ai_impact.py cve-cross-era        # one named scenario
    python scripts/visualize_ai_impact.py --all                # every scenario (more API calls)
    python scripts/visualize_ai_impact.py cve-buffer --branches AWS-LC-FIPS-2020 NetOS
    python scripts/visualize_ai_impact.py --commit <git-ref>   # any fix commit (no ground truth)

Note: this runs the AI on EVERY selected branch (even ones the deterministic
checks already resolve) so you can see the model reason in every case. That's
more model calls than the production bot makes — narrow with --branches if you
want to keep it cheap.
"""

import argparse
import os
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from backport_bot import (  # noqa: E402
    _BEDROCK_MODEL_ID,
    _ai_client,
    ai_impact_analysis,
    find_introducing_commit,
    get_changed_files,
    is_branch_affected,
)
from test_impact_analysis_v3 import GROUND_TRUTH, TEST_BRANCHES  # noqa: E402


def preflight():
    """Make sure we can actually reach Bedrock; explain clearly if not."""
    client = _ai_client()
    if client is not None:
        print(
            f"AI client ready. model={_BEDROCK_MODEL_ID} "
            f"region={os.environ.get('AWS_REGION', 'us-east-1')}\n"
        )
        return
    print("Cannot reach Bedrock — the AI client did not initialize.\n")
    try:
        import anthropic  # noqa: F401
    except ImportError:
        print('  - anthropic SDK not installed. Run: pip install "anthropic[bedrock]"')
    if not os.environ.get("AWS_ACCESS_KEY_ID"):
        print(
            "  - No AWS credentials in this shell. Authenticate, e.g. `ada credentials"
        )
        print(
            "    update ... --once` or export temp creds, then `aws sts get-caller-identity`."
        )
    print("\nFix the above and re-run.")
    sys.exit(1)


def indent(text, prefix="      "):
    return textwrap.indent(text.strip(), prefix)


def verdict_word(v):
    return {True: "affected", False: "NOT affected", None: "uncertain"}[v]


def analyze(commit, branches, truth_set):
    files = get_changed_files(commit)
    introducers = find_introducing_commit(commit, files)
    short_intro = sorted(s[:8] for s in introducers)

    print("=" * 90)
    print(f"Scenario: {commit}")
    print(f"  changed files: {files}")
    print(f"  introducer(s): {short_intro}")
    print("=" * 90)

    rows = []  # (branch, det, ai_verdict, truth)
    for branch in branches:
        det_affected, _ = is_branch_affected(introducers, branch)
        truth = (branch in truth_set) if truth_set is not None else None

        advisory = ai_impact_analysis(commit, branch, files, introducers)

        print(f"\n── {branch} {'─' * (60 - len(branch))}")
        if advisory is None:
            print("  AI: (no response — see stderr for the API error)")
            rows.append((branch, det_affected, None, truth))
            continue

        ai_v = advisory["likely_affected"]
        conf = advisory["confidence"]
        truth_str = "" if truth is None else f" | ground truth: {verdict_word(truth)}"
        print(
            f"  deterministic: {verdict_word(det_affected)}"
            f" | AI: {verdict_word(ai_v)} (confidence: {conf}){truth_str}"
        )
        agree = ""
        if truth is not None:
            agree = "  ✓ matches truth" if ai_v == truth else "  ✗ DIFFERS from truth"
        print(f"  AI reasoning:{agree}")
        print(indent(advisory["reasoning"]))
        rows.append((branch, det_affected, ai_v, truth))

    return rows


def print_tally(all_rows):
    have_truth = [r for r in all_rows if r[3] is not None]
    if not have_truth:
        return
    tp = fp = fn = tn = unknown = 0
    for _, _, ai_v, truth in have_truth:
        if ai_v is None:
            unknown += 1
            continue
        if ai_v and truth:
            tp += 1
        elif ai_v and not truth:
            fp += 1
        elif not ai_v and truth:
            fn += 1
        else:
            tn += 1
    scored = tp + fp + fn + tn
    acc = (tp + tn) / scored * 100 if scored else 0
    print("\n" + "=" * 90)
    print("AI vs. ground truth")
    print("=" * 90)
    print(f"  true positives:  {tp}")
    print(f"  true negatives:  {tn}")
    print(f"  false positives: {fp}   (AI over-flags — extra review, not dangerous)")
    print(f"  false negatives: {fn}   (AI under-flags — would MISS a backport)")
    print(f"  uncertain/no-answer: {unknown}")
    print(f"  accuracy (excl. uncertain): {acc:.1f}%")
    print("\n  Reminder: in the real bot the AI is advisory only — it never flips a")
    print(
        "  deterministic 'affected' to 'skip'. This view is purely to judge its quality."
    )


def main():
    ap = argparse.ArgumentParser(
        description="Visualize the bot's AI impact analysis locally."
    )
    ap.add_argument(
        "scenario",
        nargs="?",
        default="cve-cross-era",
        help="a ground-truth scenario tag (default: cve-cross-era), or with --commit, ignored",
    )
    ap.add_argument(
        "--all", action="store_true", help="run every ground-truth scenario"
    )
    ap.add_argument(
        "--commit", help="analyze an arbitrary git ref instead (no ground truth)"
    )
    ap.add_argument("--branches", nargs="+", help="limit to these branches")
    args = ap.parse_args()

    preflight()

    branches = args.branches or TEST_BRANCHES

    all_rows = []
    if args.commit:
        all_rows += analyze(args.commit, branches, truth_set=None)
    elif args.all:
        for tag, truth in GROUND_TRUTH.items():
            all_rows += analyze(tag, branches, truth_set=truth)
    else:
        if args.scenario not in GROUND_TRUTH:
            print(
                f"Unknown scenario '{args.scenario}'. Known: {', '.join(GROUND_TRUTH)}\n"
                f"(or pass --commit <ref> to analyze an arbitrary commit)"
            )
            sys.exit(2)
        all_rows += analyze(
            args.scenario, branches, truth_set=GROUND_TRUTH[args.scenario]
        )

    print_tally(all_rows)


if __name__ == "__main__":
    main()
