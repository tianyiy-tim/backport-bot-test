"""
Backport engine: the deterministic core.

Branch resolution, git/text helpers, impact analysis (is_branch_affected),
already-patched detection, and the vulnerable-pre-image check. The advisory AI
layer is in ai.py; the pre-merge CLI (analyze/apply/ci) is split across main.py
and its helper modules. Every git command runs against the configured REPO_PATH
(set via set_repo_path), so the engine can target an arbitrary AWS-LC checkout
without chdir.

Sections, top to bottom:
  1. Repository targeting          set_repo_path / _run / _git
  2. Caches, constants & config    tunable knobs and per-process caches
  3. Supported-branch resolution   which release branches to consider
  4. Text / line normalizers       comment- and whitespace-aware helpers
  5. Vulnerable pre-image          are the fix's removed lines still present?
  6. Git file access               rename-aware file/diff reads
  7. Introducer tracing            which commit(s) wrote the changed lines
  8. Impact verdict                is_branch_affected / present_introducers
  9. Already-patched / patch-id    skip branches that already carry the fix
"""

import json
import os
import re
import subprocess
import sys
from datetime import date, datetime

# ---------------------------------------------------------------------------
# 1. Repository targeting
# ---------------------------------------------------------------------------

# Absolute path to the AWS-LC checkout every git command runs against. None means
# "use the process working directory" (used by the replay test harness, which
# chdirs into a sandbox).
REPO_PATH = None


def set_repo_path(path):
    """Point the engine at an AWS-LC checkout; None restores the cwd fallback."""
    global REPO_PATH
    REPO_PATH = os.path.abspath(path) if path else None


def _run(cmd, **kwargs):
    """Run a command against REPO_PATH (unless an explicit cwd is given)."""
    if REPO_PATH is not None and kwargs.get("cwd") is None:
        kwargs["cwd"] = REPO_PATH
    return subprocess.run(list(cmd), **kwargs)


def _git(args, **kwargs):
    """Run a git subcommand against REPO_PATH."""
    return _run(["git", *args], **kwargs)


# ---------------------------------------------------------------------------
# 2. Caches, constants & configuration
# ---------------------------------------------------------------------------

_AI_MAX_DIFF_BYTES = 40_000  # cap diff bytes fed to the model
_AI_MAX_FILE_BYTES = 45_000  # cap per-file context bytes fed to the model

# Per-process caches for the pre-image work, which repeats identical git calls
# within one analysis. Keys are prefixed with the unique fix SHA, so entries
# never collide across fixes/sandboxes.
_REMOVED_LINES_CACHE: "dict[tuple, list]" = {}
_PREIMAGE_CACHE: "dict[tuple, object]" = {}


# Auto-generated/derived files (e.g. generated-src/). They are regenerated
# per-branch, so their bytes differ between a fix and its backport even when the
# real source change is identical -- including them in patch-id matching would
# flag an already-applied backport as novel. Overridable via env (comma-separated).
_GENERATED_PATHSPECS = [
    p.strip()
    for p in os.environ.get("BACKPORT_GENERATED_PATHS", "generated-src").split(",")
    if p.strip()
]


def _patch_id_pathspec():
    """Git pathspec keeping every file except the generated ones, so a patch-id
    reflects only human-authored source. Returns [] when nothing is excluded."""
    if not _GENERATED_PATHSPECS:
        return []
    return ["--", "."] + [f":(exclude){p}" for p in _GENERATED_PATHSPECS]


# Bedrock cross-region inference profile; verify the ID in the AWS console.
_BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-opus-4-8")

# Prefixes matched against `origin/<branch>` when there is no manifest. Covers
# real release branches (fips-YYYY-MM-DD, fips-NetOS-*) and the POC fixture.
SUPPORTED_BRANCH_PREFIXES = tuple(
    p.strip()
    for p in os.environ.get(
        "BACKPORT_BRANCH_PREFIXES",
        "origin/fips-,origin/AWS-LC-FIPS-,origin/NetOS",
    ).split(",")
    if p.strip()
)

# FIPS/LTS branch manifest (kept in sync with VERSIONING.md). When present it is
# the source of truth for which branches are supported and their end-of-support;
# when absent we fall back to prefix matching.
VERSIONS_MANIFEST_PATH = os.environ.get(
    "BACKPORT_VERSIONS_MANIFEST", "fips_versions.json"
)


# ---------------------------------------------------------------------------
# 3. Supported-branch resolution
# ---------------------------------------------------------------------------


def _remote_branch_names():
    """Branch names (without the `origin/` prefix) from `git branch -r`,
    skipping the symbolic `origin/HEAD -> origin/main` ref."""
    result = subprocess.run(["git", "branch", "-r"], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git branch -r failed: {result.stderr}")
    names = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if " -> " in line or not line.startswith("origin/"):
            continue
        names.append(line[len("origin/") :])
    return names


def load_versions_manifest():
    """Load the FIPS branch manifest (`VERSIONS_MANIFEST_PATH`), or None if absent.

    Looks in the working tree first, then at the file as it exists on the mainline
    ref (so it still works from a feature branch). A present-but-malformed file
    logs a warning and returns None so we fall back to prefix matching.
    """
    text = None
    on_disk = os.path.join(os.getcwd(), VERSIONS_MANIFEST_PATH)
    if os.path.isfile(on_disk):
        try:
            with open(on_disk, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            text = None
    if text is None:
        mainline = os.environ.get("BACKPORT_MAINLINE_REF", "origin/main")
        show = subprocess.run(
            ["git", "show", f"{mainline}:{VERSIONS_MANIFEST_PATH}"],
            capture_output=True,
            text=True,
        )
        if show.returncode == 0:
            text = show.stdout
    if not text or not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        print(
            f"[versions] {VERSIONS_MANIFEST_PATH} is present but not valid JSON "
            f"({exc}); falling back to branch-prefix matching.",
            file=sys.stderr,
        )
        return None


def _parse_eos_date(value):
    """Parse an end-of-support date (`YYYY-MM-DD` or `YYYY-MM`). Returns None if
    missing/unparseable, which callers treat as "no known EOS" (still supported)."""
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            return datetime.strptime((value or "").strip(), fmt).date()
        except ValueError:
            continue
    return None


def branch_support_status(today=None):
    """Per-branch support records derived from the manifest.

    Each record is the manifest entry plus `end_of_support_date`, `exists`
    (present as an origin/ ref), and `supported` (exists AND actively_maintained
    AND not past end_of_support as of `today`). Returns [] when no manifest.

    `today` is overridable so a historical replay can ask "was this branch in
    support as of the fix date?" rather than only "is it in support now?".
    """
    manifest = load_versions_manifest()
    if not manifest:
        return []
    today = today or date.today()
    remote = set(_remote_branch_names())
    records = []
    for entry in manifest.get("fips_branches", []):
        name = entry.get("branch")
        if not name:
            continue
        eos = _parse_eos_date(entry.get("end_of_support"))
        within_window = eos is None or eos >= today
        maintained = entry.get("actively_maintained", True)
        record = dict(entry)
        record["end_of_support_date"] = eos.isoformat() if eos else None
        record["exists"] = name in remote
        record["supported"] = bool(record["exists"] and maintained and within_window)
        records.append(record)
    return records


def _branch_date_key(name):
    """The YYYY-MM-DD embedded in *name*, or '' if none. Used to order branches."""
    m = re.search(r"\d{4}-\d{2}-\d{2}", name)
    return m.group(0) if m else ""


def sort_branches(names):
    """Order branches newest -> oldest by the date in their name (undated last).
    The single source of truth for branch ordering, so every listing matches."""
    return sorted(
        names,
        key=lambda n: (_branch_date_key(n) or "0000-00-00", n),
        reverse=True,
    )


def get_supported_branches(today=None):
    """Branch names (without `origin/`) to consider for backport, newest -> oldest.
    From the manifest when present (supported = exists as a ref, actively
    maintained, not past end-of-support), else branch-name prefix matching."""
    records = branch_support_status(today=today)
    if records:
        dropped = [r["branch"] for r in records if r["exists"] and not r["supported"]]
        if dropped:
            print(
                "[versions] skipping out-of-support branch(es) per "
                f"{VERSIONS_MANIFEST_PATH}: {', '.join(dropped)}",
                file=sys.stderr,
            )
        supported = [r["branch"] for r in records if r["supported"]]
    else:
        supported = [
            name
            for name in _remote_branch_names()
            if f"origin/{name}".startswith(SUPPORTED_BRANCH_PREFIXES)
        ]
    return sort_branches(supported)


def get_changed_files(commit):
    """Files changed by the fix commit (vs. its parent)."""
    result = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff-tree failed: {result.stderr}")

    files = []

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        files.append(line)

    return files


# ---------------------------------------------------------------------------
# 4. Text / line normalizers
# ---------------------------------------------------------------------------


def _norm_ws(s):
    """Collapse runs of whitespace so a reformatted line still matches."""
    return re.sub(r"\s+", " ", s).strip()


_C_FAMILY_EXT = (".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hh", ".hxx")


def _is_c_file(file):
    """True for C/C++ source/headers, where '#' is a preprocessor directive
    (real code), not a comment."""
    return file is not None and file.lower().endswith(_C_FAMILY_EXT)


def _is_noise_line(s, file=None):
    """True for lines with no vulnerable-code signal: comments, blanks, pure
    punctuation/braces. '#' is a comment only in non-C files; in C/C++ it is a
    preprocessor directive (real code) and is kept."""
    s = s.strip()
    if not s:
        return True
    if s.startswith(("//", "/*", "*/", "*")):  # C/C++ comments
        return True
    if s.startswith("#") and not _is_c_file(file):  # script/config comment
        return True
    if set(s) <= set("{}();,: \t"):  # punctuation only
        return True
    return False


def _is_boilerplate_line(s):
    """True for real-but-undistinctive lines (bare control-flow, #include, a lone
    string literal) that match too many files to be a reliable pre-image. Skipping
    them only weakens a match, so it is false-negative safe."""
    s = s.strip()
    if re.match(r"^(return|break|continue|goto)\b[^;{}]*;?$", s):
        return True
    if s.startswith("#include"):
        return True
    # Substance is only a string/char literal: strip quoted spans, require enough
    # remaining alnum to be distinctive.
    without_strings = re.sub(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'', "", s)
    if len(re.sub(r"\W", "", without_strings)) < 6:
        return True
    return False


# ---------------------------------------------------------------------------
# 5. Vulnerable pre-image (are the fix's removed lines still on a branch?)
# ---------------------------------------------------------------------------


def _fix_removed_lines(commit, file):
    """The distinctive lines the fix removes/changes for *file* (the vulnerable
    pre-image), skipping comments, blanks, punctuation, and boilerplate."""
    cache_key = (commit, file)
    if cache_key in _REMOVED_LINES_CACHE:
        return _REMOVED_LINES_CACHE[cache_key]
    diff = subprocess.run(
        ["git", "diff", f"{commit}^", commit, "--", file],
        capture_output=True,
        text=True,
    )
    if diff.returncode != 0:
        _REMOVED_LINES_CACHE[cache_key] = []
        return []
    removed = []
    for line in diff.stdout.splitlines():
        if line.startswith("-") and not line.startswith("---"):
            s = line[1:].strip()
            if _is_noise_line(s, file):
                continue
            if _is_boilerplate_line(s):
                continue
            if len(re.sub(r"\W", "", s)) >= 6:  # enough alnum to be distinctive
                removed.append(s)
    _REMOVED_LINES_CACHE[cache_key] = removed
    return removed


def vulnerable_preimage_present(commit, changed_files, ref):
    """Whether the exact lines the fix removes/changes are still on *ref*:
    True  -> present (branch still vulnerable);
    False -> provably absent (code diverged or not here);
    None  -> the fix removes nothing distinctive (pure addition), can't tell.
    """
    cache_key = (commit, tuple(changed_files), ref)
    if cache_key in _PREIMAGE_CACHE:
        return _PREIMAGE_CACHE[cache_key]
    result = _vulnerable_preimage_present_uncached(commit, changed_files, ref)
    _PREIMAGE_CACHE[cache_key] = result
    return result


def _is_test_or_generated_file(f):
    """True for test or auto-generated files. Their content is not the shipped
    vulnerable source, so a pre-image match there is not evidence of impact."""
    if any(f == p or f.startswith(p.rstrip("/") + "/") for p in _GENERATED_PATHSPECS):
        return True
    base = f.rsplit("/", 1)[-1]
    return (
        "_test." in base
        or base.startswith("test_")
        or f.startswith("test/")
        or "/test/" in f
        or "fuzz" in f
    )


def _vulnerable_preimage_present_uncached(commit, changed_files, ref):
    saw_removed = False
    for file in changed_files:
        # Skip test/generated files: a match there isn't the shipped vulnerable
        # code, and counting it produced false 'still present' (affected) results.
        if _is_test_or_generated_file(file):
            continue
        removed = _fix_removed_lines(commit, file)
        if not removed:
            continue
        saw_removed = True
        show = subprocess.run(
            ["git", "show", f"{ref}:{file}"], capture_output=True, text=True
        )
        if show.returncode != 0:
            continue
        content = _norm_ws(show.stdout)
        for rl in removed:
            if _norm_ws(rl) in content:
                return True
    if not saw_removed:
        return None
    return False


# ---------------------------------------------------------------------------
# 6. Git file access (rename-aware)
# ---------------------------------------------------------------------------


def _get_commit_diff(commit):
    """Return the full diff for *commit* as a string (capped at _AI_MAX_DIFF_BYTES)."""
    result = subprocess.run(
        ["git", "show", "--stat", "-p", commit],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if result.returncode != 0:
        return ""
    return result.stdout[:_AI_MAX_DIFF_BYTES]


def _show_file(ref, path):
    """Raw contents of *path* at *ref*, or None if it doesn't exist there."""
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _historical_paths(commit, file_path, limit=6):
    """Paths *file_path* has occupied over its history (current first, then older
    names, following renames) as of *commit* -- so we can find the file on a
    branch that forked before a rename."""
    paths = [file_path]
    result = subprocess.run(
        [
            "git",
            "log",
            "--follow",
            "--name-status",
            "--format=",
            commit,
            "--",
            file_path,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return paths
    seen = {file_path}
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        # Rename entries look like: R100<TAB>old/path<TAB>new/path
        if parts and parts[0].startswith("R") and len(parts) >= 3:
            old = parts[1].strip()
            if old and old not in seen:
                paths.append(old)
                seen.add(old)
                if len(paths) >= limit:
                    break
    return paths


def _get_file_on_branch(file_path, branch_ref, commit=None):
    """(content, resolved_path) for *file_path* on *branch_ref*, capped at
    _AI_MAX_FILE_BYTES. If absent at the current path and *commit* is given,
    follows rename history to try earlier paths. (None, None) if not found."""
    content = _show_file(branch_ref, file_path)
    if content is not None:
        return content[:_AI_MAX_FILE_BYTES], file_path
    if commit:
        for older in _historical_paths(commit, file_path):
            if older == file_path:
                continue
            content = _show_file(branch_ref, older)
            if content is not None:
                return content[:_AI_MAX_FILE_BYTES], older
    return None, None


# ---------------------------------------------------------------------------
# 7. Introducer tracing
# ---------------------------------------------------------------------------


def find_introducing_commit(commit, files):
    """Commit(s) that introduced the code the fix changes. For each touched line
    range, `git log -L --reverse` gives the oldest commit to write those lines
    (the introducer), falling back to `git blame -w -M -C`. Comment/blank/
    punctuation-only hunks are skipped so a stale comment can't trace to an
    ancient import. Returns a set of SHAs."""
    introducing = set()

    for file in files:
        # Test/generated files aren't the vulnerable source, and their introducer
        # would over-flag branches that lack the fixed module.
        if _is_test_or_generated_file(file):
            continue
        result = subprocess.run(
            ["git", "diff", "-U0", f"{commit}^", commit, "--", file],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git diff failed: {result.stderr}")

        # Parse each hunk with its changed lines so noise-only hunks can be skipped.
        hunks = []
        cur = None
        for line in result.stdout.splitlines():
            if line.startswith("@@"):
                match = re.match(r"^@@ -(\d+)(?:,(\d+))? ", line)
                cur = None
                if match:
                    cur = {
                        "start": int(match.group(1)),
                        "count": int(match.group(2)) if match.group(2) else 1,
                        "changed": [],
                    }
                    hunks.append(cur)
            elif (
                cur is not None
                and line
                and line[0] in "+-"
                and not line.startswith(("+++", "---"))
            ):
                cur["changed"].append(line[1:])

        for h in hunks:
            if h["changed"] and all(_is_noise_line(c, file) for c in h["changed"]):
                continue  # comment/blank/punctuation-only change: not impact-relevant
            old_start, old_count = h["start"], h["count"]
            if old_count == 0:
                # Pure addition: inspect the line right after the insertion point.
                blame_start = old_start + 1
                blame_end = old_start + 1
            else:
                # Lines were removed/modified: inspect those exact lines.
                blame_start = old_start
                blame_end = old_start + old_count - 1

            origin_sha = _find_line_origin(file, blame_start, blame_end, f"{commit}^")
            if origin_sha:
                introducing.add(origin_sha)

    return introducing


def _find_line_origin(file, line_start, line_end, ref):
    """SHA of the oldest commit to touch lines [line_start, line_end] of *file* as
    of *ref* (via `git log -L --reverse`), falling back to `git blame -w -M -C`."""
    log_result = subprocess.run(
        [
            "git",
            "log",
            f"-L{line_start},{line_end}:{file}",
            "--format=%H",
            "--reverse",
            ref,
        ],
        capture_output=True,
        text=True,
    )
    if log_result.returncode == 0:
        for log_line in log_result.stdout.splitlines():
            log_line = log_line.strip()
            # `--format=%H` only prints SHAs on their own lines; the rest is the
            # diff body. Take the first 40-char hex string we see.
            if len(log_line) == 40 and all(c in "0123456789abcdef" for c in log_line):
                return log_line

    # Fallback: use blame (with whitespace/move-aware flags). Less accurate for
    # finding the original introducer, but works on edge cases log -L can't.
    blame_result = subprocess.run(
        [
            "git",
            "blame",
            "-w",
            "-M",
            "-C",
            "-L",
            f"{line_start},{line_end}",
            ref,
            "--",
            file,
        ],
        capture_output=True,
        text=True,
    )
    if blame_result.returncode != 0:
        # Both failed -- usually a pure addition whose post-insertion line is at/past
        # EOF in the parent (newly-added lines have no pre-image). Skip this hunk.
        print(
            f"[introducer] no pre-image for {file}:{line_start}-{line_end} on "
            f"{ref} (likely newly-added lines); skipping this hunk.",
            file=sys.stderr,
        )
        return None
    for blame_line in blame_result.stdout.splitlines():
        if not blame_line:
            continue
        return blame_line.split()[0].lstrip("^")
    return None


# ---------------------------------------------------------------------------
# 8. Impact verdict
# ---------------------------------------------------------------------------


def _introducer_reaches(introducing_commits, ref):
    """True if any introducer reaches *ref* by SHA ancestry (Path 1) or patch-id
    equivalence (Path 2 -- a cherry-pick that got a new SHA)."""
    for sha in introducing_commits:
        r = subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, ref],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            return True
        if r.returncode != 1:
            raise RuntimeError(
                f"git merge-base failed (code {r.returncode}) checking {sha} "
                f"against {ref}: {r.stderr}"
            )
    branch_pids = _get_branch_patch_ids(ref)
    for sha in introducing_commits:
        pid = _patch_id_of(sha)
        if pid and pid in branch_pids:
            return True
    return False


def _source_files_present(changed_files, ref, commit):
    """True if any non-test/-generated changed file exists on *ref* (rename-aware)."""
    source = [
        f for f in changed_files if not _is_test_or_generated_file(f)
    ] or changed_files
    return any(
        _get_file_on_branch(f, ref, commit=commit)[0] is not None for f in source
    )


def _deterministic_impact(introducing_commits, ref, commit, changed_files):
    """Deterministic verdict before the AI layer: 'affected', 'not_affected', or
    'inconclusive'. Implements Paths 1/2 (ancestry, patch-id), 2b (positive
    pre-image), 3 (file absence), and 4 (pre-image downgrade)."""
    has_context = bool(commit and changed_files)
    affected = _introducer_reaches(introducing_commits, ref)
    # Path 2b: a branch-specific introducer that Paths 1/2 miss, caught by the
    # exact removed lines still being present.
    if not affected and has_context:
        affected = vulnerable_preimage_present(commit, changed_files, ref) is True

    if affected:
        # Path 4: ancestry matched only old shared code -- if the removed lines are
        # provably absent, downgrade to inconclusive (the AI tie-breaker re-flags a
        # reshaped-but-vulnerable branch). Gated by BACKPORT_PREIMAGE_DOWNGRADE.
        if (
            has_context
            and os.environ.get("BACKPORT_PREIMAGE_DOWNGRADE", "1") == "1"
            and vulnerable_preimage_present(commit, changed_files, ref) is False
        ):
            return "inconclusive"
        return "affected"

    # Path 3: none of the fixed source files exist here -> confident not-affected.
    if changed_files and not _source_files_present(changed_files, ref, commit):
        return "not_affected"
    return "inconclusive"


def _run_ai_advisory(commit, branch, changed_files, introducing_commits, det_affected):
    """Call the advisory AI in the role implied by the deterministic verdict, tag
    the result, and log it. Returns the advisory dict or None."""
    from ai import ai_impact_analysis  # local import avoids an ai<->engine cycle

    det_verdict = "affected" if det_affected else "inconclusive"
    advisory = ai_impact_analysis(
        commit, branch, changed_files, introducing_commits, det_verdict=det_verdict
    )
    if advisory is not None:
        advisory["role"] = "auditor" if det_affected else "tiebreaker"
        advisory["overrode_deterministic"] = False
        # Live progress line; off by default so it doesn't interleave with the
        # replay's per-fix tables (the AI verdict is already in each fix's Notes).
        # Set BACKPORT_VERBOSE=1 to see it.
        if os.environ.get("BACKPORT_VERBOSE"):
            print(
                f"[ai] {advisory['role']} for {branch}: det={det_verdict}, "
                f"likely_affected={advisory['likely_affected']}, "
                f"confidence={advisory['confidence']}",
                file=sys.stderr,
            )
    return advisory


def _fold_advisory(det_affected, advisory, commit, changed_files, ref):
    """Combine the deterministic verdict with the advisory, gated by direction so
    the AI never acts alone:
      tie-breaker (inconclusive -> affected): safe, only ADDS a backport;
      auditor (affected -> not affected): can MISS a backport, so suppress only on
      HIGH-confidence "not affected", BACKPORT_AI_SUPPRESS on (default), AND the
      removed lines provably absent.
    """
    if advisory is None:
        return det_affected
    likely = advisory.get("likely_affected")
    conf = advisory.get("confidence")

    if det_affected:
        suppress = os.environ.get("BACKPORT_AI_SUPPRESS", "1") == "1"
        if (
            suppress
            and likely is False
            and conf == "high"
            and vulnerable_preimage_present(commit, changed_files, ref) is False
        ):
            advisory["overrode_deterministic"] = True
            return False
        return True

    # Inconclusive: a "likely affected" upgrades only if a fixed file is actually
    # here at its exact path (else a backport would be an impossible cherry-pick).
    if likely is True:
        if _any_changed_file_present_exact(changed_files, ref):
            advisory["overrode_deterministic"] = True
            return True
        advisory["tiebreaker_blocked_no_file"] = True
    return False


def is_branch_affected(
    introducing_commits, branch, commit=None, changed_files=None
) -> "tuple[bool, dict | None]":
    """Is *branch* affected by the fix? Returns (affected, ai_advisory).

    The deterministic engine (see _deterministic_impact) owns the verdict; the AI
    layer only nudges it under strict gating (see _fold_advisory). Called with
    just (introducers, branch) it is a pure ancestry + patch-id check; called with
    commit + changed_files it also runs the pre-image paths and the AI layer.
    See CLAUDE.md for the full rationale behind each path.
    """
    ref = f"origin/{branch}"
    verdict = _deterministic_impact(introducing_commits, ref, commit, changed_files)
    if verdict == "not_affected":
        return False, None
    det_affected = verdict == "affected"

    # No fix context (the 2-arg call from bucketing): return the deterministic verdict.
    if not (commit and changed_files):
        return det_affected, None

    # Inconclusive AND the code isn't on this branch at all -> confident
    # not-affected; an AI call here could only guess.
    if not det_affected and not _any_changed_file_present_exact(changed_files, ref):
        return False, None

    advisory = _run_ai_advisory(
        commit, branch, changed_files, introducing_commits, det_affected
    )
    return _fold_advisory(det_affected, advisory, commit, changed_files, ref), advisory


def present_introducers(introducing_commits, branch):
    """Subset of *introducing_commits* present on *branch*, by SHA ancestry OR
    patch-id. Finer-grained than is_branch_affected (which stops at the first
    match): lets a caller tell a FULL lineage (all introducers present ->
    confidently affected) from a PARTIAL one (only old shared code present, the
    newer bug-introducing commit absent -> likely over-flag worth review)."""
    ref = f"origin/{branch}"
    present = set()
    for sha in introducing_commits:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, ref], capture_output=True
        )
        if result.returncode == 0:
            present.add(sha)
    remaining = set(introducing_commits) - present
    if remaining:
        branch_pids = _get_branch_patch_ids(ref)
        for sha in remaining:
            pid = _patch_id_of(sha)
            if pid and pid in branch_pids:
                present.add(sha)
    return present


def _any_changed_file_present_exact(changed_files, ref):
    """True if any changed source file exists on *ref* at its EXACT path. Used to
    stop the tie-breaker upgrading a branch where the fix's code isn't present
    (rename-aware matching could falsely link unrelated same-named files)."""
    source = [f for f in (changed_files or ()) if not _is_test_or_generated_file(f)]
    for f in source or (changed_files or ()):
        r = subprocess.run(["git", "cat-file", "-e", f"{ref}:{f}"], capture_output=True)
        if r.returncode == 0:
            return True
    return False


# ---------------------------------------------------------------------------
# 9. Already-patched / patch-id
# ---------------------------------------------------------------------------


def _branch_cites_cherry_pick(commit, ref):
    """True if a divergent commit on *ref* records `cherry picked from commit
    <full-sha>` for *commit*. Catches bundled/reshaped -x backports whose patch-id
    differs; the exact-SHA match means it never false-negatives. Mainline ref via
    BACKPORT_MAINLINE_REF (default origin/main)."""
    full = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    if full.returncode != 0 or not full.stdout.strip():
        return False
    full_sha = full.stdout.strip()
    mainline = os.environ.get("BACKPORT_MAINLINE_REF", "origin/main")
    log = subprocess.run(
        ["git", "log", "--format=%B%x00", f"{mainline}..{ref}"],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if log.returncode != 0:
        return False
    return f"cherry picked from commit {full_sha}" in log.stdout


def _get_branch_patch_ids(ref):
    """Patch-ids of the branch's DIVERGENT commits (on *ref* but not mainline),
    where cherry-picked backports live. Output read as bytes to tolerate binary
    diffs. Mainline ref via BACKPORT_MAINLINE_REF (default origin/main)."""
    mainline = os.environ.get("BACKPORT_MAINLINE_REF", "origin/main")
    rev_range = f"{mainline}..{ref}"
    log = subprocess.run(
        [
            "git",
            "log",
            "-p",
            "--no-merges",
            "--format=%H",
            rev_range,
            *_patch_id_pathspec(),
        ],
        capture_output=True,  # bytes, not text: diffs may contain binary content
    )
    if log.returncode != 0:
        return set()
    pid_proc = subprocess.run(
        ["git", "patch-id", "--stable"],
        input=log.stdout,
        capture_output=True,
    )
    if pid_proc.returncode != 0:
        return set()
    out = pid_proc.stdout.decode("ascii", errors="replace")
    return {line.split()[0] for line in out.splitlines() if line.split()}


def is_already_patched(commit, branch):
    """Whether *commit*'s change is already on *branch* -- as a direct ancestor
    (forked after the fix), a `-x` cherry-pick annotation, or a matching patch-id
    (manual cherry-pick under a new SHA). Patch-ids exclude generated files."""
    ref = f"origin/{branch}"

    # Fast path: the exact commit is an ancestor (branch forked after the fix).
    # The divergent-only patch-id scan below would otherwise miss this.
    anc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, ref], capture_output=True
    )
    if anc.returncode == 0:
        return True

    # A `-x` annotation proves a cherry-pick even when a reshaped/bundled backport
    # has a different patch-id.
    if _branch_cites_cherry_pick(commit, ref):
        return True

    target_pid = _patch_id_of(commit)
    if not target_pid:
        return False

    branch_pids = _get_branch_patch_ids(ref)
    return target_pid in branch_pids


def _patch_id_of(commit):
    """Return the patch-id (content hash) of a single commit, or None on failure."""
    show = subprocess.run(
        ["git", "show", commit, *_patch_id_pathspec()],
        capture_output=True,  # bytes: the commit may touch binary files
    )
    if show.returncode != 0:
        return None
    pid = subprocess.run(
        ["git", "patch-id", "--stable"],
        input=show.stdout,
        capture_output=True,
    )
    if pid.returncode != 0 or not pid.stdout.strip():
        return None
    return pid.stdout.decode("ascii", errors="replace").split()[0]
