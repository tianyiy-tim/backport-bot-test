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
import re
import subprocess
import sys

import engine as bot
from ci import _assert_fork_remote, _gh, _plan_marker, _summary_table
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

    if marker_files:
        print(f"   edit:   {', '.join(os.path.basename(p) for p in marker_files)}")
    if rerere_files:
        names = ", ".join(os.path.basename(p) for p in rerere_files)
        print(f"   verify: {names}  (auto-applied by rerere)")
    print(f"   shell in {wt}")
    print("   fix the files, then `exit` to continue")
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
        print("   clean — no conflict (ci/apply opens clean backports)")
        return "clean", None

    base_sha = git("rev-parse", ref).stdout.strip()
    while True:
        _edit_in_branch_shell(wt, branch)
        if not _cherry_pick_in_progress(wt):
            # The user finished (or aborted) the cherry-pick themselves in the shell.
            head = git("rev-parse", "HEAD", cwd=wt).stdout.strip()
            if head == base_sha:
                print(f"   skipped — cherry-pick aborted in shell (kept: {wt})")
                return "blocked", wt
            break  # they committed the resolution themselves
        still = _stage_resolved(wt)
        if not still:
            break
        left = ", ".join(os.path.basename(p) for p in still)
        print(f"   still unresolved: {left}")
        if not _ask_yn("   re-enter to keep editing?"):
            print(f"   left for later: {wt}")
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
            print(f"   cherry-pick --continue failed (kept: {wt})")
            return "blocked", wt
    new_sha = git("rev-parse", "HEAD", cwd=wt).stdout.strip()
    git("branch", "-f", local_branch, new_sha)
    remove_worktree(wt)
    print("   resolved ✓")
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
        print("   clean — no conflict (ci/apply opens clean backports)")
        return "clean", None

    base_sha = git("rev-parse", ref).stdout.strip()
    while True:
        conflicts = unmerged_files(repo)
        marker = [
            c["path"]
            for c in conflicts
            if file_has_conflict_markers(os.path.join(repo, c["path"]))
        ]
        rerere = [c["path"] for c in conflicts if c["path"] not in marker]
        print(f"   checked out in your repo — edit in your IDE ({repo})")
        if rerere:
            names = ", ".join(os.path.basename(p) for p in rerere)
            print(f"   auto-applied by rerere, just verify: {names}")
        if marker:
            print(f"   edit: {', '.join(os.path.basename(p) for p in marker)}")
        else:
            print("   nothing to edit — rerere resolved it; confirm to continue")

        if not _ask_yn(f"   done resolving {branch}?"):
            print(
                f"   left checked out on {branch} to finish by hand "
                "(`git add -A && git cherry-pick --continue`, or `--abort`)"
            )
            return "blocked", branch
        if not _cherry_pick_in_progress(repo):
            head = git("rev-parse", "HEAD").stdout.strip()
            if head == base_sha:
                print("   skipped — cherry-pick aborted")
                return "blocked", branch
            break  # user ran --continue themselves
        still = _stage_resolved(repo)
        if not still:
            break
        print(f"   still unresolved: {', '.join(os.path.basename(p) for p in still)}")

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
            print("   cherry-pick --continue failed")
            return "blocked", branch
    new_sha = git("rev-parse", "HEAD").stdout.strip()
    git("branch", "-f", local_branch, new_sha)
    print("   resolved ✓")
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


def _find_open_pr_url(head: str) -> "str | None":
    """URL of the open PR whose head branch is *head*, or None. Used to relink the
    clean backport PRs `ci` already opened when we rebuild the summary."""
    r = _gh(
        "pr",
        "list",
        "--head",
        head,
        "--state",
        "open",
        "--json",
        "url",
        "-q",
        ".[0].url",
        check=False,
    )
    return r.stdout.strip() or None


def _post_resolution_summary(
    pr,
    fix_sha,
    subject,
    buckets,
    created,
    clean_skipped,
    still_conflicting,
    errors,
    run_id,
) -> None:
    """Post an updated, ci-style summary comment on the source PR after resolving.

    Same table format as `ci`, but the previously-conflicting branches now show
    their freshly opened backport PR (✅) instead of a merge-conflict warning.
    """
    outcomes: dict = {}
    for branch, url in created.items():
        outcomes[branch] = ("opened", url)
    for branch in clean_skipped:
        url = _find_open_pr_url(f"backport/{branch}/{run_id}")
        outcomes[branch] = ("opened", url) if url else ("done", None)
    for branch in still_conflicting:
        outcomes[branch] = ("error", "still needs resolution")
    for branch, msg in errors.items():
        outcomes[branch] = ("error", msg)
    table = _summary_table(fix_sha, subject, buckets, outcomes, source_pr=pr)
    body = (
        "🔧 **Updated after `backport resolve`** — conflicts resolved locally; "
        "backport PRs opened for the previously-conflicting branches.\n\n"
        + table
        + "\n\n"
        + _plan_marker(fix_sha, subject, buckets, outcomes)
    )
    _gh("pr", "comment", str(pr), "--body", body, check=False)


_PLAN_RE = re.compile(r"<!-- backport-bot-plan:(.*?) -->")


def _read_bot_plan(pr) -> "dict | None":
    """Read the backport bot's machine-readable plan from the latest summary
    comment on *pr*, so we can target exactly the branches `ci` flagged without
    re-running the impact analysis. Returns the parsed dict, or None if there is
    no such comment (then the caller falls back to computing it locally).
    """
    r = _gh(
        "pr",
        "view",
        str(pr),
        "--json",
        "comments",
        "-q",
        ".comments[].body",
        check=False,
    )
    if r.returncode != 0:
        return None
    matches = _PLAN_RE.findall(r.stdout)
    if not matches:
        return None
    try:
        return json.loads(matches[-1])  # the most recent summary wins
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------
# Command
# --------------------------------------------------------------------------


def _run_resolution(
    args, fix_sha, subject, buckets, targets, preopened, source_pr
) -> int:
    """Interactively resolve *targets* for *fix_sha*, then optionally open PRs.

    Shared engine behind both entry points: `cmd_resolve` (which derives the
    targets from the PR plan or a local analysis) and `apply` (which hands off the
    branches that just conflicted during a local cherry-pick session, so nothing
    is re-analyzed). *preopened* are branches already backported elsewhere (ci's
    clean PRs), carried into the final summary; *source_pr*, when set, gets the
    updated summary comment.
    """
    if not sys.stdin.isatty():
        print(
            "\nresolve is interactive; run it in a terminal (not a pipe/CI).",
            file=sys.stderr,
        )
        return 3

    remote = getattr(args, "remote", "origin")
    enable_rerere()
    in_place = getattr(args, "in_place", False)
    original_ref = None
    left_on_branch = None  # set if an --in-place branch is left checked out
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

    where = "your checkout" if in_place else "a worktree"
    print(f"\nResolving {len(targets)} conflicting branch(es) in {where}:")
    print(f"   {', '.join(targets)}")
    print("   (rerere on — a fix is reused across identical conflicts)")

    resolved: "dict[str, str]" = {}  # conflict branch -> local_branch (ready to PR)
    clean_skipped: "list[str]" = list(preopened)  # clean/already-opened (ci's job)
    blocked: "dict[str, str]" = {}  # branch -> worktree path / branch name
    errors: "dict[str, str]" = {}  # branch -> message
    for branch in targets:
        print(f"\n── {branch} " + "─" * max(0, 50 - len(branch)))
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
            print(f"   error: {detail}")

    # Restore the user's original branch unless we deliberately left them on one.
    if in_place and original_ref and not left_on_branch:
        git("checkout", "--quiet", original_ref, check=False)

    print("\n── summary " + "─" * 42)
    if clean_skipped:
        print(f"   clean (ci handles) : {', '.join(clean_skipped)}")
    print(f"   resolved           : {', '.join(resolved) or '—'}")
    if left_on_branch:
        print(f"   stopped on         : {left_on_branch} (finish it, then re-run)")
    if blocked:
        print(f"   unfinished         : {', '.join(blocked)}")
    if errors:
        print(f"   errors             : {', '.join(errors)}")

    if not resolved:
        print("\nNothing to open PRs for.")
        return 0

    if not _ask_yn(f"\nOpen {len(resolved)} PR(s) ({', '.join(resolved)})?"):
        print("Skipped. Local branches kept: " + ", ".join(resolved.values()))
        return 0

    _assert_fork_remote(remote)  # only gate the push, so local resolution always works
    print()
    created: "dict[str, str]" = {}
    for branch, local_branch in resolved.items():
        url = _open_pr(branch, local_branch, fix_sha, subject, source_pr, remote)
        if url.startswith("error:"):
            print(f"   {branch}: {url}")
        else:
            created[branch] = url
            print(f"   {branch}  {url}")

    # Post an updated ci-style summary on the source PR: the previously-conflicting
    # branches now show their opened backport PR instead of a conflict warning.
    still_conflicting = list(blocked) + ([left_on_branch] if left_on_branch else [])
    if source_pr and created:
        _post_resolution_summary(
            source_pr,
            fix_sha,
            subject,
            buckets,
            created,
            clean_skipped,
            still_conflicting,
            errors,
            fix_sha[:8],
        )
        print(f"\nUpdated the summary on #{source_pr}.")
    return 0


def cmd_resolve(args) -> int:
    """Interactively resolve backport conflicts and open one PR per branch."""
    # Prefer the backport bot's own summary on the PR: it already ran the impact
    # analysis (AI) in CI, so reading its plan avoids a second AI pass and targets
    # exactly the branches it flagged. Fall back to computing locally when there is
    # no such comment (e.g. --commit with no PR) or when --reanalyze is given.
    plan = None
    if getattr(args, "pr", None) and not getattr(args, "reanalyze", False):
        plan = _read_bot_plan(args.pr)

    if plan:
        fix_sha = plan.get("fix") or _resolve_fix_ref(args)[0]
        subject = plan.get("subject", "")
        branch_info = plan.get("branches", {})
        buckets = {b: info.get("impact", AFFECTED) for b, info in branch_info.items()}
        targets = bot.sort_branches(
            b for b, info in branch_info.items() if info.get("outcome") == "conflict"
        )
        # Branches ci already opened clean PRs for -- carry them into the final
        # summary so it stays complete (relinked to their existing PRs).
        preopened = [
            b
            for b, info in branch_info.items()
            if info.get("outcome") in ("opened", "done")
        ]
        if not ref_exists(fix_sha):
            git("fetch", args.remote, fix_sha, check=False)
        print(
            f"Using the backport bot's summary from #{args.pr} "
            f"(no re-analysis): {len(targets)} conflicting branch(es) to resolve."
        )
        if not targets:
            print("Nothing left to resolve on that PR.")
            return 0
    else:
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
        preopened = []
        if not targets:
            print("\nNo AFFECTED branches; nothing to resolve.")
            return 0

    return _run_resolution(
        args,
        fix_sha,
        subject,
        buckets,
        targets,
        preopened,
        source_pr=getattr(args, "pr", None),
    )
