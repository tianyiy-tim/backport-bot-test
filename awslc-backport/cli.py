"""
backport - local, patch-driven CLI for the AWS-LC backport bot.

Runs the same impact engine as :mod:`aws_lc_python_utils.backport.engine`, but
locally and from a PATCH rather than a merged commit, so an embargoed security
fix can be assessed (and backported to local branches) before any public code
change.

Subcommands:

  analyze [patch]     Give a definite verdict for every supported branch:
                      AFFECTED / NOT AFFECTED (or ALREADY PATCHED). With no
                      patch argument it analyzes the repo's current uncommitted
                      changes (`git diff HEAD`); otherwise it reads the given
                      patch file. The deterministic check (ancestry + patch-id + file presence)
                      decides the clear branches; for any branch it cannot
                      confirm, the AI advisory is consulted automatically to
                      decide. If the AI is uncertain or unavailable, the branch
                      is flagged AFFECTED for review, never silently dropped.
                      Flags: --no-ai (deterministic only, inconclusive ->
                      flagged AFFECTED). Saves the run. Before analyzing it
                      always asks you to confirm the patch's test file (AWS-LC
                      fixes ship a *_test.cc next to the change).

  apply [--all-affected | --branches ..]
                      Cherry-pick the patch onto the chosen branches in LOCAL
                      branches (backport/<branch>/<id>), reporting clean vs
                      conflict. Never pushes, opens a PR, or auto-merges.

Typical flow:

  # edit your fix in the AWS-LC checkout, then from inside it:
  backport analyze                                     # uses the current `git diff HEAD`
  backport apply --all-affected                        # local backport branches

  # or hand it an explicit patch from anywhere:
  git diff > fix.patch
  backport analyze fix.patch --repo <aws-lc>

The target AWS-LC checkout is selected with ``--repo <path>`` or the
``BACKPORT_REPO_PATH`` environment variable; if neither is given the current
working directory is used. The checkout must have the release branches fetched
(origin/AWS-LC-FIPS-*, origin/NetOS, origin/main).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Set, Tuple

# Reuse the deterministic engine (impact analysis, branch resolution). engine.py
# and ai.py live next to this file.
import engine as bot
from ai import ai_impact_analysis

# Run-state lives inside the target repo so that `analyze` -> `apply` reuse is
# scoped per-checkout (and never writes into the installed package directory).
_RUN_DIR_NAME = ".backport-runs"
_RUN_FILE_NAME = "last-run.json"


def _run_dir() -> Path:
    base = bot.REPO_PATH or os.getcwd()
    return Path(base) / _RUN_DIR_NAME


def _run_file() -> Path:
    return _run_dir() / _RUN_FILE_NAME


# --------------------------------------------------------------------------
# git helpers
# --------------------------------------------------------------------------


def _run(
    args: Sequence[str],
    check: bool = True,
    cwd: Optional[str] = None,
    stdin: Optional[str] = None,
):
    # Default every command to the engine's configured repo path; explicit cwd
    # (used by the throwaway worktrees) always wins. `stdin` is fed to the
    # command's standard input, used to pipe a patch into `git apply`/`git am`.
    if cwd is None:
        cwd = bot.REPO_PATH
    p = subprocess.run(list(args), capture_output=True, text=True, cwd=cwd, input=stdin)
    if check and p.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(args)}\nstdout: {p.stdout}\nstderr: {p.stderr}"
        )
    return p


def _git(
    *args: str,
    check: bool = True,
    cwd: Optional[str] = None,
    stdin: Optional[str] = None,
):
    return _run(["git", *args], check=check, cwd=cwd, stdin=stdin)


def _ref_exists(ref: str) -> bool:
    return _git("rev-parse", "--verify", "--quiet", ref, check=False).returncode == 0


@contextmanager
def _temp_worktree(base: str, prefix: str = "backport-") -> "Iterator[str]":
    """Check out *base* in a throwaway detached `git worktree` and yield its path.

    This lets us apply a patch or cherry-pick into a clean tree without touching
    the user's working copy. On exit the worktree and its temp parent dir are
    removed; any commits made inside it survive in git's shared object store,
    which is all the engine and the caller need.
    """
    scratch_dir = tempfile.mkdtemp(prefix=prefix)
    worktree = os.path.join(scratch_dir, "wt")
    try:
        _git("worktree", "add", "--detach", "--quiet", worktree, base)
        yield worktree
    finally:
        _git("worktree", "remove", "--force", worktree, check=False)
        shutil.rmtree(scratch_dir, ignore_errors=True)
        _git("worktree", "prune", check=False)


# --------------------------------------------------------------------------
# turning a patch into a real (temporary) commit
# --------------------------------------------------------------------------


def _is_format_patch(patch: str) -> bool:
    """True if this is `git format-patch` output rather than a plain `git diff`.

    `git format-patch` writes the patch as an email (it begins with a
    "From <sha> <date>" line and has Subject:/author headers); it is applied with
    `git am`. A plain `git diff` has no such header and is applied with
    `git apply`. We check the first line to decide which to use.
    """
    head = patch.lstrip().splitlines()[:1]
    return bool(head) and head[0].startswith("From ")


def _commit_message_from_patch(
    patch: str, fallback: str = "Backport candidate (from patch)"
) -> str:
    """Use the patch's `Subject:` line as the commit message, else a fallback."""
    for line in patch.splitlines():
        if line.startswith("Subject:"):
            return (
                line[len("Subject:") :].strip().lstrip("[PATCH] ").strip() or fallback
            )
    return fallback


@contextmanager
def commit_from_patch(
    patch: str, base: str, three_way: bool = False
) -> "Iterator[str]":
    """Make the patch into a real, temporary commit and yield its SHA.

    The engine works on commits, but the caller gives us a patch (a `git diff`
    or `git format-patch`, of a committed change or uncommitted edits, it does
    not matter). So we check out *base* in a throwaway `git worktree`, pipe the
    patch into `git apply`/`git am` there, commit it, and hand back the new
    commit's SHA. On exit the worktree is deleted; the commit object lingers in
    git's shared object store for the rest of the process, which is all the
    engine needs to read it.
    """
    if not _ref_exists(base):
        raise RuntimeError(
            f"base ref '{base}' not found. Fetch the mainline first "
            f"(git fetch origin) or pass --base <ref>."
        )

    # The patch is piped to git over stdin, so it never gets written to disk.
    with _temp_worktree(base) as worktree:
        committed = False
        if _is_format_patch(patch):
            am_args = ["am", "--3way"] if three_way else ["am"]
            am = _git(*am_args, check=False, cwd=worktree, stdin=patch)
            if am.returncode == 0:
                committed = True
            else:
                _git("am", "--abort", check=False, cwd=worktree)

        if not committed:
            apply_args = ["apply", "--3way"] if three_way else ["apply"]
            ap = _git(*apply_args, check=False, cwd=worktree, stdin=patch)
            if ap.returncode != 0:
                raise RuntimeError(
                    "patch did not apply onto "
                    f"{base}:\n{(ap.stderr or ap.stdout).strip()}\n"
                    "If your local mainline has drifted, retry with --3way."
                )
            _git("add", "-A", cwd=worktree)
            _git(
                "-c",
                "user.name=backport-cli",
                "-c",
                "user.email=backport-cli@local",
                "commit",
                "--quiet",
                "-m",
                _commit_message_from_patch(patch),
                cwd=worktree,
            )

        sha = _git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
        yield sha


# --------------------------------------------------------------------------
# impact analysis (deterministic bucketing)
# --------------------------------------------------------------------------

AFFECTED = "affected"
NOT_AFFECTED = "not_affected"
UNSURE = "unsure"
ALREADY = "already_patched"

_LABEL = {
    AFFECTED: "AFFECTED",
    NOT_AFFECTED: "not affected",
    UNSURE: "UNSURE",
    ALREADY: "already patched",
}


def _changed_files_with_status(commit: str) -> "Tuple[List[str], List[str]]":
    """Return (changed_files, traceable_files) for *commit*.

    `git diff-tree --name-status` prints one line per changed file, e.g.:
        M\tcrypto/aead.c          modified
        A\ttls/new_feature.c      added
        R100\told.c\tnew.c        renamed (the new path is the last column)

    - changed_files: every path the fix touches.
    - traceable_files: the same, minus files this fix *added* (status 'A'). A
      brand-new file has no prior history, so there is no introducing commit to
      trace for it; we exclude it so introducer detection does not choke.
    """
    output = bot._git(
        ["diff-tree", "--no-commit-id", "--name-status", "-r", commit],
        capture_output=True,
        text=True,
    ).stdout

    changed_files: List[str] = []
    traceable_files: List[str] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        columns = line.split("\t")
        status, path = columns[0], columns[-1]  # last column = the (new) path
        changed_files.append(path)
        if not status.startswith("A"):  # skip files added by this fix
            traceable_files.append(path)
    return changed_files, traceable_files


def _branch_basenames(ref: str) -> Set[str]:
    """Set of file basenames present anywhere on *ref*. Used as a conservative
    anti-false-negative guard: a same-named file under a path our rename trace
    missed means the code may still be on the branch."""
    out = bot._git(
        ["ls-tree", "-r", "--name-only", ref],
        check=False,
        capture_output=True,
        text=True,
    ).stdout
    return {os.path.basename(p) for p in out.splitlines() if p.strip()}


def bucket_branches(
    fix_sha: str, branches: Sequence[str]
) -> "Tuple[List[str], List[str], Dict[str, str]]":
    """Classify each branch deterministically (no AI).

    Returns (changed_files, sorted_introducers, buckets), where buckets maps each
    branch to one of AFFECTED / NOT_AFFECTED / UNSURE / ALREADY.

    Safety stance: a branch is only ever called NOT AFFECTED when we are
    confident the changed code is absent. If ancestry/patch-id do not match but
    the file is present (or a same-named file exists under a path we could not
    trace), the branch is escalated to UNSURE rather than risk a silent false
    negative. The only confident NOT AFFECTED is "the code is genuinely not here".
    """
    files, introducer_files = _changed_files_with_status(fix_sha)
    introducers = bot.find_introducing_commit(fix_sha, introducer_files)

    # Impact is judged on shipped SOURCE only: a co-changed *_test.cc / generated
    # file must never make a branch affected (its presence, or a stale line in it,
    # is not the vulnerable code). Fall back to all files only if the fix is
    # test/generated-only.
    src_files = [f for f in files if not bot._is_test_or_generated_file(f)] or files

    buckets: Dict[str, str] = {}
    for branch in branches:
        ref = f"origin/{branch}"
        affected, _ = bot.is_branch_affected(introducers, branch)  # Path 1 + Path 2
        # Corroborate ancestry/patch-id with the vulnerable pre-image. The
        # oldest-introducer heuristic flags a branch as soon as ONE introducer
        # reaches it, which over-flags when that introducer is old shared code the
        # fix also touched. `vulnerable_preimage_present` is the tiebreaker:
        #   True  -> the exact lines the fix removes are still here (real hit)
        #   None  -> pure-addition fix, nothing to check (trust ancestry)
        #   False -> those lines are provably absent (ancestry matched old shared
        #            code) -> NOT a confident AFFECTED; fall through to UNSURE so
        #            the AI decides (and it is flagged for review under --no-ai,
        #            never a silent miss).
        preimage = bot.vulnerable_preimage_present(fix_sha, src_files, ref)
        if affected and preimage is not False:
            buckets[branch] = (
                ALREADY if bot.is_already_patched(fix_sha, branch) else AFFECTED
            )
            continue
        # Path 2b: ancestry/patch-id missed (a branch-specific introducer), but the
        # exact removed lines ARE present -> deterministically AFFECTED.
        if not affected and preimage is True:
            buckets[branch] = AFFECTED
            continue
        # Not confidently affected. Decide UNSURE vs a confident NOT AFFECTED,
        # biasing hard toward UNSURE so a miss is never silent.
        present = any(
            bot._get_file_on_branch(f, ref, commit=fix_sha)[0] is not None
            for f in src_files
        )
        if not present:
            # Conservative guard: if the rename-aware lookup found nothing but a
            # file with the same name exists elsewhere on the branch, the code
            # may be there under a path we could not trace. Escalate to UNSURE
            # rather than declare a confident (and possibly false) NOT AFFECTED.
            basenames = _branch_basenames(ref)
            if any(os.path.basename(f) in basenames for f in src_files):
                present = True
        buckets[branch] = UNSURE if present else NOT_AFFECTED
    return files, sorted(introducers), buckets


# --------------------------------------------------------------------------
# run-state persistence
# --------------------------------------------------------------------------


def _save_run(
    patch: str,
    base: str,
    branches: Sequence[str],
    buckets: Dict[str, str],
    patch_path: Optional[str] = None,
) -> None:
    """Persist this analyze run so a later `apply` can reuse it without
    re-reading the patch. The diff is cached under the "patch" key; patch_path
    is the source file (if any) so `apply` can delete it on a clean run."""
    run_dir = _run_dir()
    run_dir.mkdir(parents=True, exist_ok=True)
    _run_file().write_text(
        json.dumps(
            {
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "base": base,
                "branches": list(branches),
                "buckets": buckets,
                "patch": patch,
                "patch_path": patch_path,
            },
            indent=2,
        )
    )


def _delete_patch_artifacts(patch_path: Optional[str]) -> List[str]:
    """Remove the source patch file and the saved run state (which embeds the
    patch text). Called after a clean apply so an embargoed diff does not linger
    on disk once the backport branches exist. Returns what was removed.
    """
    removed: List[str] = []
    if patch_path and os.path.isfile(patch_path):
        try:
            os.remove(patch_path)
            removed.append(patch_path)
        except OSError:
            pass
    run_file = _run_file()
    if run_file.exists():
        try:
            run_file.unlink()
            removed.append(str(run_file))
        except OSError:
            pass
    # Drop the now-empty run directory so nothing lingers in the checkout.
    run_dir = _run_dir()
    if run_dir.is_dir() and not any(run_dir.iterdir()):
        try:
            run_dir.rmdir()
            removed.append(str(run_dir))
        except OSError:
            pass
    return removed


def _load_run() -> dict:
    run_file = _run_file()
    if not run_file.exists():
        raise RuntimeError(
            "no saved run found. Run `backport analyze <patch>` first, "
            "or pass --patch <file>."
        )
    return json.loads(run_file.read_text())


def _resolve_patch_and_base(args) -> "Tuple[str, str, Optional[dict]]":
    """Return (patch, base, run) for `apply`.

    With an explicit --patch/--base, read those and skip the saved run. Otherwise
    reuse the patch and base cached by the last `analyze`.
    """
    if getattr(args, "commit", None):
        patch = _git("format-patch", "-1", "--stdout", args.commit).stdout
        return patch, (args.base or f"{args.commit}^"), None
    if getattr(args, "patch", None):
        patch = Path(args.patch).read_text()
        base = args.base or "origin/main"
        return patch, base, None
    run = _load_run()
    base = args.base or run.get("base", "origin/main")
    return run["patch"], base, run


def _is_empty_patch(patch: str) -> bool:
    """True if the patch has no diff content (blank file or only blank lines)."""
    return not patch.strip()


_TEST_SUFFIXES = ("_test.cc", "_test.cpp", "_test.c", "_test.cxx")


def _patch_paths(patch: str) -> Set[str]:
    """File paths touched by the patch, parsed from the diff headers."""
    paths: Set[str] = set()
    for line in patch.splitlines():
        if line.startswith("diff --git ") and " b/" in line:
            paths.add(line.split(" b/", 1)[1].strip())
        elif line.startswith("+++ b/"):
            p = line[len("+++ b/") :].strip()
            if p and p != "/dev/null":
                paths.add(p)
    return paths


def _ask_yn(prompt: str) -> bool:
    """Prompt until the user answers Y or N. Returns True for Y."""
    while True:
        try:
            ans = input(f"{prompt} [Y/N] ").strip().lower()
        except EOFError:
            # no input available (e.g. stdin closed) -> treat as a safe abort
            return False
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("Please answer Y or N.")


def _confirm_test_file(patch: str) -> bool:
    """Confirm the patch's test file before analysis. Returns True to proceed.

    AWS-LC tests usually live next to the fix as a `*_test.cc` file in the same
    diff. If one is present, confirm it is the right test; if none is present,
    confirm the user wants to proceed without one. Answering N aborts. This
    always prompts on the terminal and reads the answer.
    """
    tests = sorted(p for p in _patch_paths(patch) if p.endswith(_TEST_SUFFIXES))
    if tests:
        print(f"Test file found in the patch: {', '.join(tests)}")
        return _ask_yn("Is this the test file for your patch?")
    print("No test file (e.g. *_test.cc) found in the patch.")
    return _ask_yn("Proceed without a test file?")


def resolve_unsure(
    fix_sha: str,
    files: Sequence[str],
    introducers: Sequence[str],
    buckets: Dict[str, str],
    use_ai: bool = True,
) -> "Tuple[Dict[str, str], Dict[str, str], Dict[str, str]]":
    """Turn every UNSURE branch into a definite AFFECTED / NOT_AFFECTED verdict.

    The deterministic pass leaves a branch UNSURE when the fixed code is present
    but ancestry/patch-id can't confirm the introducer reached it. Rather than
    show that to the user, consult the AI advisory to decide.

    Safety: if the AI is uncertain, returns no answer, or is unavailable, the
    branch resolves to AFFECTED (flagged for review), never NOT_AFFECTED. So the
    automatic resolution can only over-flag, never create a silent miss.

    Returns (buckets, decided_by, summaries). decided_by[branch] is a one-line
    basis; summaries[branch] is the AI's reasoning for branches it judged.
    """
    decided_by: Dict[str, str] = {b: "deterministic" for b in buckets}
    summaries: Dict[str, str] = {}
    unsure = [b for b, s in buckets.items() if s == UNSURE]
    for branch in unsure:
        adv = (
            ai_impact_analysis(fix_sha, branch, files, set(introducers))
            if use_ai
            else None
        )
        if adv is None:
            buckets[branch] = AFFECTED
            decided_by[branch] = (
                "inconclusive, --no-ai -> flagged for review"
                if not use_ai
                else "inconclusive, AI unavailable -> flagged for review"
            )
        elif adv.get("likely_affected") is True:
            buckets[branch] = AFFECTED
            decided_by[branch] = f"AI: likely affected ({adv.get('confidence')})"
            summaries[branch] = adv.get("reasoning", "").strip()
        elif adv.get("likely_affected") is False:
            buckets[branch] = NOT_AFFECTED
            decided_by[branch] = f"AI: likely not affected ({adv.get('confidence')})"
            summaries[branch] = adv.get("reasoning", "").strip()
        else:
            buckets[branch] = AFFECTED
            decided_by[branch] = (
                f"AI: uncertain ({adv.get('confidence')}) -> flagged for review"
            )
            summaries[branch] = adv.get("reasoning", "").strip()
    return buckets, decided_by, summaries


def _commit_time(sha: str) -> int:
    """Unix commit timestamp of *sha* (0 if it can't be resolved). Used to pick
    the newest introducer."""
    out = _git("show", "-s", "--format=%ct", sha, check=False).stdout.strip()
    return int(out) if out.isdigit() else 0


def _suspect_affected_branches(
    introducers: Sequence[str], buckets: Dict[str, str]
) -> "Dict[str, Tuple[int, int]]":
    """AFFECTED branches that look like over-flags worth a second opinion.

    A branch is bucketed AFFECTED as soon as one introducer reaches it. When the
    fix also touches old, shared code (e.g. lines tracing back to the initial
    import), that lone match can be ancient and present on branches that predate
    the actual vulnerability -- the documented over-flag.

    The signal: the branch is missing the NEWEST introducer (the commit most
    likely to have written the actual bug) while still having some older lineage.
    A genuinely affected branch has that newest commit; one that predates the
    vulnerability does not. Returns {branch: (present_count, total)} for each
    candidate. Deterministic, no AI.
    """
    intro = list(introducers)
    suspects: "Dict[str, Tuple[int, int]]" = {}
    if len(intro) < 2:
        # A single introducer that reaches the branch is an unambiguous hit;
        # there is no old-vs-new lineage split to be suspicious about.
        return suspects
    newest = max(intro, key=_commit_time)
    intro_set = set(intro)
    for branch, state in buckets.items():
        if state != AFFECTED:
            continue
        present = bot.present_introducers(intro_set, branch)
        if present and newest not in present:
            suspects[branch] = (len(present), len(intro))
    return suspects


def review_suspect_affected(
    fix_sha: str,
    files: Sequence[str],
    introducers: Sequence[str],
    suspects: "Dict[str, Tuple[int, int]]",
    decided_by: Dict[str, str],
    summaries: Dict[str, str],
    use_ai: bool = True,
) -> None:
    """Attach a false-positive review note to over-flag-candidate AFFECTED
    branches (those from :func:`_suspect_affected_branches`), consulting the AI
    advisory when *use_ai*.

    CRITICAL: this is advisory only and NEVER changes the verdict. The branch
    stays AFFECTED even if the AI thinks it is a false positive -- we only
    annotate it for human review. So widening AI coverage here can reduce noise
    but can never turn a real hit into a silent miss (no false negatives).
    """
    intro = set(introducers)
    for branch, (present, total) in suspects.items():
        note = (
            f"affected via {present}/{total} introducers; newer commit(s) absent "
            "-> possible false positive, review"
        )
        if use_ai:
            adv = ai_impact_analysis(fix_sha, branch, files, intro)
            if adv is not None:
                conf = adv.get("confidence")
                if adv.get("likely_affected") is False:
                    note = (
                        "AFFECTED (deterministic) but AI suspects FALSE POSITIVE "
                        f"({conf}) -> confirm before skipping"
                    )
                elif adv.get("likely_affected") is True:
                    note = f"affected; AI confirms ({conf})"
                else:
                    note = f"affected; AI uncertain ({conf}) -> review"
                summaries[branch] = adv.get("reasoning", "").strip()
        decided_by[branch] = note


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _print_summary(
    fix_sha: str,
    files: Sequence[str],
    introducers: Sequence[str],
    buckets: Dict[str, str],
    decided_by: Dict[str, str],
) -> None:
    print(f"Fix commit (built from patch): {fix_sha[:10]}")
    print(f"Changed files: {list(files)}")
    print(f"Introducer(s): {[s[:8] for s in introducers] or '(none / new file)'}")
    print()
    print(f"  {'branch':<22} {'status':<16} basis")
    print(f"  {'-' * 22} {'-' * 16} {'-' * 40}")
    # Show AFFECTED first (the actionable branches), then the rest; buckets are
    # already newest-first, and the sort is stable, so each group keeps that order.
    _ORDER = {AFFECTED: 0, UNSURE: 1, ALREADY: 2, NOT_AFFECTED: 3}
    for branch, state in sorted(buckets.items(), key=lambda kv: _ORDER.get(kv[1], 9)):
        print(f"  {branch:<22} {_LABEL[state]:<16} {decided_by.get(branch, '')}")

    def names(state):
        return [b for b, s in buckets.items() if s == state]

    print()
    aff, no, pat = names(AFFECTED), names(NOT_AFFECTED), names(ALREADY)
    print(f"Affected (need backport): {', '.join(aff) or '-'}")
    print(f"Not affected: {', '.join(no) or '-'}")
    if pat:
        print(f"Already patched (skip): {', '.join(pat)}")


# --------------------------------------------------------------------------
# subcommands
# --------------------------------------------------------------------------


def _read_patch(args) -> "Tuple[str, str, bool]":
    """Return (patch, base, from_file) for `analyze`.

    Sources, in priority order:
      --commit <ref>  reconstruct the fix from an existing commit internally
                      (base defaults to <ref>^); handy for testing a past fix.
      <patch file>    read the given git diff / format-patch (base origin/main).
      (nothing)       capture the repo's uncommitted diff `git diff HEAD`
                      (base = current HEAD) -- the normal pre-merge flow.
    """
    if getattr(args, "commit", None):
        patch = _git("format-patch", "-1", "--stdout", args.commit).stdout
        return patch, (args.base or f"{args.commit}^"), False
    if args.patch:
        return Path(args.patch).read_text(), (args.base or "origin/main"), True
    diff = _git("diff", "HEAD").stdout
    base = args.base or _git("rev-parse", "HEAD").stdout.strip()
    return diff, base, False


def _resolve_inconclusive(args, fix_sha, files, introducers, buckets):
    """Decide the UNSURE branches via the AI advisory (unless --no-ai).

    Returns (buckets, decided_by, summaries), printing a one-line notice when
    the AI is about to be consulted.
    """
    unsure = [b for b, s in buckets.items() if s == UNSURE]
    use_ai = not args.no_ai
    if unsure and use_ai and not args.json:
        print(
            f"{len(unsure)} branch(es) inconclusive by git history; "
            f"consulting AI to decide...\n",
            file=sys.stderr,
        )
    buckets, decided_by, summaries = resolve_unsure(
        fix_sha, files, introducers, buckets, use_ai=use_ai
    )

    # Second pass: AFFECTED branches matched only by a partial introducer lineage
    # are likely over-flags (old shared code present, newer vulnerable commit
    # absent). Flag them for review -- consulting AI when enabled -- but never
    # change the verdict, so this can only reduce noise, never cause a miss.
    suspects = _suspect_affected_branches(introducers, buckets)
    if suspects:
        if use_ai and not args.json:
            print(
                f"{len(suspects)} AFFECTED branch(es) match only part of the fix's "
                "lineage (possible over-flag); consulting AI for a review note...\n",
                file=sys.stderr,
            )
        review_suspect_affected(
            fix_sha, files, introducers, suspects, decided_by, summaries, use_ai=use_ai
        )
    return buckets, decided_by, summaries


def _emit_analysis(
    as_json, fix_sha, base, files, introducers, buckets, decided_by, summaries
) -> None:
    """Print the analysis result, as JSON or as the human-readable table."""
    if as_json:
        print(
            json.dumps(
                {
                    "fix_commit": fix_sha,
                    "base": base,
                    "changed_files": files,
                    "introducers": introducers,
                    "buckets": buckets,
                    "decided_by": decided_by,
                    "summaries": summaries,
                },
                indent=2,
            )
        )
    else:
        _print_summary(fix_sha, files, introducers, buckets, decided_by)


def _delete_analyze_patch(args) -> None:
    """Delete the source patch file once analysis is done (unless --keep-patch).

    The diff is still cached in the run file for a later `apply`. Nothing to do
    when the diff was captured from the working tree (no file).
    """
    if args.patch and not args.keep_patch:
        try:
            os.remove(args.patch)
            print(f"\nDeleted patch file: {args.patch}")
        except OSError:
            pass


def cmd_analyze(args) -> int:
    """Give an affected / not affected verdict for every supported branch.

    Pipeline: read the patch (explicit file, or the working-tree diff) -> confirm
    the test file -> bucket each branch deterministically -> let the AI decide
    the inconclusive ones -> print -> save the run -> delete the patch file.
    """
    patch, base, from_file = _read_patch(args)

    if _is_empty_patch(patch):
        if from_file:
            print("patch is empty; nothing to analyze.")
        else:
            print(
                "No uncommitted changes to analyze (git diff HEAD is empty). "
                "Make your fix in the repo first, `git add` any new files, or "
                "pass a patch file."
            )
        return 0

    if not args.yes and not _confirm_test_file(patch):
        print("Aborted. Re-run when your patch is ready.")
        return 0

    branches = bot.sort_branches(args.branches or bot.get_supported_branches())
    if not branches:
        print(
            "No supported branches found. Is this an AWS-LC clone with the "
            "release branches fetched (git fetch origin)?",
            file=sys.stderr,
        )
        return 1

    with commit_from_patch(patch, base, three_way=args.three_way) as fix_sha:
        files, introducers, buckets = bucket_branches(fix_sha, branches)
        buckets, decided_by, summaries = _resolve_inconclusive(
            args, fix_sha, files, introducers, buckets
        )
        _emit_analysis(
            args.json, fix_sha, base, files, introducers, buckets, decided_by, summaries
        )

    _save_run(patch, base, branches, buckets, patch_path=args.patch)
    _delete_analyze_patch(args)
    return 0


def _cherry_pick_local(fix_sha: str, branch: str, run_id: str) -> "Tuple[str, str]":
    """Cherry-pick fix_sha onto origin/<branch> in a throwaway worktree.

    On a clean apply, create a local branch backport/<branch>/<run_id> at the
    result and return ("clean", branch_name). On conflict, abort and return
    ("conflict", first_conflict_line). Never pushes or opens a PR.
    """
    ref = f"origin/{branch}"
    if not _ref_exists(ref):
        return "error", f"{ref} not found"
    try:
        with _temp_worktree(ref, prefix="backport-cp-") as wt:
            pick = _git("cherry-pick", fix_sha, check=False, cwd=wt)
            if pick.returncode == 0:
                new_sha = _git("rev-parse", "HEAD", cwd=wt).stdout.strip()
                local_branch = f"backport/{branch}/{run_id}"
                _git("branch", "-f", local_branch, new_sha)
                return "clean", local_branch
            _git("cherry-pick", "--abort", check=False, cwd=wt)
            combined = pick.stdout + pick.stderr
            first = next(
                (ln for ln in combined.splitlines() if "conflict" in ln.lower()),
                "conflict",
            )
            return "conflict", first.strip()
    except RuntimeError as exc:
        return "error", str(exc)


def _run_cherry_picks(
    fix_sha: str, targets: Sequence[str]
) -> "Tuple[List[str], List[str], List[str]]":
    """Cherry-pick the fix onto each target branch, printing per-branch status.

    Returns (clean, conflict, errors) as lists of branch names.
    """
    run_id = fix_sha[:8]
    clean: List[str] = []
    conflict: List[str] = []
    errors: List[str] = []
    for branch in targets:
        status, detail = _cherry_pick_local(fix_sha, branch, run_id)
        if status == "clean":
            print(f"  [OK] {branch}  ->  {detail}")
            clean.append(branch)
        elif status == "conflict":
            print(f"  [!!] {branch}  ->  conflict: {detail}")
            conflict.append(branch)
        else:
            print(f"  [??] {branch}  ->  error: {detail}")
            errors.append(branch)
    return clean, conflict, errors


def _cleanup_after_apply(args, run, conflict, errors) -> None:
    """After a fully clean apply, delete the patch file and cached run state.

    Once the backport branches exist there is no reason to keep an embargoed
    diff lying around on disk. If any branch conflicted or errored we keep
    everything so the user can fix it and re-run. --keep-patch opts out.
    """
    if args.keep_patch or conflict or errors:
        return
    patch_path = run.get("patch_path") if run else getattr(args, "patch", None)
    removed = _delete_patch_artifacts(patch_path)
    if removed:
        print("\nCleaned up (clean apply): " + ", ".join(removed))


def cmd_apply(args) -> int:
    """Cherry-pick the patch onto local branches (never pushes / opens a PR).

    Targets come from --branches, or --all-affected (the AFFECTED branches from
    the last analyze). Each clean pick lands as a local backport/<branch>/<id>
    branch; conflicts are reported, never auto-resolved.
    """
    patch, base, run = _resolve_patch_and_base(args)
    if _is_empty_patch(patch):
        print("patch is empty; nothing to apply.")
        return 0

    with commit_from_patch(patch, base, three_way=args.three_way) as fix_sha:
        branches = run["branches"] if run else bot.get_supported_branches()
        buckets = run["buckets"] if run else bucket_branches(fix_sha, branches)[2]

        # Choose which branches to cherry-pick onto (always chronological).
        if args.branches:
            targets = bot.sort_branches(args.branches)
        elif args.all_affected:
            targets = bot.sort_branches(b for b, s in buckets.items() if s == AFFECTED)
        else:
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

    print(
        "\nNothing was pushed or merged. Inspect `git branch --list 'backport/*'`, "
        "then push and open PRs for human review when ready."
    )
    return 0


def cmd_clear(args) -> int:
    """Remove the saved run state (.backport-runs/) from the target repo."""
    run_dir = _run_dir()
    if run_dir.exists():
        shutil.rmtree(run_dir, ignore_errors=True)
        print(f"Removed {run_dir}")
    else:
        print(f"Nothing to clear ({run_dir} does not exist).")
    return 0


# --------------------------------------------------------------------------
# entrypoint
# --------------------------------------------------------------------------


def _resolve_repo_path(repo_arg: Optional[str]) -> Optional[str]:
    """Resolve the target repo from --repo, then BACKPORT_REPO_PATH, then cwd."""
    return repo_arg or os.environ.get("BACKPORT_REPO_PATH") or None


def _build_parser() -> argparse.ArgumentParser:
    """Build the `backport` argument parser (analyze / apply / clear)."""
    ap = argparse.ArgumentParser(
        prog="backport",
        description="Local, patch-driven AWS-LC backport impact analysis + apply.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p):
        p.add_argument(
            "--repo",
            help="path to the AWS-LC checkout to operate on "
            "(default: $BACKPORT_REPO_PATH, else the current directory)",
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

    pa = sub.add_parser(
        "analyze", help="give an affected / not affected verdict for every branch"
    )
    pa.add_argument(
        "patch",
        nargs="?",
        help="path to the fix patch (git diff or format-patch); omit to analyze "
        "the repo's current uncommitted diff (git diff HEAD)",
    )
    pa.add_argument(
        "--commit",
        help="analyze an existing commit instead of a patch/working tree; the fix "
        "is reconstructed internally (base defaults to <commit>^)",
    )
    pa.add_argument(
        "--yes",
        action="store_true",
        help="skip the interactive test-file confirmation (for scripted/CI runs)",
    )
    pa.add_argument("--branches", nargs="+", help="limit to these branches")
    pa.add_argument(
        "--no-ai",
        action="store_true",
        help="deterministic only; do not consult the AI on inconclusive branches "
        "(they are flagged AFFECTED for review instead)",
    )
    pa.add_argument(
        "--keep-patch",
        action="store_true",
        help="do not delete the patch file after analysis",
    )
    pa.add_argument("--json", action="store_true", help="emit JSON")
    add_common(pa)
    pa.set_defaults(func=cmd_analyze)

    pp = sub.add_parser("apply", help="cherry-pick the patch onto local branches")
    pp.add_argument("--branches", nargs="+", help="branches to apply to")
    pp.add_argument(
        "--all-affected", action="store_true", help="apply to every AFFECTED branch"
    )
    pp.add_argument(
        "--commit",
        help="apply an existing commit instead of the last analyzed run "
        "(base defaults to <commit>^)",
    )
    pp.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    pp.add_argument(
        "--keep-patch",
        action="store_true",
        help="do not delete the patch file / cached run after a clean apply",
    )
    add_common(pp)
    pp.set_defaults(func=cmd_apply)

    pc = sub.add_parser(
        "clear", help="remove the saved run state (.backport-runs/) from the repo"
    )
    add_common(pc)
    pc.set_defaults(func=cmd_clear)
    return ap


def _target_repo(args) -> str:
    """Resolve the AWS-LC checkout (--repo / $BACKPORT_REPO_PATH / cwd), confirm it
    is a git repo, point the engine at its top level, and chdir there. Returns the
    repo's top-level path; raises RuntimeError if it isn't a git repository."""
    repo = _resolve_repo_path(getattr(args, "repo", None)) or os.getcwd()
    top = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if top.returncode != 0:
        raise RuntimeError(
            f"'{repo}' is not inside a git repository "
            "(use --repo <path> or set BACKPORT_REPO_PATH)."
        )
    repo_top = top.stdout.strip()
    bot.set_repo_path(repo_top)
    # The engine's internal git calls use the process working directory, so point
    # it at the repo. Throwaway worktrees always pass an explicit cwd, so they are
    # unaffected.
    os.chdir(repo_top)
    return repo_top


def _resolve_patch_path(args, repo_top) -> None:
    """If a patch file was given, resolve it relative to the caller's cwd first,
    then relative to the repo root (a common spot to drop a patch)."""
    patch = getattr(args, "patch", None)
    if not patch:
        return
    given = Path(patch)
    if given.exists():
        args.patch = str(given.resolve())
    elif (Path(repo_top) / patch).exists():
        args.patch = str((Path(repo_top) / patch).resolve())
    else:
        raise RuntimeError(
            f"patch file not found: {patch}\n"
            f"  looked in the current directory and at the repo root ({repo_top})."
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        repo_top = _target_repo(args)
        _resolve_patch_path(args, repo_top)
        return args.func(args)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
