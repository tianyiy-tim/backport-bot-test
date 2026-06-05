#!/usr/bin/env bash
#
# v2 fixture — richer commit graph for stress-testing the bot.
#
# Layout:
#   A. initial (app.c, utils/buffer.c)              ← FIPS-2020
#   B. add crypto.c (vulnerable handshake)          ← FIPS-2021
#   C. add tls.c (vulnerable record)                ← FIPS-2022
#   D. add cert.c                                   ← FIPS-2023
#   E. add digest.c                                 ← FIPS-2024 (+ divergent)
#                                                   ← NetOS (+ custom)
#                                                   ← FIPS-2025 (+ cherry-pick of F)
#   F. cve-handshake-1 (bounds check)
#   G. cve-record-1 (length check)
#   H. refactor: crypto.c → crypto/handshake.c, etc
#   I. cve-buffer (fix in utils/buffer.c — oldest code)              [tag: cve-buffer]
#   J. cve-handshake-postrefactor (fix in moved file)                [tag: cve-handshake-postrefactor]
#   K. cve-record-multifile (fix in 2 files)                         [tag: cve-record-multifile]
#                                                   ← main
#
# WARNING: this rewrites history and force-pushes. Test repos only.

set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

if ! git remote get-url origin | grep -q backport-bot-test; then
  echo "Refusing to run: origin doesn't look like backport-bot-test" >&2
  exit 1
fi

# Save untracked work
WORK=$(mktemp -d)
echo "Backing up untracked work to $WORK"
[ -d scripts ] && cp -R scripts "$WORK/"
[ -d .github ] && cp -R .github "$WORK/"

# Wipe local refs
git checkout --quiet --detach
for b in main NetOS \
         AWS-LC-FIPS-2020 AWS-LC-FIPS-2021 AWS-LC-FIPS-2022 AWS-LC-FIPS-2023 \
         AWS-LC-FIPS-2024 AWS-LC-FIPS-2025 \
         AWS-LC-FIPS-2021-old AWS-LC-FIPS-2022-old AWS-LC-FIPS-2023-old AWS-LC-FIPS-2024-old; do
  git branch -D "$b" 2>/dev/null || true
done
for t in cve-crypto cve-tls cve-multi \
         cve-buffer cve-handshake-original cve-handshake-postrefactor \
         cve-record-multifile cve-digest-recent; do
  git tag -d "$t" 2>/dev/null || true
done

# Wipe tracked files for clean slate
git rm -rf --quiet --ignore-unmatch . || true
rm -rf app.c crypto.c tls.c cert.c utils crypto tls

# Restore scripts/.github (untracked)
[ -d "$WORK/scripts" ] && cp -R "$WORK/scripts" .
[ -d "$WORK/.github" ] && cp -R "$WORK/.github" .

# ============================================================================
# A. initial commit — app.c + utils/buffer.c
# ============================================================================
mkdir -p utils
cat > app.c <<'EOF'
int main() { return 0; }
EOF
cat > utils/buffer.c <<'EOF'
#include <string.h>

void copy_buffer(char *dst, const char *src, int len) {
    memcpy(dst, src, len);
}

int buffer_length(const char *buf) {
    return (int)strlen(buf);
}
EOF
git checkout --orphan rebuild
git rm -rf --quiet --cached . 2>/dev/null || true
git add app.c utils/buffer.c
git commit --quiet -m "initial commit (app.c, utils/buffer.c)"
git tag _A

# ============================================================================
# B. add crypto.c (vulnerable process_handshake)
# ============================================================================
cat > crypto.c <<'EOF'
#include <string.h>

#define MAX_HANDSHAKE 4096

void process_handshake(char *buf, const char *input, int len) {
    memcpy(buf, input, len);
}

int verify_signature(const char *sig) {
    return 1;
}
EOF
git add crypto.c
git commit --quiet -m "add crypto.c with process_handshake"
git tag _B

# ============================================================================
# C. add tls.c (vulnerable handle_record)
# ============================================================================
cat > tls.c <<'EOF'
#include <string.h>

#define MAX_RECORD 16384

void handle_record(char *out, const char *record, int record_len) {
    memcpy(out, record, record_len);
}
EOF
git add tls.c
git commit --quiet -m "add tls.c with handle_record"
git tag _C

# ============================================================================
# D. add cert.c
# ============================================================================
cat > cert.c <<'EOF'
int parse_cert(const char *cert_data, int len) {
    return 1;
}

int validate_chain(const char *chain) {
    return 1;
}
EOF
git add cert.c
git commit --quiet -m "add cert.c with parse_cert"
git tag _D

# ============================================================================
# E. add digest.c
# ============================================================================
cat > digest.c <<'EOF'
#include <string.h>

void compute_hash(char *out, const char *input, int len) {
    memcpy(out, input, 32);
}

int hash_compare(const char *a, const char *b) {
    return memcmp(a, b, 32);
}
EOF
git add digest.c
git commit --quiet -m "add digest.c with compute_hash"
git tag _E

# ============================================================================
# F. cve-handshake-1: bounds check in process_handshake (single-file CVE)
# ============================================================================
cat > crypto.c <<'EOF'
#include <string.h>

#define MAX_HANDSHAKE 4096

// Fixed: bounds check
void process_handshake(char *buf, const char *input, int len) {
    if (len > MAX_HANDSHAKE) {
        return;
    }
    memcpy(buf, input, len);
}

int verify_signature(const char *sig) {
    return 1;
}
EOF
git add crypto.c
git commit --quiet -m "fix: bounds check in process_handshake (cve-handshake-1)"
git tag _F
git tag cve-handshake-original

# ============================================================================
# G. cve-record-1: length validation in handle_record
# ============================================================================
cat > tls.c <<'EOF'
#include <string.h>

#define MAX_RECORD 16384

// Fixed: length validation
void handle_record(char *out, const char *record, int record_len) {
    if (record_len > MAX_RECORD) {
        return;
    }
    memcpy(out, record, record_len);
}
EOF
git add tls.c
git commit --quiet -m "fix: length validation in handle_record (cve-record-1)"
git tag _G

# ============================================================================
# H. refactor: move files into subdirectories
# ============================================================================
mkdir -p crypto tls
git mv crypto.c crypto/handshake.c
git mv digest.c crypto/digest.c
git mv tls.c tls/record.c
git mv cert.c tls/cert.c
git commit --quiet -m "refactor: organize source files into subdirectories"
git tag _H

# ============================================================================
# I. cve-buffer: fix in utils/buffer.c (touches OLDEST code, from commit A)
# ============================================================================
cat > utils/buffer.c <<'EOF'
#include <string.h>

void copy_buffer(char *dst, const char *src, int len) {
    if (dst == NULL || src == NULL) {
        return;
    }
    memcpy(dst, src, len);
}

int buffer_length(const char *buf) {
    if (buf == NULL) {
        return 0;
    }
    return (int)strlen(buf);
}
EOF
git add utils/buffer.c
git commit --quiet -m "fix: null guards in buffer utilities (cve-buffer)"
git tag _I
git tag cve-buffer

# ============================================================================
# J. cve-handshake-postrefactor: null check in moved handshake file
# ============================================================================
cat > crypto/handshake.c <<'EOF'
#include <string.h>

#define MAX_HANDSHAKE 4096

// Fixed: bounds check + null check
void process_handshake(char *buf, const char *input, int len) {
    if (buf == NULL || input == NULL) {
        return;
    }
    if (len > MAX_HANDSHAKE) {
        return;
    }
    memcpy(buf, input, len);
}

int verify_signature(const char *sig) {
    if (sig == NULL) {
        return 0;
    }
    return 1;
}
EOF
git add crypto/handshake.c
git commit --quiet -m "fix: null guards in handshake (cve-handshake-postrefactor)"
git tag _J
git tag cve-handshake-postrefactor

# ============================================================================
# K. cve-record-multifile: fixes in tls/record.c + tls/cert.c
# ============================================================================
cat > tls/record.c <<'EOF'
#include <string.h>

#define MAX_RECORD 16384

// Fixed: length + null
void handle_record(char *out, const char *record, int record_len) {
    if (out == NULL || record == NULL) {
        return;
    }
    if (record_len > MAX_RECORD) {
        return;
    }
    memcpy(out, record, record_len);
}
EOF
cat > tls/cert.c <<'EOF'
int parse_cert(const char *cert_data, int len) {
    if (cert_data == NULL || len <= 0) {
        return 0;
    }
    return 1;
}

int validate_chain(const char *chain) {
    if (chain == NULL) {
        return 0;
    }
    return 1;
}
EOF
git add tls/record.c tls/cert.c
git commit --quiet -m "fix: null guards in tls record + cert (cve-record-multifile)"
git tag _K
git tag cve-record-multifile

# ============================================================================
# Move main → K
# ============================================================================
git branch -f main _K

# ============================================================================
# Place release branches at their fork points
# ============================================================================
git branch -f AWS-LC-FIPS-2020 _A
git branch -f AWS-LC-FIPS-2021 _B
git branch -f AWS-LC-FIPS-2022 _C
git branch -f AWS-LC-FIPS-2023 _D
git branch -f AWS-LC-FIPS-2024 _E

# ============================================================================
# AWS-LC-FIPS-2024: divergent commit (will conflict on cherry-pick)
# Rewrite process_handshake to use strncpy instead of memcpy.
# ============================================================================
git checkout --quiet AWS-LC-FIPS-2024
cat > crypto.c <<'EOF'
#include <string.h>

#define MAX_HANDSHAKE 4096

void process_handshake(char *buf, const char *input, int len) {
    strncpy(buf, input, len);
}

int verify_signature(const char *sig) {
    return 1;
}
EOF
git add crypto.c
git commit --quiet -m "refactor: switch process_handshake to strncpy"

# ============================================================================
# AWS-LC-FIPS-2025: forks at E, then cherry-picks F (cve-handshake-1)
# This creates a new commit with cve-handshake-1's CHANGES but a new SHA.
# Tests false-negative detection: the bot looking for F's SHA won't find it
# in 2025's history, but the fix IS there.
# ============================================================================
git checkout --quiet -B AWS-LC-FIPS-2025 _E
git cherry-pick --quiet _F

# ============================================================================
# NetOS: forks at E, custom commit (one-off branch)
# ============================================================================
git checkout --quiet -B NetOS _E
cat > crypto.c <<'EOF'
#include <stdio.h>
#include <string.h>

#define MAX_HANDSHAKE 4096

void process_handshake(char *buf, const char *input, int len) {
    fprintf(stderr, "[NetOS] handshake len=%d\n", len);
    memcpy(buf, input, len);
}

int verify_signature(const char *sig) {
    return 1;
}
EOF
git add crypto.c
git commit --quiet -m "NetOS: add debug logging to handshake"

# Back to main
git checkout --quiet main

# ============================================================================
# Cleanup internal tags
# ============================================================================
for t in _A _B _C _D _E _F _G _H _I _J _K; do
  git tag -d "$t" >/dev/null
done
git branch -D rebuild 2>/dev/null || true

# ============================================================================
# Force-push everything
# ============================================================================
echo
echo "=== Pushing to origin... ==="
git push --force origin main
for b in NetOS \
         AWS-LC-FIPS-2020 AWS-LC-FIPS-2021 AWS-LC-FIPS-2022 \
         AWS-LC-FIPS-2023 AWS-LC-FIPS-2024 AWS-LC-FIPS-2025; do
  git push --force origin "$b"
done
git push --force origin \
  cve-buffer cve-handshake-original cve-handshake-postrefactor cve-record-multifile

# Delete old tags from remote that we removed
for old in cve-crypto cve-tls cve-multi; do
  git push --delete origin "$old" 2>/dev/null || true
done

# Delete stale remote feature branches if any
for stale in fix/cert-parser-v2 fix/cert-parser-v3 fix/handle-record-overflow \
             test-backport-trigger fix/app-version-comment fix/cert-parser; do
  git push --delete origin "$stale" 2>/dev/null || true
done

echo
echo "=== Done. New layout: ==="
git --no-pager log --all --oneline --graph --decorate | head -40
