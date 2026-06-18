"""
Given a commit, print which supported branches it affects.

This is a thin CLI over the bot's impact analysis (backport_bot.is_branch_affected):
deterministic ancestry + patch-id + file-existence, with the AI advisory as a
fallback on the inconclusive branches. The AI step only runs if AWS Bedrock
credentials are available; otherwise it is skipped automatically and you get the
deterministic answer.

It does NOT cherry-pick or open PRs. It only answers "which branches are affected?".

Usage:
    python3 scripts/affected_branches.py <commit>
    python3 scripts/affected_branches.py <commit> --branches AWS-LC-FIPS-2021 NetOS
    python3 scripts/affected_branches.py <commit> --no-ai      # deterministic only
    python3 scripts/affected_branches.py <commit> --json
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import backport_bot as bot  # noqa: E402


def commit_subject(commit):
    r = subprocess.run(
        ["git", "log", "-1", "--format=%s", commit], capture_output=True, text=True
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def main():
    ap = argparse.ArgumentParser(
        description="Show which supported branches a commit affects."
    )
    ap.add_argument("commit", help="commit SHA or ref (the fix on mainline)")
    ap.add_argument(
        "--branches",
        nargs="+",
        help="limit to these branches (default: all supported branches)",
    )
    ap.add_argument(
        "--no-ai",
        action="store_true",
        help="deterministic only; skip the AI advisory fallback",
    )
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args()

    commit = args.commit
    files = bot.get_changed_files(commit)
    introducers = bot.find_introducing_commit(commit, files)
    branches = args.branches or bot.get_supported_branches()

    # Pass commit/changed_files so the AI fallback can run, unless --no-ai.
    ai_kwargs = {} if args.no_ai else {"commit": commit, "changed_files": files}

    results = []
    for branch in branches:
        affected, advisory = bot.is_branch_affected(introducers, branch, **ai_kwargs)

        if affected:
            already = bot.is_already_patched(commit, branch)
            status = "already-patched" if already else "AFFECTED"
            detail = (
                "fix already present (patch-id match)"
                if already
                else "deterministic (ancestry / patch-id)"
            )
            ai_note = ""
        else:
            status = "not affected"
            detail = ""
            ai_note = ""
            if advisory is not None:
                likely = advisory.get("likely_affected")
                conf = advisory.get("confidence")
                verdict = {
                    True: "likely affected",
                    False: "likely not affected",
                    None: "uncertain",
                }[likely]
                ai_note = f"AI: {verdict} ({conf})"
                if likely:
                    detail = "deterministic says no; AI flags a possible miss"

        results.append(
            {
                "branch": branch,
                "needs_backport": status == "AFFECTED",
                "status": status,
                "detail": detail,
                "ai": ai_note,
            }
        )

    if args.json:
        print(
            json.dumps(
                {
                    "commit": commit,
                    "changed_files": files,
                    "introducers": sorted(introducers),
                    "branches": results,
                },
                indent=2,
            )
        )
        return

    print(f'Commit: {commit}  "{commit_subject(commit)}"')
    print(f"Changed files: {files}")
    print(f"Introducer(s): {sorted(s[:8] for s in introducers)}")
    print()
    print(f"  {'branch':<24} {'status':<16} detail")
    print(f"  {'-' * 24} {'-' * 16} {'-' * 44}")
    for r in results:
        detail = r["detail"]
        if r["ai"]:
            detail = (detail + " | " if detail else "") + r["ai"]
        print(f"  {r['branch']:<24} {r['status']:<16} {detail}")

    affected = [r["branch"] for r in results if r["needs_backport"]]
    print()
    if affected:
        print(f"Affected (need backport): {', '.join(affected)}")
    else:
        print("No supported branches need a backport.")

    ai_flags = [
        r["branch"]
        for r in results
        if r["status"] == "not affected" and "likely affected" in r["ai"]
    ]
    if ai_flags:
        print(
            "AI-flagged for human review (deterministic said no): "
            + ", ".join(ai_flags)
        )


if __name__ == "__main__":
    main()
