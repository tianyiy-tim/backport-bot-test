"""
The ``ci`` command: post-merge automation for GitHub Actions.

Given a merged commit, analyze every supported branch (AI layer on) and open a
backport PR on the fork for each AFFECTED branch. Clean cherry-picks become PRs
into the release branch (never auto-merged); conflicts/errors are reported and,
if ``--pr`` is given, flagged in a comment on the source PR. Refuses to target
upstream aws/aws-lc -- fork remotes only.
"""

import os
import re
from typing import Optional

import engine as bot
from common import AFFECTED, ALREADY, LABEL, NOT_AFFECTED, BackportError
from gitutil import cherry_pick_local, git, resolve_commit, run
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


def _test_only(conflicts) -> bool:
    """True if every conflicting path is a test/generated file (not real source),
    which usually means the source fix applied cleanly and only a test hunk clashed."""
    return bool(conflicts) and all(
        bot._is_test_or_generated_file(c["path"]) for c in conflicts
    )


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
    """Push a clean cherry-pick branch to the fork and open a normal PR into the
    release branch (never a draft, never auto-merged). Conflicted branches are not
    handled here -- CI only reports them, and the user runs ``backport resolve``.
    Returns the PR URL, ``"dry-run"``, or an ``"error: ..."`` string."""
    link = f" of #{source_pr}" if source_pr else ""
    title = f"[backport {branch}] {subject}"
    body = (
        f"Automated backport{link} (`{fix_sha[:12]}`) onto `{branch}`.\n\n"
        f"- Impact verdict: **AFFECTED** ({reason or 'deterministic'}).\n"
        "- Clean cherry-pick; **not** auto-merged -- please review before "
        "merging.\n\n"
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


def _backport_cell(state: str, outcome) -> str:
    """Render the 'Backport' column for one branch. *outcome* is (kind, value)."""
    if state == ALREADY:
        return "already applied"
    if state != AFFECTED or outcome is None:
        return "—"
    kind, value = outcome
    if kind == "dry-run":
        return "would open PR (dry-run)"
    if kind == "error":
        return f"⚠️ {value}"
    if kind == "conflict":
        names = ", ".join(f"`{os.path.basename(c['path'])}`" for c in value)
        suffix = " (test-only, likely trivial)" if _test_only(value) else ""
        return f"⚠️ merge conflict: {names} — resolve locally{suffix}"
    num = value.rstrip("/").rsplit("/", 1)[-1]
    return f"✅ [#{num}]({value})"


def _summary_table(
    fix_sha: str, subject: str, buckets, outcomes, source_pr=None
) -> str:
    """Build the markdown status table (AFFECTED branches first)."""
    order = {AFFECTED: 0, ALREADY: 1, NOT_AFFECTED: 2}
    rows = sorted(buckets.items(), key=lambda kv: order.get(kv[1], 9))

    def kind_of(b):
        return (outcomes.get(b) or (None, None))[0]

    opened = sum(1 for b in buckets if kind_of(b) == "opened")
    manual = sum(1 for b in buckets if kind_of(b) in ("conflict", "error"))
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
    if manual:
        target = f"--pr {source_pr}" if source_pr else f"--commit {fix_sha[:12]}"
        lines += [
            "",
            "> ℹ️ Conflicting branches were **not** modified. Resolve them "
            "interactively (walks each file, opens one PR per branch) with:\n"
            f"> `backport resolve {target}`",
        ]
    return "\n".join(lines)


def _ci_report(args, fix_sha, subject, buckets, outcomes) -> None:
    """Print the per-branch status table, post it as a comment on the source PR,
    and emit GitHub Actions warnings for branches that need manual backport."""
    table = _summary_table(fix_sha, subject, buckets, outcomes, source_pr=args.pr)
    print("\n" + table)
    if args.pr and not args.dry_run:
        _gh("pr", "comment", str(args.pr), "--body", table, check=False)
    for branch, outcome in outcomes.items():
        if outcome[0] in ("conflict", "error"):
            print(f"::warning::backport to {branch} needs manual resolution")


# --------------------------------------------------------------------------
# Command
# --------------------------------------------------------------------------


def cmd_ci(args) -> int:
    """Analyze a merged commit and open a backport PR on the fork for every
    AFFECTED branch."""
    _assert_fork_remote(args.remote)
    fix_sha, subject = resolve_commit(args.commit)

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

    print(f"\nBackporting to '{args.remote}' for: {', '.join(targets)}\n")
    outcomes = {}
    for branch in targets:
        status, detail, conflicts = cherry_pick_local(fix_sha, branch, fix_sha[:8])
        if status == "error":
            outcomes[branch] = ("error", detail)
            print(f"  [??] {branch}: error: {detail}")
            continue
        if status == "conflict":
            outcomes[branch] = ("conflict", conflicts)
            names = ", ".join(c["path"] for c in conflicts)
            tag = " (test-only)" if _test_only(conflicts) else ""
            print(
                f"  [!!] {branch}: merge conflict{tag} in {names} — "
                "resolve locally with `backport resolve`"
            )
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
            outcomes[branch] = ("error", url)
            print(f"  [??] {branch}: {url}")
        elif url == "dry-run":
            outcomes[branch] = ("dry-run", None)
        else:
            outcomes[branch] = ("opened", url)
            print(f"  [OK] {branch}: {url}")

    _ci_report(args, fix_sha, subject, buckets, outcomes)
    return 0
