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

    rows = []  # (scenario, branch, det, ai_verdict, truth)
    for branch in branches:
        det_affected, _ = is_branch_affected(introducers, branch)
        truth = (branch in truth_set) if truth_set is not None else None

        advisory = ai_impact_analysis(commit, branch, files, introducers)

        print(f"\n── {branch} {'─' * (60 - len(branch))}")
        if advisory is None:
            print("  AI: (no response — see stderr for the API error)")
            rows.append((commit, branch, det_affected, None, truth))
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
        rows.append((commit, branch, det_affected, ai_v, truth))

    return rows


def _classify(verdict, truth):
    """verdict is True/False/None (None = uncertain); truth is bool."""
    if verdict is None:
        return "uncertain"
    if verdict and truth:
        return "TP"
    if verdict and not truth:
        return "FP"
    if not verdict and truth:
        return "FN"
    return "TN"


def print_tally(all_rows):
    have_truth = [r for r in all_rows if r[4] is not None]
    if not have_truth:
        return
    tp = fp = fn = tn = unknown = 0
    for _scenario, _branch, _det, ai_v, truth in have_truth:
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


def print_comparison(all_rows):
    """Head-to-head: deterministic (git log -L ancestry) vs AI vs ground truth."""
    rows = [r for r in all_rows if r[4] is not None]
    if not rows:
        print("\n(no ground truth — skipping deterministic-vs-AI comparison)")
        return

    def tally(use_ai):
        c = {"TP": 0, "TN": 0, "FP": 0, "FN": 0, "uncertain": 0}
        for _scenario, _branch, det, ai, truth in rows:
            c[_classify(ai if use_ai else det, truth)] += 1
        return c

    d = tally(use_ai=False)
    a = tally(use_ai=True)
    d_dec = d["TP"] + d["TN"] + d["FP"] + d["FN"]
    a_dec = a["TP"] + a["TN"] + a["FP"] + a["FN"]
    d_acc = (d["TP"] + d["TN"]) / d_dec * 100 if d_dec else 0
    a_acc = (a["TP"] + a["TN"]) / a_dec * 100 if a_dec else 0

    print("\n" + "=" * 90)
    print("Deterministic (git log -L ancestry) vs AI advisory")
    print("=" * 90)
    print(f"  {'metric':<28}{'deterministic':>16}{'AI':>16}")
    print(f"  {'-' * 28}{'-' * 16}{'-' * 16}")
    print(f"  {'true positives':<28}{d['TP']:>16}{a['TP']:>16}")
    print(f"  {'true negatives':<28}{d['TN']:>16}{a['TN']:>16}")
    print(f"  {'false positives':<28}{d['FP']:>16}{a['FP']:>16}")
    print(f"  {'false negatives (misses)':<28}{d['FN']:>16}{a['FN']:>16}")
    print(f"  {'uncertain / no-answer':<28}{'n/a':>16}{a['uncertain']:>16}")
    print(f"  {'accuracy (of decisive)':<28}{d_acc:>15.1f}%{a_acc:>15.1f}%")

    # Where the two methods diverge.
    fn_rescued = [r for r in rows if (not r[2] and r[4]) and r[3] is True]
    fp_rescued = [r for r in rows if (r[2] and not r[4]) and r[3] is False]
    regressions = [
        r for r in rows if (r[2] == r[4]) and r[3] is not None and r[3] != r[4]
    ]
    hedged = [r for r in rows if (r[2] == r[4]) and r[3] is None]

    print()
    print("  Where the two methods differ:")
    print(
        f"    deterministic MISSED, AI caught it (false-neg rescued): {len(fn_rescued)}"
    )
    for scenario, branch, *_ in fn_rescued:
        print(f"        + {scenario} / {branch}")
    if fp_rescued:
        print(f"    deterministic over-flagged, AI cleared it: {len(fp_rescued)}")
        for scenario, branch, *_ in fp_rescued:
            print(f"        + {scenario} / {branch}")
    print(f"    deterministic correct, AI WRONG (regressions): {len(regressions)}")
    for scenario, branch, *_ in regressions:
        print(f"        ! {scenario} / {branch}")
    print(f"    deterministic correct, AI hedged (uncertain): {len(hedged)}")

    net = len(fn_rescued) + len(fp_rescued) - len(regressions)
    print()
    print(
        f"  Net effect of layering AI on the deterministic baseline: "
        f"{'+' if net >= 0 else ''}{net} corrected decision(s)."
    )
    print("    In production the AI only runs where deterministic says 'not affected',")
    print("    so its real job is exactly the 'false-neg rescued' row — turning silent")
    print("    deterministic misses into human-reviewed advisories. It never flips a")
    print("    deterministic 'affected' to 'skip'.")


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
    print_comparison(all_rows)


if __name__ == "__main__":
    main()
