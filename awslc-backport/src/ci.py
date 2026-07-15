"""
The ``ci`` command: post-merge automation for GitHub Actions.

Given a merged commit, analyze every supported branch (AI layer on) and open a
backport PR on the fork for each AFFECTED branch. Clean cherry-picks become PRs
into the release branch (never auto-merged); conflicts/errors are reported and,
if ``--pr`` is given, flagged in a comment on the source PR. Refuses to target
upstream aws/aws-lc -- fork remotes only.
"""

import re
from typing import List, Optional

import engine as bot
from common import AFFECTED, BackportError
from gitutil import cherry_pick_local, git, run
from render import print_summary
from verdicts import bucket_branches, resolve_inconclusive


# --------------------------------------------------------------------------
# GitHub CLI + safety guard
# --------------------------------------------------------------------------


def _gh(*args: str, check: bool = True):
    """Run the GitHub CLI in the target repo. ``gh`` reads GH_TOKEN/GITHUB_TOKEN
    from the environment, which the workflow provides."""
    return run(["gh", *args], check=check)


def _assert_fork_remote(remote: str) -> None:
    """Refuse to run if *remote* points at upstream aws/aws-lc. CI may only push
    branches and open PRs on a fork, never on the canonical repository."""
    url = git("remote", "get-url", remote).stdout.strip()
    if re.search(r"github\.com[:/]aws/aws-lc(\.git)?/?$", url):
        raise BackportError(
            f"remote '{remote}' points at upstream aws/aws-lc ({url}); "
            "CI backports may only target a fork. Aborting."
        )


# --------------------------------------------------------------------------
# Publishing a backport PR
# --------------------------------------------------------------------------


def _open_backport_pr(
    branch: str,
    local_branch: str,
    fix_sha: str,
    subject: str,
    source_pr: Optional[str],
    remote: str,
    reason: str,
    dry_run: bool,
) -> str:
    """Push a clean cherry-pick branch to the fork and open a PR into the release
    branch. Returns the PR URL, or ``"dry-run"``, or an ``"error: ..."`` string."""
    title = f"[backport {branch}] {subject}"
    link = f" of #{source_pr}" if source_pr else ""
    body = (
        f"Automated backport{link} (`{fix_sha[:12]}`) onto `{branch}`.\n\n"
        f"- Impact verdict: **AFFECTED** ({reason or 'deterministic'}).\n"
        "- Clean cherry-pick; **not** auto-merged -- please review before merging.\n\n"
        "_Opened by the AWS-LC backport bot._"
    )
    if dry_run:
        print(f"    [dry-run] would push {local_branch} and open PR: {title}")
        return "dry-run"
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


def _ci_report(args, opened, conflicts, errors) -> None:
    """Print the CI summary, comment on the source PR about anything that needs a
    manual backport, and emit GitHub Actions warnings for the stuck branches."""
    print()
    print(f"Backport PRs opened: {', '.join(opened) or '-'}")
    if conflicts:
        print(f"Conflicts (manual backport needed): {', '.join(conflicts)}")
    if errors:
        print(f"Errors: {', '.join(errors)}")
    stuck = conflicts + errors
    if stuck and args.pr and not args.dry_run:
        _gh(
            "pr",
            "comment",
            str(args.pr),
            "--body",
            "Automated backport needs manual attention for: "
            f"{', '.join(stuck)} (cherry-pick did not apply cleanly). "
            f"Clean backport PRs opened for: {', '.join(opened) or 'none'}.",
            check=False,
        )
    for b in stuck:
        print(f"::warning::backport to {b} needs manual resolution")


# --------------------------------------------------------------------------
# Command
# --------------------------------------------------------------------------


def cmd_ci(args) -> int:
    """Analyze a merged commit and open a backport PR on the fork for every
    AFFECTED branch."""
    _assert_fork_remote(args.remote)
    fix = git("rev-parse", "--verify", f"{args.commit}^{{commit}}", check=False)
    if fix.returncode != 0:
        raise BackportError(f"commit '{args.commit}' not found in the checkout.")
    fix_sha = fix.stdout.strip()
    # If we were handed a merge commit (a PR merged with a merge commit instead of
    # squashed), its own diff-tree is empty -- the real change is on the merged-in
    # side. Re-point to the second parent (the PR head). Squash/normal commits have
    # a single parent and are unaffected. AWS-LC squash-merges, so this is mainly a
    # guard for forks / repos that use merge commits.
    parents = git("rev-list", "--parents", "-n", "1", fix_sha).stdout.split()
    if len(parents) > 2:  # sha + 2+ parent shas => merge commit
        merged_head = git("rev-parse", f"{fix_sha}^2").stdout.strip()
        print(
            f"note: {fix_sha[:10]} is a merge commit; analyzing the merged-in "
            f"commit {merged_head[:10]} instead."
        )
        fix_sha = merged_head
    subject = git("log", "-1", "--format=%s", fix_sha).stdout.strip()

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
        print("\nNo AFFECTED branches; nothing to backport.")
        return 0

    print(f"\nOpening backport PRs on '{args.remote}' for: {', '.join(targets)}\n")
    opened: List[str] = []
    conflicts: List[str] = []
    errors: List[str] = []
    for branch in targets:
        status, detail = cherry_pick_local(fix_sha, branch, fix_sha[:8])
        if status != "clean":
            print(f"  [!!] {branch}: {status} ({detail}) -> manual backport needed")
            (conflicts if status == "conflict" else errors).append(branch)
            continue
        url = _open_backport_pr(
            branch,
            detail,
            fix_sha,
            subject,
            args.pr,
            args.remote,
            decided_by.get(branch, ""),
            args.dry_run,
        )
        if url.startswith("error:"):
            print(f"  [??] {branch}: {url}")
            errors.append(branch)
        else:
            print(f"  [OK] {branch}: {url}")
            opened.append(branch)

    _ci_report(args, opened, conflicts, errors)
    return 0
