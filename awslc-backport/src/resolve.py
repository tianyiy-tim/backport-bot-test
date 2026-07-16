"""
The ``resolve`` command: interactive, local backport-conflict resolution.

Given a fix (``--commit <sha>`` or ``--pr <number>``), find the AFFECTED release
branches (AI on unless ``--no-ai``) and, for each one whose cherry-pick
**conflicts**, drop the user into an interactive shell *inside* that branch's
throwaway ``git worktree`` -- the fix is already applied and the conflict is live,
so they edit the files in place with their own editor, then ``exit`` to continue
to the next branch. Files they've cleaned up are staged automatically; anything
still holding conflict markers is reported and they can re-enter. ``git rerere`` is
enabled, so resolving a conflict once auto-applies to identical conflicts on
sibling branches (e.g. the FIPS twins). When the conflicts are resolved,
optionally push and open one normal (non-draft) PR per resolved branch.

The user's real checkout is never touched -- everything happens in worktrees.
Clean cherry-picks are **skipped** here on purpose: ``ci`` (and ``apply``) already
open those, so re-opening them from ``resolve`` would clash on the same branch
name. ``resolve`` owns exactly the branches ``ci`` reported as conflicts — it is
the local, human-in-the-loop counterpart that finishes what ``ci`` could not.
"""

import json
import os
import subprocess
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


def _cherry_pick_in_progress(wt: str) -> bool:
    """True if a cherry-pick is still in progress in *wt* (CHERRY_PICK_HEAD exists).
    False once the user (or we) run ``git cherry-pick --continue``/``--abort``."""
    return (
        git(
            "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD", check=False, cwd=wt
        ).returncode
        == 0
    )


def _stage_resolved(wt: str) -> "list[str]":
    """Stage every unmerged file that no longer contains conflict markers, and
    return the paths that STILL have markers (i.e. not yet resolved).

    This is how we let the user just edit files in the shell without needing to
    ``git add`` -- anything they've cleaned up gets staged for them, and anything
    still holding ``<<<<<<<`` / ``>>>>>>>`` markers is reported back so we never
    continue the cherry-pick with an unresolved file.
    """
    still: "list[str]" = []
    for f in unmerged_files(wt):
        if file_has_conflict_markers(os.path.join(wt, f["path"])):
            still.append(f["path"])
        else:
            git("add", "--", f["path"], cwd=wt)
    return still


def _edit_in_branch_shell(wt: str, branch: str) -> None:
    """Drop the user into an interactive shell *inside* the branch's worktree.

    The fix is already cherry-picked there and the conflicts are live, so the user
    is literally "in" the branch: `git status` shows the conflict, they edit with
    their own editor, and can run any git command. Typing ``exit`` (or Ctrl-D)
    returns control to ``resolve``. Their real checkout is never touched.
    """
    conflicts = unmerged_files(wt)
    marker_files = [
        c["path"]
        for c in conflicts
        if file_has_conflict_markers(os.path.join(wt, c["path"]))
    ]
    rerere_files = [c["path"] for c in conflicts if c["path"] not in marker_files]

    print(
        f"\n  >> Entering {branch} -- the fix is applied and conflicts are live here."
    )
    print(f"     Worktree: {wt}")
    if marker_files:
        print("     Edit these (they contain <<<<<<< / >>>>>>> markers):")
        for p in marker_files:
            print(f"       - {p}")
    if rerere_files:
        print("     Auto-resolved via rerere -- please VERIFY:")
        for p in rerere_files:
            print(f"       - {p}")
    print(
        "     Then type `exit` (or Ctrl-D) to continue. No need to `git add` -- "
        "resolved files are staged for you.\n"
    )
    shell = os.environ.get("SHELL") or "/bin/bash"
    subprocess.call([shell], cwd=wt)


def _resolve_branch(fix_sha: str, branch: str, run_id: str) -> "tuple[str, str]":
    """Cherry-pick *fix_sha* onto ``origin/<branch>`` in a persistent worktree,
    resolving conflicts interactively.

    Returns ``(status, detail)``:
      - ``("clean", None)``           applied with no conflict; skipped (clean
                                      backports are `ci`/`apply`'s job), worktree removed.
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
        # No conflict -> nothing to resolve. `ci` (and `apply`) already open clean
        # backport PRs, so we skip it here to avoid clashing on the same branch
        # name. `resolve` only owns the branches that actually conflict.
        git("cherry-pick", "--abort", check=False, cwd=wt)
        remove_worktree(wt)
        print(
            f"  OK {branch}: clean cherry-pick, no conflict -- skipping "
            "(clean backports are opened by `ci`/`apply`)."
        )
        return "clean", None

    print(f"\n  !! {branch}: {len(unmerged_files(wt))} conflicting file(s).")
    base_sha = git("rev-parse", ref).stdout.strip()
    while True:
        _edit_in_branch_shell(wt, branch)
        if not _cherry_pick_in_progress(wt):
            # The user finished (or aborted) the cherry-pick themselves in the shell.
            head = git("rev-parse", "HEAD", cwd=wt).stdout.strip()
            if head == base_sha:
                print(
                    f"  .. {branch}: cherry-pick was aborted in the shell; nothing "
                    f"committed. Skipping. Worktree left in place: {wt}"
                )
                return "blocked", wt
            break  # they committed the resolution themselves
        still = _stage_resolved(wt)
        if not still:
            break
        print(f"  .. {branch}: still has conflict markers in: {', '.join(still)}")
        if not _ask_yn(f"  Re-enter {branch} to keep resolving?"):
            print(f"      Worktree left in place: {wt}")
            return "blocked", wt

    if _cherry_pick_in_progress(wt):
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


def _current_ref() -> str:
    """The branch name currently checked out, or the raw SHA if detached."""
    r = git("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return git("rev-parse", "HEAD").stdout.strip()


def _resolve_branch_in_place(
    fix_sha: str, branch: str, run_id: str, repo: str
) -> "tuple[str, str]":
    """Like :func:`_resolve_branch`, but checks the branch out **in the user's own
    working repo** (detached) instead of a worktree, so their open IDE reflects the
    conflict live. The caller restores the original branch afterwards.

    Returns the same ``(status, detail)`` contract; for ``"blocked"`` the *detail*
    is the branch name and the repo is intentionally left checked out on it so the
    user can finish by hand.
    """
    ref = f"origin/{branch}"
    if not ref_exists(ref):
        return "error", f"{ref} not found"
    local_branch = f"backport/{branch}/{run_id}"
    co = git("checkout", "--quiet", "--detach", ref, check=False)
    if co.returncode != 0:
        return "error", f"could not check out {ref}: {(co.stderr or co.stdout).strip()}"

    pick = git("cherry-pick", fix_sha, check=False)
    if pick.returncode == 0:
        # Clean -> ci/apply own it; skip. The commit sits on detached HEAD and is
        # discarded when we check out the next branch / restore the original.
        print(
            f"  OK {branch}: clean cherry-pick, no conflict -- skipping "
            "(clean backports are opened by `ci`/`apply`)."
        )
        return "clean", None

    base_sha = git("rev-parse", ref).stdout.strip()
    print(f"\n  >> Your working tree is now ON {branch} (detached), fix applied.")
    print(f"     Repo: {repo}")
    for c in unmerged_files(repo):
        print(f"       - {c['path']} ({c['kind']})")
    print(
        "     Resolve the conflicts in your IDE (this is your real checkout), "
        "then answer below. No need to `git add` -- resolved files are staged."
    )
    while True:
        if not _ask_yn(f"  Done resolving {branch}?"):
            print(
                f"     Leaving you checked out on {branch} (mid cherry-pick) to "
                "finish by hand:\n"
                "       git add -A && git cherry-pick --continue   # when done\n"
                "       git cherry-pick --abort                    # to bail"
            )
            return "blocked", branch
        if not _cherry_pick_in_progress(repo):
            head = git("rev-parse", "HEAD").stdout.strip()
            if head == base_sha:
                print(f"  .. {branch}: cherry-pick aborted; skipping.")
                return "blocked", branch
            break  # user ran --continue themselves
        still = _stage_resolved(repo)
        if not still:
            break
        print(f"  .. {branch}: still has conflict markers in: {', '.join(still)}")

    if _cherry_pick_in_progress(repo):
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
        )
        if cont.returncode != 0:
            print(
                f"  !! {branch}: `cherry-pick --continue` failed: "
                f"{(cont.stderr or cont.stdout).strip()}"
            )
            return "blocked", branch
    new_sha = git("rev-parse", "HEAD").stdout.strip()
    git("branch", "-f", local_branch, new_sha)
    print(f"  OK {branch}: conflicts resolved, backport commit ready ({local_branch}).")
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
    in_place = getattr(args, "in_place", False)
    original_ref = None
    left_on_branch = (
        None  # set if an --in-place branch is left checked out for the user
    )
    if in_place:
        dirty = git("status", "--porcelain").stdout.strip()
        if dirty:
            raise BackportError(
                "--in-place needs a clean working tree (it checks each branch out "
                "in your current repo). Commit or stash your changes first."
            )
        original_ref = _current_ref()
        if os.path.abspath(__file__).startswith(
            os.path.abspath(bot.REPO_PATH) + os.sep
        ):
            print(
                "note: the tool lives inside the target repo, so --in-place will "
                "briefly remove `awslc-backport/` while a release branch is checked "
                "out (restored at the end). To avoid that, run from a separate clone "
                "with --repo."
            )

    mode = "in your current checkout" if in_place else "in isolated worktrees"
    print(f"\nResolving conflicting backports ({mode}) among: {', '.join(targets)}")
    print(
        "(clean cherry-picks are skipped -- `ci`/`apply` open those; `resolve` only "
        "handles branches that conflict. git rerere is on, so resolving a conflict "
        "once auto-applies it to identical conflicts on sibling branches.)"
    )

    resolved: "dict[str, str]" = {}  # conflict branch -> local_branch (ready to PR)
    clean_skipped: "list[str]" = []  # clean cherry-picks (ci/apply's job)
    blocked: "dict[str, str]" = {}  # branch -> worktree path / branch name
    errors: "dict[str, str]" = {}  # branch -> message
    for branch in targets:
        print(f"\n== {branch} " + "=" * max(0, 48 - len(branch)))
        if in_place:
            status, detail = _resolve_branch_in_place(
                fix_sha, branch, fix_sha[:8], bot.REPO_PATH
            )
        else:
            status, detail = _resolve_branch(fix_sha, branch, fix_sha[:8])
        if status == "clean":
            clean_skipped.append(branch)
        elif status == "ready":
            resolved[branch] = detail
        elif status == "blocked":
            if in_place:
                # The repo is left checked out on this branch mid-cherry-pick; stop
                # here rather than yanking it out from under the user.
                left_on_branch = branch
                break
            blocked[branch] = detail
        else:
            errors[branch] = detail
            print(f"  !! {branch}: {detail}")

    # Restore the user's original branch unless we deliberately left them on one.
    if in_place and original_ref and not left_on_branch:
        git("checkout", "--quiet", original_ref, check=False)

    print("\n" + "=" * 60)
    if clean_skipped:
        print(f"Clean (no conflict, handled by ci/apply): {', '.join(clean_skipped)}")
    print(f"Resolved & ready to PR: {', '.join(resolved) or '-'}")
    if left_on_branch:
        print(
            f"Stopped on {left_on_branch} (checked out in your repo, mid cherry-pick). "
            f"Finish it, then re-run `resolve` for the rest."
        )
    if blocked:
        print(f"Unfinished  : {', '.join(blocked)} (worktrees kept)")
    if errors:
        print(f"Errors      : {', '.join(errors)}")

    if not resolved:
        print("\nNo conflicts were resolved; nothing to open PRs for.")
        return 0

    if not _ask_yn(
        f"\nCreate PRs for {len(resolved)} resolved branch(es) ({', '.join(resolved)})?"
    ):
        print(
            "Skipped PR creation. Local branches kept: " + ", ".join(resolved.values())
        )
        return 0

    print()
    for branch, local_branch in resolved.items():
        url = _open_pr(branch, local_branch, fix_sha, subject, args.pr, args.remote)
        if url.startswith("error:"):
            print(f"  !! {branch}: {url}")
        else:
            print(f"  OK {branch}: {url}")
    return 0
