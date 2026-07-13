#!/usr/bin/env python3
"""
backport - local, patch-driven CLI for the AWS-LC backport bot.

Runs the deterministic impact engine locally and from a PATCH rather than a
merged commit, so an embargoed security fix can be assessed (and backported to
local branches) before any public code change.

Subcommands:

  analyze [patch]     Give every supported branch a definite verdict: AFFECTED /
                      NOT AFFECTED / ALREADY PATCHED. With no patch argument it
                      analyzes the repo's uncommitted diff (`git diff HEAD`);
                      otherwise it reads the given patch file, or `--commit <ref>`.
                      Deterministic checks (ancestry + patch-id + pre-image + file
                      presence) decide the clear branches; anything inconclusive
                      goes to the AI advisory, and if that is uncertain or
                      unavailable the branch is flagged AFFECTED for review --
                      never silently dropped. Saves the run for a later `apply`.

  apply [--all-affected | --branches ..]
                      Cherry-pick the patch onto the chosen branches as LOCAL
                      branches (backport/<branch>/<id>), reporting clean vs
                      conflict. Never pushes, opens a PR, or auto-merges.

  ci --commit <sha>   Post-merge automation (GitHub Actions): analyze a merged
                      commit and open a backport PR on the fork for every
                      AFFECTED branch. Fork remotes only.

  clear               Remove the saved run state.

The target AWS-LC checkout is selected with `--repo <path>`, the
`BACKPORT_REPO_PATH` environment variable, or (default) the checkout this tool
lives in. It must have the release branches fetched (origin/fips-*, origin/main).

Module map: main (this file) dispatches; gitutil = git plumbing + repo targeting;
patches = patch->commit + source resolution; runstate = analyze->apply cache;
verdicts = deterministic bucketing + AI passes; render = output; analyze/apply/ci
= the commands; engine + ai = the impact core.
"""

import argparse
import sys
from typing import Optional, Sequence

from analyze import cmd_analyze
from apply import cmd_apply, cmd_clear
from ci import cmd_ci
from common import BackportError
from gitutil import resolve_patch_path, target_repo


# --------------------------------------------------------------------------
# Argument parser
# --------------------------------------------------------------------------


def _add_common(p: argparse.ArgumentParser) -> None:
    """Flags shared by every subcommand."""
    p.add_argument(
        "--repo",
        help="path to the AWS-LC checkout to operate on (default: "
        "$BACKPORT_REPO_PATH, else the checkout this tool lives in)",
    )
    p.add_argument(
        "--base", help="base ref to apply the patch on (default origin/main)"
    )
    p.add_argument(
        "--3way",
        dest="three_way",
        action="store_true",
        help="use 3-way apply/am when the base has drifted",
    )


def _add_analyze(sub) -> None:
    p = sub.add_parser(
        "analyze", help="give an affected / not affected verdict for every branch"
    )
    p.add_argument(
        "patch",
        nargs="?",
        help="path to the fix patch (git diff or format-patch); omit to analyze "
        "the repo's current uncommitted diff (git diff HEAD)",
    )
    p.add_argument(
        "--commit",
        help="analyze an existing commit instead of a patch/working tree; the fix "
        "is reconstructed internally (base defaults to <commit>^)",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="skip the interactive test-file confirmation (for scripted/CI runs)",
    )
    p.add_argument("--branches", nargs="+", help="limit to these branches")
    p.add_argument(
        "--no-ai",
        action="store_true",
        help="deterministic only; do not consult the AI on inconclusive branches "
        "(they are flagged AFFECTED for review instead)",
    )
    p.add_argument(
        "--keep-patch",
        action="store_true",
        help="do not delete the patch file after analysis",
    )
    p.add_argument("--json", action="store_true", help="emit JSON")
    _add_common(p)
    p.set_defaults(func=cmd_analyze)


def _add_apply(sub) -> None:
    p = sub.add_parser("apply", help="cherry-pick the patch onto local branches")
    p.add_argument("--branches", nargs="+", help="branches to apply to")
    p.add_argument(
        "--all-affected", action="store_true", help="apply to every AFFECTED branch"
    )
    p.add_argument(
        "--commit",
        help="apply an existing commit instead of the last analyzed run "
        "(base defaults to <commit>^)",
    )
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument(
        "--keep-patch",
        action="store_true",
        help="do not delete the patch file / cached run after a clean apply",
    )
    _add_common(p)
    p.set_defaults(func=cmd_apply)


def _add_ci(sub) -> None:
    p = sub.add_parser(
        "ci",
        help="post-merge: open backport PRs on the fork for a merged commit",
    )
    p.add_argument("--commit", required=True, help="merged commit SHA to back-port")
    p.add_argument("--pr", help="source PR number (for cross-linking / comments)")
    p.add_argument(
        "--remote",
        default="origin",
        help="fork remote to push branches / open PRs on (default origin)",
    )
    p.add_argument(
        "--no-ai",
        action="store_true",
        help="deterministic only; do not consult the AI (default: AI on)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="analyze and cherry-pick locally but do not push or open PRs",
    )
    _add_common(p)
    p.set_defaults(func=cmd_ci, json=False)


def _add_clear(sub) -> None:
    p = sub.add_parser(
        "clear",
        help="remove the saved run state (.backport-runs/) from the tool folder",
    )
    _add_common(p)
    p.set_defaults(func=cmd_clear)


def build_parser() -> argparse.ArgumentParser:
    """Build the ``backport`` argument parser (analyze / apply / ci / clear)."""
    ap = argparse.ArgumentParser(
        prog="backport",
        description="Local, patch-driven AWS-LC backport impact analysis + apply.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    _add_analyze(sub)
    _add_apply(sub)
    _add_ci(sub)
    _add_clear(sub)
    return ap


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        repo_top = target_repo(args)
        resolve_patch_path(args, repo_top)
        return args.func(args)
    except BackportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
