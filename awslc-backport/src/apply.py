"""
The ``apply`` and ``clear`` commands.

``apply`` cherry-picks the fix onto local ``backport/<branch>/<id>`` branches for
review -- it never pushes, opens a PR, or auto-merges. ``clear`` drops the saved
run state.
"""

import shutil
import sys
from typing import List, Sequence, Tuple

import engine as bot
from common import AFFECTED
from gitutil import cherry_pick_local, git
from patches import (
    _ask_yn,
    commit_from_patch,
    is_empty_patch,
    resolve_patch_and_base,
)
from resolve import _run_resolution
from runstate import delete_patch_artifacts, run_dir
from verdicts import bucket_branches


def _run_cherry_picks(
    fix_sha: str, targets: Sequence[str]
) -> "Tuple[List[str], List[str], List[str]]":
    """Cherry-pick the fix onto each target branch, printing per-branch status.

    Returns ``(clean, conflict, errors)`` as lists of branch names.
    """
    run_id = fix_sha[:8]
    clean: List[str] = []
    conflict: List[str] = []
    errors: List[str] = []
    for branch in targets:
        status, detail, conflicts = cherry_pick_local(fix_sha, branch, run_id)
        if status == "clean":
            print(f"  [OK] {branch}  ->  {detail}")
            clean.append(branch)
        elif status == "conflict":
            files = ", ".join(f"{c['path']} ({c['kind']})" for c in conflicts)
            print(f"  [!!] {branch}  ->  merge conflict in {files}")
            conflict.append(branch)
        else:
            print(f"  [??] {branch}  ->  error: {detail}")
            errors.append(branch)
    return clean, conflict, errors


def _cleanup_after_apply(args, run, conflict, errors) -> None:
    """After a fully clean apply, delete the patch file and cached run state.

    Once the backport branches exist there is no reason to keep an embargoed diff
    lying around on disk. If any branch conflicted or errored we keep everything
    so the user can fix it and re-run. --keep-patch opts out.
    """
    if args.keep_patch or conflict or errors:
        return
    patch_path = run.get("patch_path") if run else getattr(args, "patch", None)
    removed = delete_patch_artifacts(patch_path)
    if removed:
        print("\nCleaned up (clean apply): " + ", ".join(removed))


def _select_targets(args, buckets):
    """Which branches to cherry-pick onto: --branches, or --all-affected.

    Returns a chronologically sorted list, or None if neither flag was given
    (the caller turns that into a usage error).
    """
    if args.branches:
        return bot.sort_branches(args.branches)
    if args.all_affected:
        return bot.sort_branches(b for b, s in buckets.items() if s == AFFECTED)
    return None


def cmd_apply(args) -> int:
    """Cherry-pick the patch onto local branches (never pushes / opens a PR).

    Targets come from --branches, or --all-affected (the AFFECTED branches from
    the last analyze). Each clean pick lands as a local ``backport/<branch>/<id>``
    branch; conflicts are reported, never auto-resolved.
    """
    patch, base, run = resolve_patch_and_base(args)
    if is_empty_patch(patch):
        print("patch is empty; nothing to apply.")
        return 0

    with commit_from_patch(patch, base, three_way=args.three_way) as fix_sha:
        branches = run["branches"] if run else bot.get_supported_branches()
        buckets = run["buckets"] if run else bucket_branches(fix_sha, branches)[2]

        targets = _select_targets(args, buckets)
        if targets is None:
            print(
                "Specify what to apply: --all-affected, or --branches <name..>.",
                file=sys.stderr,
            )
            return 2
        if not targets:
            print("Nothing to apply (no matching branches).")
            return 0

        # Show the plan and confirm before touching anything.
        print("Will cherry-pick the patch onto local branches (no push, no PR):")
        for b in targets:
            print(f"  - {b}  ->  backport/{b}/{fix_sha[:8]}")
        if not args.yes:
            if not sys.stdin.isatty():
                print("\nRefusing to proceed without --yes in a non-interactive shell.")
                return 3
            if input("\nProceed? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Aborted.")
                return 0

        print()
        clean, conflict, errors = _run_cherry_picks(fix_sha, targets)

    print()
    print(f"Clean: {', '.join(clean) or '-'}")
    print(f"Conflicts (resolve by hand): {', '.join(conflict) or '-'}")
    if errors:
        print(f"Errors: {', '.join(errors)}")

    _cleanup_after_apply(args, run, conflict, errors)

    if conflict:
        print(
            "\nConflicting branches were NOT modified (the cherry-pick was aborted; "
            "no conflict markers were committed)."
        )
        if sys.stdin.isatty() and _ask_yn(
            f"Resolve the {len(conflict)} conflicting branch(es) interactively now?"
        ):
            subject = git("log", "-1", "--format=%s", fix_sha).stdout.strip()
            return _run_resolution(
                args,
                fix_sha,
                subject,
                buckets,
                bot.sort_branches(conflict),
                clean,  # already-applied clean backports (for the summary)
                source_pr=None,
            )
        print("  Resolve them later with:  backport resolve --commit " + fix_sha[:12])
    print(
        "\nNothing was pushed or merged. Inspect `git branch --list 'backport/*'`, "
        "then push and open PRs for human review when ready."
    )
    return 0


def cmd_clear(args) -> int:
    """Remove the saved run state (.backport-runs/) from the tool folder."""
    directory = run_dir()
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)
        print(f"Removed {directory}")
    else:
        print(f"Nothing to clear ({directory} does not exist).")
    return 0
