"""
Git plumbing and repository targeting.

Everything that shells out to git lives here: the low-level ``_run``/``git``
wrappers, throwaway worktrees, the cherry-pick primitive shared by ``apply`` and
``ci``, the two ``git diff-tree`` parsers, and the logic that points the engine
at the right AWS-LC checkout.
"""

import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Set, Tuple

import engine as bot
from common import BackportError


# --------------------------------------------------------------------------
# Low-level command runners
# --------------------------------------------------------------------------


def run(
    args: Sequence[str],
    check: bool = True,
    cwd: Optional[str] = None,
    stdin: Optional[str] = None,
):
    """Run a command and capture its output.

    Defaults to the engine's configured repo path; an explicit *cwd* (used by the
    throwaway worktrees) always wins. *stdin* is fed to the command's standard
    input -- used to pipe a patch into ``git apply``/``git am``.
    """
    if cwd is None:
        cwd = bot.REPO_PATH
    p = subprocess.run(list(args), capture_output=True, text=True, cwd=cwd, input=stdin)
    if check and p.returncode != 0:
        raise BackportError(
            f"command failed: {' '.join(args)}\nstdout: {p.stdout}\nstderr: {p.stderr}"
        )
    return p


def git(
    *args: str,
    check: bool = True,
    cwd: Optional[str] = None,
    stdin: Optional[str] = None,
):
    """Run a git subcommand (thin wrapper over :func:`run`)."""
    return run(["git", *args], check=check, cwd=cwd, stdin=stdin)


def ref_exists(ref: str) -> bool:
    """True if *ref* resolves to an object in the repo."""
    return git("rev-parse", "--verify", "--quiet", ref, check=False).returncode == 0


@contextmanager
def temp_worktree(base: str, prefix: str = "backport-") -> "Iterator[str]":
    """Check out *base* in a throwaway detached ``git worktree`` and yield its path.

    This lets us apply a patch or cherry-pick into a clean tree without touching
    the user's working copy. On exit the worktree and its temp parent dir are
    removed; any commits made inside it survive in git's shared object store,
    which is all the engine and the caller need.
    """
    scratch_dir = tempfile.mkdtemp(prefix=prefix)
    worktree = os.path.join(scratch_dir, "wt")
    try:
        git("worktree", "add", "--detach", "--quiet", worktree, base)
        yield worktree
    finally:
        git("worktree", "remove", "--force", worktree, check=False)
        shutil.rmtree(scratch_dir, ignore_errors=True)
        git("worktree", "prune", check=False)


# --------------------------------------------------------------------------
# Cherry-pick primitive (shared by `apply` and `ci`)
# --------------------------------------------------------------------------


def _conflicted_files(wt: str) -> List[str]:
    """List the unmerged files after a conflicted cherry-pick, each tagged with a
    short kind (content conflict / modify-delete / add-add) so the user knows what
    they're walking into. Must be called before the tree is staged."""
    out = git("status", "--porcelain", cwd=wt).stdout
    files: List[str] = []
    for line in out.splitlines():
        xy, path = line[:2], line[3:].strip()
        if "U" in xy or xy in ("AA", "DD"):
            kind = (
                "content conflict"
                if xy == "UU"
                else "modify/delete" if "D" in xy else "add/add" if xy == "AA" else xy
            )
            files.append(f"{path} ({kind})")
    return files


def cherry_pick_local(
    fix_sha: str, branch: str, run_id: str
) -> "Tuple[str, str, List[str]]":
    """Cherry-pick *fix_sha* onto ``origin/<branch>`` in a throwaway worktree.

    Returns ``(status, detail, conflicts)``:
      - ``("clean", local_branch, [])`` -- applied cleanly; branch created.
      - ``("conflict", local_branch, [files])`` -- the half-applied state (clean
        files applied, conflict markers left in the clashing ones) is committed as
        a WIP commit on ``backport/<branch>/<run_id>`` so the user has a branch to
        resolve rather than nothing. *conflicts* tags each unmerged file.
      - ``("error", message, [])`` -- the branch/ref was missing or git failed.

    Never pushes or opens a PR.
    """
    ref = f"origin/{branch}"
    if not ref_exists(ref):
        return "error", f"{ref} not found", []
    local_branch = f"backport/{branch}/{run_id}"
    try:
        with temp_worktree(ref, prefix="backport-cp-") as wt:
            pick = git("cherry-pick", fix_sha, check=False, cwd=wt)
            conflicts: List[str] = []
            if pick.returncode != 0:
                # Keep the conflicted result instead of aborting: stage everything
                # (clean hunks + files with <<<<<<< markers) and commit it, so the
                # branch is a ready-to-resolve starting point.
                conflicts = _conflicted_files(wt)
                git("add", "-A", cwd=wt)
                git(
                    "-c",
                    "user.name=backport-cli",
                    "-c",
                    "user.email=backport-cli@local",
                    "commit",
                    "--no-verify",
                    "--quiet",
                    "-m",
                    f"WIP backport of {fix_sha[:10]} onto {branch} "
                    "(unresolved conflicts)",
                    cwd=wt,
                )
            new_sha = git("rev-parse", "HEAD", cwd=wt).stdout.strip()
            git("branch", "-f", local_branch, new_sha)
            return ("conflict" if conflicts else "clean"), local_branch, conflicts
    except BackportError as exc:
        return "error", str(exc), []


# --------------------------------------------------------------------------
# git diff-tree parsers
# --------------------------------------------------------------------------


def changed_files_with_status(commit: str) -> "Tuple[List[str], List[str]]":
    """Return ``(changed_files, traceable_files)`` for *commit*.

    ``git diff-tree --name-status`` prints one line per changed file, e.g.::

        M\tcrypto/aead.c          modified
        A\ttls/new_feature.c      added
        R100\told.c\tnew.c        renamed (the new path is the last column)

    - ``changed_files``: every path the fix touches.
    - ``traceable_files``: the same, minus files this fix *added* (status ``A``).
      A brand-new file has no prior history, so there is no introducing commit to
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


def branch_basenames(ref: str) -> Set[str]:
    """Set of file basenames present anywhere on *ref*.

    A conservative anti-false-negative guard: a same-named file under a path our
    rename trace missed means the code may still be on the branch.
    """
    out = bot._git(
        ["ls-tree", "-r", "--name-only", ref],
        check=False,
        capture_output=True,
        text=True,
    ).stdout
    return {os.path.basename(p) for p in out.splitlines() if p.strip()}


# --------------------------------------------------------------------------
# Repository targeting
# --------------------------------------------------------------------------


def resolve_repo_path(repo_arg: Optional[str]) -> str:
    """Resolve the target repo: ``--repo``, then ``$BACKPORT_REPO_PATH``, then the
    checkout this tool lives in.

    Because ``awslc-backport/`` sits inside the AWS-LC repo, the last default
    means ``--repo`` is optional when you run the tool from the repo.
    """
    return (
        repo_arg
        or os.environ.get("BACKPORT_REPO_PATH")
        or os.path.dirname(os.path.abspath(__file__))
    )


def target_repo(args) -> str:
    """Resolve + activate the AWS-LC checkout for this run.

    Confirms it is a git repo, points the engine at its top level, and chdir's
    there. Returns the top-level path; raises :class:`BackportError` if the path
    is not inside a git repository.
    """
    repo = resolve_repo_path(getattr(args, "repo", None))
    top = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if top.returncode != 0:
        raise BackportError(
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


def resolve_patch_path(args, repo_top) -> None:
    """Resolve a given patch file relative to the caller's cwd first, then the
    repo root (a common spot to drop a patch). Rewrites ``args.patch`` in place."""
    patch = getattr(args, "patch", None)
    if not patch:
        return
    given = Path(patch)
    if given.exists():
        args.patch = str(given.resolve())
    elif (Path(repo_top) / patch).exists():
        args.patch = str((Path(repo_top) / patch).resolve())
    else:
        raise BackportError(
            f"patch file not found: {patch}\n"
            f"  looked in the current directory and at the repo root ({repo_top})."
        )
