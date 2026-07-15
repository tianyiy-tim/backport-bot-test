"""
The ``ci`` command: post-merge automation for GitHub Actions.

Given a merged commit, analyze every supported branch (AI layer on) and open a
backport PR on the fork for each AFFECTED branch. Clean cherry-picks become PRs
into the release branch (never auto-merged); conflicts/errors are reported and,
if ``--pr`` is given, flagged in a comment on the source PR. Refuses to target
upstream aws/aws-lc -- fork remotes only.
"""

import re
from typing import Optional

import engine as bot
from common import AFFECTED, ALREADY, LABEL, NOT_AFFECTED, BackportError
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


def _backport_cell(state: str, outcome: Optional[str]) -> str:
    """Render the 'Backport' column for one branch."""
    if state == ALREADY:
        return "already applied"
    if state != AFFECTED:
        return "—"
    if outcome is None:
        return "—"
    if outcome == "dry-run":
        return "would open PR (dry-run)"
    if outcome == "conflict":
        return "⚠️ conflict — needs manual backport"
    if outcome.startswith("error"):
        return f"⚠️ {outcome}"
    # otherwise it's a PR URL
    num = outcome.rstrip("/").rsplit("/", 1)[-1]
    return f"✅ [#{num}]({outcome})"


def _summary_table(fix_sha: str, subject: str, buckets, outcomes) -> str:
    """Build the markdown status table (AFFECTED branches first)."""
    order = {AFFECTED: 0, ALREADY: 1, NOT_AFFECTED: 2}
    rows = sorted(buckets.items(), key=lambda kv: order.get(kv[1], 9))

    opened = sum(
        1
        for b, s in buckets.items()
        if s == AFFECTED and (outcomes.get(b) or "").startswith("http")
    )
    manual = sum(
        1
        for b, s in buckets.items()
        if s == AFFECTED
        and (
            outcomes.get(b) == "conflict" or (outcomes.get(b) or "").startswith("error")
        )
    )
    not_aff = sum(1 for s in buckets.values() if s == NOT_AFFECTED)
    already = sum(1 for s in buckets.values() if s == ALREADY)

    lines = [
        f"### 🔁 Backport bot — {subject}",
        "",
        f"Analyzed `{fix_sha[:12]}` across {len(buckets)} supported branches. "
        "Nothing is auto-merged — every backport PR needs human review.",
        "",
        "| Branch | Impact | Backport |",
        "| --- | --- | --- |",
    ]
    for branch, state in rows:
        lines.append(
            f"| `{branch}` | {LABEL[state]} | "
            f"{_backport_cell(state, outcomes.get(branch))} |"
        )
    lines += [
        "",
        f"**{opened} opened · {manual} need manual backport · "
        f"{not_aff} not affected · {already} already applied**",
    ]
    return "\n".join(lines)


def _ci_report(args, fix_sha, subject, buckets, outcomes) -> None:
    """Print the per-branch status table, post it as a comment on the source PR,
    and emit GitHub Actions warnings for branches that need manual backport."""
    table = _summary_table(fix_sha, subject, buckets, outcomes)
    print("\n" + table)
    if args.pr and not args.dry_run:
        _gh("pr", "comment", str(args.pr), "--body", table, check=False)
    for branch, state in buckets.items():
        outcome = outcomes.get(branch) or ""
        if state == AFFECTED and (outcome == "conflict" or outcome.startswith("error")):
            print(f"::warning::backport to {branch} needs manual resolution")


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
    outcomes = {}
    for branch in targets:
        status, detail = cherry_pick_local(fix_sha, branch, fix_sha[:8])
        if status != "clean":
            outcomes[branch] = (
                "conflict" if status == "conflict" else f"error: {detail}"
            )
            print(f"  [!!] {branch}: {status} ({detail}) -> manual backport needed")
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
        outcomes[branch] = url
        print(f"  [{'??' if url.startswith('error:') else 'OK'}] {branch}: {url}")

    _ci_report(args, fix_sha, subject, buckets, outcomes)
    return 0
