"""
The ``resolve`` command: interactive, local backport-conflict resolution.

Given a fix (``--commit <sha>`` or ``--pr <number>``), find the AFFECTED release
branches (AI on unless ``--no-ai``) and, for each one that conflicts, walk the
user file-by-file through resolving the conflict in a real ``git worktree`` they
can edit. ``git rerere`` is enabled, so resolving a conflict once auto-applies to
identical conflicts on sibling branches (e.g. the FIPS twin branches). When every
branch is ready, optionally push and open one normal (non-draft) PR per branch.

This is the local, human-in-the-loop counterpart to ``ci``: ``ci`` opens PRs for
the clean cherry-picks and only *reports* conflicts; ``resolve`` is where those
conflicts actually get fixed.
"""

import json
import os
import sys

import engine as bot
from ci import _assert_fork_remote, _gh
from common import AFFECTED, BackportError
from gitutil import (
    add_worktree,
    enable_rerere,
    file_has_conflict_markers,
    git,
    ref_exists,
    remove_worktree,
    resolve_commit,
    unmerged_files,
)
from patches import _ask_yn
from render import print_summary
from verdicts import bucket_branches, resolve_inconclusive


# --------------------------------------------------------------------------
# Resolving which fix to backport
# --------------------------------------------------------------------------


def _pr_commit(pr: str, remote: str) -> str:
    """Resolve a PR number to a backportable commit-ish via ``gh``.

    A merged PR resolves to its merge/squash commit; an open PR to its head
    commit. If the commit is not present locally we fetch it from *remote* first
    (a merge commit, or the ``pull/<n>/head`` ref for an open PR).
    """
    info = _gh(
        "pr", "view", str(pr), "--json", "state,mergeCommit,headRefOid", check=False
    )
    if info.returncode != 0:
        raise BackportError(
            f"could not read PR #{pr}: {(info.stderr or info.stdout).strip()}"
        )
    data = json.loads(info.stdout or "{}")
    merge = (data.get("mergeCommit") or {}).get("oid")
    if merge:
        if not ref_exists(merge):
            git("fetch", remote, merge, check=False)
        return merge
    head = data.get("headRefOid")
    if not head:
        raise BackportError(f"PR #{pr} has no resolvable commit.")
    if not ref_exists(head):
        git("fetch", remote, f"pull/{pr}/head", check=False)
    return head


def _resolve_fix_ref(args) -> "tuple[str, str]":
    """Return ``(fix_sha, subject)`` for the fix named by ``--commit`` or ``--pr``."""
    if getattr(args, "commit", None):
        return resolve_commit(args.commit)
    if getattr(args, "pr", None):
        return resolve_commit(_pr_commit(args.pr, args.remote))
    raise BackportError("resolve needs --commit <sha> or --pr <number>.")


# --------------------------------------------------------------------------
# Interactive per-branch conflict walk
# --------------------------------------------------------------------------


def _walk_conflicts(wt: str, branch: str) -> "list[str]":
    """Walk the unmerged files in *wt* one at a time, prompting the user.

    For each file: content conflicts prompt "has the conflict been resolved?";
    files with no markers (auto-resolved by rerere, or a modify/delete) are noted
    and confirmed. On "Y" we re-scan for leftover ``<<<<<<<`` / ``>>>>>>>`` markers
    and refuse to stage (re-prompt) if any remain -- markers are never committed.
    On "N" the file is left unresolved and we move on.

    Staging a file (``git add``) drops it from the unmerged set, so the loop
    naturally advances file by file. Returns the list of paths left unresolved.
    """
    unresolved: "list[str]" = []
    asked: "set[str]" = set()
    while True:
        remaining = [f for f in unmerged_files(wt) if f["path"] not in asked]
        if not remaining:
            break
        entry = remaining[0]
        path, kind = entry["path"], entry["kind"]
        full = os.path.join(wt, path)

        if kind == "modify/delete":
            print(
                f"\n  - {path}: {kind} -- the fix changes a file this branch "
                "deleted (or vice versa). Keep or remove it as appropriate."
            )
            prompt = f"{path} ({kind}) -- has this been resolved?"
        elif not file_has_conflict_markers(full):
            print(
                f"\n  - {path}: auto-resolved (via rerere?) -- please VERIFY the "
                "result is correct before confirming."
            )
            prompt = f"{path} -- resolution looks applied; is it correct?"
        else:
            prompt = (
                f"{path} requires conflict resolution, "
                "has the conflict been resolved?"
            )

        if not _ask_yn("  " + prompt):
            unresolved.append(path)
            asked.add(path)
            continue

        # "Y": never stage a file that still has conflict markers.
        if file_has_conflict_markers(full):
            print(
                "    !! still contains <<<<<<< / >>>>>>> conflict markers -- "
                "not resolved. Please finish editing it, then answer again."
            )
            continue  # re-prompt the same file
        git("add", "--", path, cwd=wt)

    return unresolved


def _resolve_branch(fix_sha: str, branch: str, run_id: str) -> "tuple[str, str]":
    """Cherry-pick *fix_sha* onto ``origin/<branch>`` in a persistent worktree,
    resolving conflicts interactively.

    Returns ``(status, detail)``:
      - ``("clean", local_branch)``   applied with no conflict; worktree removed.
      - ``("ready", local_branch)``   conflicts resolved and committed; worktree removed.
      - ``("blocked", worktree)``     files left unresolved; worktree KEPT for the user.
      - ``("error", message)``
    """
    ref = f"origin/{branch}"
    if not ref_exists(ref):
        return "error", f"{ref} not found"
    local_branch = f"backport/{branch}/{run_id}"
    try:
        wt = add_worktree(ref)
    except BackportError as exc:
        return "error", str(exc)

    pick = git("cherry-pick", fix_sha, check=False, cwd=wt)
    if pick.returncode == 0:
        new_sha = git("rev-parse", "HEAD", cwd=wt).stdout.strip()
        git("branch", "-f", local_branch, new_sha)
        remove_worktree(wt)
        print(f"  OK {branch}: clean cherry-pick, no conflicts.")
        return "clean", local_branch

    print(f"\n  !! {branch}: conflicts -- edit the files in:\n      {wt}")
    unresolved = _walk_conflicts(wt, branch)
    if unresolved:
        print(
            f"  .. {branch}: {len(unresolved)} file(s) still unresolved "
            f"({', '.join(unresolved)}).\n"
            f"      The cherry-pick is paused in {wt}\n"
            "      Finish there (edit, `git add`, `git cherry-pick --continue`), "
            "or re-run `resolve` later. Worktree left in place."
        )
        return "blocked", wt

    cont = git(
        "-c",
        "user.name=backport-cli",
        "-c",
        "user.email=backport-cli@local",
        "-c",
        "core.editor=true",
        "cherry-pick",
        "--continue",
        check=False,
        cwd=wt,
    )
    if cont.returncode != 0:
        print(
            f"  !! {branch}: `cherry-pick --continue` failed: "
            f"{(cont.stderr or cont.stdout).strip()}\n"
            f"      Worktree left in place: {wt}"
        )
        return "blocked", wt
    new_sha = git("rev-parse", "HEAD", cwd=wt).stdout.strip()
    git("branch", "-f", local_branch, new_sha)
    remove_worktree(wt)
    print(f"  OK {branch}: conflicts resolved, backport commit ready.")
    return "ready", local_branch


# --------------------------------------------------------------------------
# Opening a PR for a ready branch
# --------------------------------------------------------------------------


def _open_pr(
    branch: str, local_branch: str, fix_sha: str, subject: str, source_pr, remote: str
) -> str:
    """Push *local_branch* to the fork and open a normal PR into the release
    branch. Returns the PR URL or an ``"error: ..."`` string."""
    link = f" of #{source_pr}" if source_pr else ""
    title = f"[backport {branch}] {subject}"
    body = (
        f"Backport{link} (`{fix_sha[:12]}`) onto `{branch}`, with merge conflicts "
        "resolved locally.\n\n"
        "- Impact verdict: **AFFECTED**.\n"
        "- **Not** auto-merged -- please review the conflict resolution before "
        "merging.\n\n"
        "_Opened by the AWS-LC backport bot (`backport resolve`)._"
    )
    push = git(
        "push",
        "--force-with-lease",
        remote,
        f"{local_branch}:{local_branch}",
        check=False,
    )
    if push.returncode != 0:
        return f"error: push failed: {(push.stderr or push.stdout).strip()}"
    pr = _gh(
        "pr",
        "create",
        "--base",
        branch,
        "--head",
        local_branch,
        "--title",
        title,
        "--body",
        body,
        check=False,
    )
    if pr.returncode != 0:
        return f"error: gh pr create failed: {(pr.stderr or pr.stdout).strip()}"
    return pr.stdout.strip()


# --------------------------------------------------------------------------
# Command
# --------------------------------------------------------------------------


def cmd_resolve(args) -> int:
    """Interactively resolve backport conflicts and open one PR per branch."""
    _assert_fork_remote(args.remote)
    fix_sha, subject = _resolve_fix_ref(args)

    branches = bot.sort_branches(bot.get_supported_branches())
    if not branches:
        raise BackportError(
            "no supported release branches found (is this an AWS-LC clone with "
            "the release branches fetched? `git fetch origin`)."
        )

    files, introducers, buckets = bucket_branches(fix_sha, branches)
    buckets, decided_by, _ = resolve_inconclusive(
        args, fix_sha, files, introducers, buckets
    )
    print_summary(fix_sha, files, introducers, buckets, decided_by)

    targets = bot.sort_branches(b for b, s in buckets.items() if s == AFFECTED)
    if not targets:
        print("\nNo AFFECTED branches; nothing to resolve.")
        return 0

    if not sys.stdin.isatty():
        print(
            "\nresolve is interactive; run it in a terminal (not a pipe/CI).",
            file=sys.stderr,
        )
        return 3

    enable_rerere()
    print(f"\nResolving backports for: {', '.join(targets)}")
    print(
        "(git rerere is on: resolving a conflict once auto-applies it to identical "
        "conflicts on sibling branches.)"
    )

    ready: "dict[str, str]" = {}  # branch -> local_branch
    blocked: "dict[str, str]" = {}  # branch -> worktree path
    errors: "dict[str, str]" = {}  # branch -> message
    for branch in targets:
        print(f"\n== {branch} " + "=" * max(0, 48 - len(branch)))
        status, detail = _resolve_branch(fix_sha, branch, fix_sha[:8])
        if status in ("clean", "ready"):
            ready[branch] = detail
        elif status == "blocked":
            blocked[branch] = detail
        else:
            errors[branch] = detail
            print(f"  !! {branch}: {detail}")

    print("\n" + "=" * 60)
    print(f"Ready to PR : {', '.join(ready) or '-'}")
    if blocked:
        print(f"Unfinished  : {', '.join(blocked)} (worktrees kept)")
    if errors:
        print(f"Errors      : {', '.join(errors)}")

    if not ready:
        print("\nNothing ready to open PRs for.")
        return 0

    if not _ask_yn(f"\nCreate PRs for {len(ready)} branch(es) ({', '.join(ready)})?"):
        print("Skipped PR creation. Local branches kept: " + ", ".join(ready.values()))
        return 0

    print()
    for branch, local_branch in ready.items():
        url = _open_pr(branch, local_branch, fix_sha, subject, args.pr, args.remote)
        if url.startswith("error:"):
            print(f"  !! {branch}: {url}")
        else:
            print(f"  OK {branch}: {url}")
    return 0
