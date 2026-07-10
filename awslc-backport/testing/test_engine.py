#!/usr/bin/env python3
"""
Unit tests for the pure (repo-independent) helpers in engine.py.

These run without an aws-lc checkout, credentials, or network -- they only
exercise the string/date logic the impact analyzer relies on. For the
end-to-end, repo-backed behavior see replay_real_cve.py (real replays).

Run:
    python3 -m unittest testing.test_engine        # from the awslc-backport dir
    python3 testing/test_engine.py                 # or directly
"""

import sys
import unittest
from datetime import date
from pathlib import Path

# engine.py lives one directory up (awslc-backport/engine.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine  # noqa: E402


class NormWhitespace(unittest.TestCase):
    def test_collapses_runs_and_strips(self):
        self.assertEqual(engine._norm_ws("  a   b\t c  "), "a b c")

    def test_empty(self):
        self.assertEqual(engine._norm_ws("   \t "), "")


class IsCFile(unittest.TestCase):
    def test_c_family(self):
        for f in ("a.c", "b.cc", "d.h", "e.hpp", "f.cxx"):
            self.assertTrue(engine._is_c_file(f), f)

    def test_non_c(self):
        for f in ("CMakeLists.txt", "x.py", "y.pl", "z.S", "build.yaml", None):
            self.assertFalse(engine._is_c_file(f), f)


class IsNoiseLine(unittest.TestCase):
    def test_blank_and_comments(self):
        for s in ("", "   ", "// comment", "/* block", "* doc", "*/"):
            self.assertTrue(engine._is_noise_line(s), repr(s))

    def test_punctuation_only(self):
        for s in ("{", "}", "});", "  ;  "):
            self.assertTrue(engine._is_noise_line(s), repr(s))

    def test_hash_is_comment_in_non_c_but_code_in_c(self):
        # '#' is a comment in scripts/CMake/YAML ...
        self.assertTrue(engine._is_noise_line("# a cmake comment", "CMakeLists.txt"))
        # ... but a preprocessor directive (real code) in C/C++.
        self.assertFalse(engine._is_noise_line("#include <foo.h>", "a.c"))
        self.assertFalse(engine._is_noise_line("#if defined(X)", "b.h"))

    def test_real_code_is_not_noise(self):
        self.assertFalse(engine._is_noise_line("int rc = do_thing(ptr, len);", "a.c"))


class IsBoilerplateLine(unittest.TestCase):
    def test_bare_control_flow(self):
        for s in ("return;", "break;", "continue;", "goto err;", "return 0;"):
            self.assertTrue(engine._is_boilerplate_line(s), repr(s))

    def test_include(self):
        self.assertTrue(engine._is_boilerplate_line('#include "internal.h"'))

    def test_string_literal_only(self):
        self.assertTrue(engine._is_boilerplate_line('"SHA2-512"'))

    def test_distinctive_code_is_kept(self):
        self.assertFalse(
            engine._is_boilerplate_line("if (EVP_MD_size(md) <= 0) return 0;")
        )


class ParseEosDate(unittest.TestCase):
    def test_full_date(self):
        self.assertEqual(engine._parse_eos_date("2025-09-12"), date(2025, 9, 12))

    def test_year_month(self):
        # YYYY-MM is accepted (day defaults to the first).
        self.assertEqual(engine._parse_eos_date("2025-09"), date(2025, 9, 1))

    def test_invalid(self):
        for bad in (None, "", "not-a-date", "2025/09/12"):
            self.assertIsNone(engine._parse_eos_date(bad), repr(bad))


class PatchIdPathspec(unittest.TestCase):
    def test_returns_list(self):
        # Must be a list of git args (possibly empty); never raises.
        self.assertIsInstance(engine._patch_id_pathspec(), list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
