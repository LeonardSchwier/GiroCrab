"""Tests for tools/fix_proof.py (run: python3 -m unittest tools/test_fix_proof.py).

The end-to-end class builds a tiny cargo workspace in a temporary git repository: a base commit
with a bug, a head commit with the fix and new tests. It runs the real `plan` / `run` / `verdict`
path and checks that a regression test is reported red before the fix and green after it.
"""
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest

spec = importlib.util.spec_from_file_location("fix_proof", pathlib.Path(__file__).with_name("fix_proof.py"))
fp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fp)


class FakeTree:
    def __init__(self, files):
        self.files = files

    def read(self, path):
        return self.files.get(path)


class RustLexer(unittest.TestCase):
    def test_braces_in_strings_chars_and_comments_do_not_count(self):
        src = textwrap.dedent('''
            fn a() { let s = "}{"; let r = r#"a "} b"#; let c = '{'; /* } /* nested { */ */ }
            // }
            fn b<'a>(x: &'a str) -> &'a str { x }
        ''')
        items = fp.rust_items(src)
        self.assertEqual([i["key"].split(" (")[0] for i in items], ["fn a", "fn b < 'a >"])

    def test_raw_identifier_and_byte_strings(self):
        toks = fp.lex_rust('let r#type = b"x}"; let y = br#"{"#; let z = b\'}\';')
        self.assertIn(("ident", "type"), [(k, t) for k, t, *_ in toks])
        self.assertFalse(any(k == "punct" and t in "{}" for k, t, *_ in toks))


BASE = textwrap.dedent('''
    //! crate docs
    #![allow(dead_code)]
    pub fn add(a: i32, b: i32) -> i32 { a - b }

    #[cfg(test)]
    fn helper() -> i32 { 1 }

    #[cfg(test)]
    mod tests {
        use super::*;
        #[test]
        fn old() { assert_eq!(add(0, 0), 0); }
    }

    #[cfg(test)]
    mod gone_tests {
        #[test]
        fn removed() {}
    }
''')

HEAD = textwrap.dedent('''
    //! crate docs
    #![allow(dead_code)]
    pub fn add(a: i32, b: i32) -> i32 { a + b }
    pub fn new_api() -> i32 { 7 }

    #[cfg(test)]
    fn helper() -> i32 { 2 }

    #[cfg(test)]
    mod tests {
        use super::*;
        #[test]
        fn old() { assert_eq!(add(0, 0), 0); }
        #[test]
        fn adds() { assert_eq!(add(2, 2), 4); }
    }

    #[cfg(test)]
    #[allow(unused)]
    mod extra_tests {
        #[tokio::test(start_paused = true)]
        async fn later() {}
    }
''')


class Splice(unittest.TestCase):
    def test_base_production_code_with_head_tests(self):
        out = fp.splice_tests(BASE, HEAD)
        self.assertIn("a - b", out)            # production code stays at base
        self.assertNotIn("a + b", out)
        self.assertNotIn("new_api", out)
        self.assertIn("fn adds()", out)        # head's test module
        self.assertIn("fn helper() -> i32 { 2 }", out)
        self.assertNotIn("gone_tests", out)    # deleted in head
        self.assertIn("mod extra_tests", out)  # new in head, appended
        self.assertIn("#![allow(dead_code)]", out)

    def test_no_test_items_leaves_source_alone(self):
        src = "fn main() {}\n"
        self.assertEqual(fp.splice_tests(src, "fn main() { changed() }\n"), src)

    def test_cfg_any_test_is_production_code(self):
        items = fp.rust_items('#[cfg(any(test, feature = "x"))]\nfn f() {}\n#[cfg(all(test, unix))]\nfn g() {}\n')
        self.assertEqual([i["test"] for i in items], [False, True])

    def test_external_test_module_declaration(self):
        items = fp.rust_items("#[cfg(test)]\nmod tests;\npub(crate) fn x() {}\n")
        self.assertEqual([(i["key"], i["test"]) for i in items], [("mod tests", True), ("pub ( crate ) fn x ( )", False)])


class RustTestDiscovery(unittest.TestCase):
    def test_paths_and_changes(self):
        before, after = fp.rust_tests_in(BASE), fp.rust_tests_in(HEAD)
        self.assertEqual(sorted(before), ["gone_tests::removed", "tests::old"])
        self.assertEqual(sorted(after), ["extra_tests::later", "tests::adds", "tests::old"])
        self.assertEqual(before["tests::old"], after["tests::old"])

    def test_nested_modules_and_attributes(self):
        src = textwrap.dedent('''
            mod a {
                mod b {
                    #[test]
                    #[should_panic(expected = "boom")]
                    fn panics() { panic!("boom") }
                    fn not_a_test() {}
                }
                #[tokio::test]
                pub async fn top() {}
            }
            #[test] fn root() {}
        ''')
        self.assertEqual(sorted(fp.rust_tests_in(src)), ["a::b::panics", "a::top", "root"])

    def test_module_location(self):
        self.assertEqual(fp.rust_location("girocrab-core/src/lib.rs", "girocrab-core"), ("lib", ""))
        self.assertEqual(fp.rust_location("girocrab-core/src/vault/rules.rs", "girocrab-core"), ("lib", "vault::rules"))
        self.assertEqual(fp.rust_location("girocrab-core/src/connectors/tests/mod.rs", "girocrab-core"),
                         ("lib", "connectors::tests"))
        self.assertEqual(fp.rust_location("girocrab-mcp/src/main.rs", "girocrab-mcp"), ("bin", ""))
        self.assertEqual(fp.rust_location("girocrab-core/tests/schema_fixtures.rs", "girocrab-core"),
                         ("test:schema_fixtures", ""))
        self.assertIsNone(fp.rust_location("girocrab-core/tests/common/mod.rs", "girocrab-core"))
        self.assertIsNone(fp.rust_location("girocrab-core/examples/x.rs", "girocrab-core"))

    def test_test_only_files(self):
        tree = FakeTree({
            "c/src/lib.rs": "#[cfg(test)]\npub(crate) mod helpers;\nmod real;\n",
            "c/src/vault.rs": "#[cfg(test)]\nmod checks;\n",
        })
        self.assertTrue(fp.rust_test_only("c/src/helpers.rs", tree))
        self.assertTrue(fp.rust_test_only("c/src/helpers/deep.rs", tree))   # transitively
        self.assertFalse(fp.rust_test_only("c/src/real.rs", tree))
        self.assertTrue(fp.rust_test_only("c/src/vault/checks.rs", tree))
        self.assertTrue(fp.rust_test_only("c/src/loans/tests.rs", tree))   # by name
        self.assertTrue(fp.rust_test_only("c/tests/fixtures/a.json", tree))


CARGO_OUT = textwrap.dedent('''
       Compiling calc v0.1.0 (/tmp/x/calc)
        Finished `test` profile [unoptimized + debuginfo] target(s) in 0.50s
         Running unittests src/lib.rs (target/debug/deps/calc-1234)

    running 2 tests
    test tests::adds ... FAILED
    test tests::panics - should panic ... ok
    test tests::skipped ... ignored

    failures:
         Running tests/it.rs (target/debug/deps/it-5678)

    running 1 test
    test integration ... ok
''')


class Parsers(unittest.TestCase):
    def test_cargo(self):
        res, failed = fp.parse_cargo(CARGO_OUT)
        self.assertFalse(failed)
        self.assertEqual(res, {("lib", "tests::adds"): "FAILED", ("lib", "tests::panics"): "ok",
                               ("lib", "tests::skipped"): "ignored", ("test:it", "integration"): "ok"})

    def test_compile_error_files_ignore_warnings(self):
        out = textwrap.dedent('''
            warning: unused variable: `x`
              --> calc/src/other.rs:3:9
            error[E0425]: cannot find function `triple` in this scope
               --> calc/src/lib.rs:12:30
            error: cannot find macro `nope` in this scope
             --> calc/src/checks.rs:4:5
            error[E0425]: cannot find function `triple` in this scope
               --> calc/src/lib.rs:14:30
        ''')
        self.assertEqual(fp.compile_error_files(out), ["calc/src/lib.rs", "calc/src/checks.rs"])

    def test_cargo_compile_error(self):
        res, failed = fp.parse_cargo("error[E0425]: cannot find function `x` in this scope\n"
                                     "error: could not compile `calc` (lib test) due to 1 previous error\n")
        self.assertEqual(res, {})
        self.assertTrue(failed)

class Classification(unittest.TestCase):
    def test_matrix(self):
        c = fp.classify
        self.assertEqual(c("FAILED", "ok", False, False), "proven")
        self.assertEqual(c("ok", "ok", False, False), "not_red")
        self.assertEqual(c(None, "ok", True, False), "proven_build")
        self.assertEqual(c(None, "ok", False, True), "proven_timeout")
        self.assertEqual(c(None, "ok", False, False), "proven_build")   # absent on base
        self.assertEqual(c("FAILED", "FAILED", False, False), "head_failed")
        self.assertEqual(c("ok", None, False, False), "not_run")
        self.assertEqual(c("ok", "ignored", False, False), "not_run")

    def test_fix_detection_and_exemption(self):
        self.assertTrue(fp.is_fix("fix(ci): x", ""))
        self.assertTrue(fp.is_fix("Fix: x", ""))
        self.assertFalse(fp.is_fix("feat: fix-like words", ""))
        self.assertTrue(fp.is_fix("", "fix: a\n\nbody\x00fix(x): b\x00"))
        self.assertFalse(fp.is_fix("", "fix: a\x00docs: b\x00"))
        self.assertEqual(fp.find_exemption("- **Fix-Proof:** skip – nur Tippfehler in einem Log-Text", ""),
                         "nur Tippfehler in einem Log-Text")
        self.assertIsNone(fp.find_exemption("Fix-Proof: skip", ""))          # a reason is required
        self.assertIsNone(fp.find_exemption("<!-- Dann (mit Grund):\nFix-Proof: skip – <Grund aus der Vorlage>\n-->", ""))
        self.assertIsNone(fp.find_exemption("Fix-Proof: skip – kurz", ""))
        self.assertEqual(fp.find_exemption("", "fix: x\n\nFix-Proof: skip: Timing nur auf echter Hardware\x00"),
                         "Timing nur auf echter Hardware")


def plan_with(tests, is_fix=True, exempt=None):
    return {"is_fix": is_fix, "exempt": exempt, "verifier_changed": False, "base": "b", "head": "h",
            "tests": [{"lang": "rust", "path": p, "file": "f.rs", "change": "added"} for p, _ in tests]}


def results_with(tests):
    return [{"lang": "rust", "status": "ran", "tests": [{"path": p, "file": "f.rs", "status": s} for p, s in tests]}]


class Verdict(unittest.TestCase):
    def verdict(self, tests, **kw):
        s = fp.combine(plan_with(tests, **kw), results_with(tests))
        return s["verdict"], fp.fails(s)

    def test_verdicts(self):
        self.assertEqual(self.verdict([("a", "proven"), ("b", "not_red")]), ("proven", False))
        self.assertEqual(self.verdict([("a", "proven_build")]), ("proven_weak", False))
        self.assertEqual(self.verdict([("a", "not_red")]), ("not_red", True))
        self.assertEqual(self.verdict([]), ("missing", True))
        self.assertEqual(self.verdict([], is_fix=False), ("not_applicable", False))
        self.assertEqual(self.verdict([("a", "not_red")], is_fix=False), ("info", False))
        self.assertEqual(self.verdict([], exempt="Timing nur auf Hardware"), ("skipped", False))
        self.assertEqual(self.verdict([("a", "not_run")]), ("not_run", True))

    def test_missing_language_results_count_as_not_run(self):
        s = fp.combine(plan_with([("a", None)]), [])
        self.assertEqual(s["tests"][0]["status"], "not_run")
        self.assertTrue(fp.fails(s))

    def test_summary_mentions_every_test(self):
        s = fp.combine(plan_with([("tests::adds", "proven")]), results_with([("tests::adds", "proven")]))
        md = fp.render_summary(s)
        self.assertIn("✅ Fix bewiesen", md)
        self.assertIn("`tests::adds`", md)


@unittest.skipUnless(shutil.which("cargo") and shutil.which("git"), "needs cargo and git")
class EndToEnd(unittest.TestCase):
    """A real red -> green proof on a throwaway cargo workspace (offline, no dependencies)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix="fix-proof-e2e-"))
        cls.repo = cls.tmp / "repo"
        cls.repo.mkdir()
        cls.env = dict(os.environ, CARGO_TARGET_DIR=str(cls.tmp / "target"), GIT_AUTHOR_NAME="t",
                       GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
                       GIT_COMMITTER_EMAIL="t@example.invalid", CARGO_NET_OFFLINE="true")
        os.environ["CARGO_TARGET_DIR"] = cls.env["CARGO_TARGET_DIR"]
        cls.write({
            "Cargo.toml": '[workspace]\nmembers = ["calc"]\nresolver = "2"\n',
            ".gitignore": "Cargo.lock\n",
            "calc/Cargo.toml": '[package]\nname = "calc"\nversion = "0.1.0"\nedition = "2021"\n',
            "calc/src/lib.rs": textwrap.dedent('''
                pub fn add(a: i32, b: i32) -> i32 { a - b }
                pub fn double(a: i32) -> i32 { a * 2 }
                #[cfg(test)]
                mod tests {
                    use super::*;
                    #[test]
                    fn zero() { assert_eq!(add(0, 0), 0); }
                }
            '''),
            "calc/tests/it.rs": "#[test]\nfn doubles() { assert_eq!(calc::double(2), 4); }\n",
        })
        cls.git("init", "-q", "-b", "main")
        cls.git("add", "-A")
        cls.git("commit", "-q", "-m", "base")
        cls.write({
            "calc/src/lib.rs": textwrap.dedent('''
                pub fn add(a: i32, b: i32) -> i32 { a + b }
                pub fn double(a: i32) -> i32 { a * 2 }
                pub fn triple(a: i32) -> i32 { a * 3 }
                #[cfg(test)]
                mod tests {
                    use super::*;
                    #[test]
                    fn zero() { assert_eq!(add(0, 0), 0); }
                    #[test]
                    fn adds() { assert_eq!(add(2, 2), 4); }
                    #[test]
                    fn doubles_too() { assert_eq!(double(3), 6); }
                    #[test]
                    fn triples() { assert_eq!(triple(1), 3); }
                }
            '''),
            "calc/tests/it.rs": "#[test]\nfn doubles() { assert_eq!(calc::double(2), 4); }\n"
                                "#[test]\nfn adds_from_outside() { assert_eq!(calc::add(1, 1), 2); }\n",
        })
        cls.git("add", "-A")
        cls.git("commit", "-q", "-m", "fix: add adds")
        cls.base = cls.git("rev-parse", "HEAD~1").strip()
        cls.head = cls.git("rev-parse", "HEAD").strip()

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("CARGO_TARGET_DIR", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def write(cls, files):
        for rel, text in files.items():
            p = cls.repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")

    @classmethod
    def git(cls, *args):
        return subprocess.run(["git", "-C", str(cls.repo), *args], check=True, capture_output=True,
                              text=True, env=cls.env).stdout

    def test_red_before_green_after(self):
        plan = fp.build_plan(self.repo, self.base, self.head, "fix: add adds", "")
        self.assertTrue(plan["is_fix"])
        self.assertEqual(sorted(t["path"] for t in plan["tests"]),
                         ["adds_from_outside", "tests::adds", "tests::doubles_too", "tests::triples"])
        logs = self.tmp / "logs"
        logs.mkdir(exist_ok=True)
        res = fp.run_rust(plan, self.repo, logs, timeout=600, log=lambda *_: None)
        status = {t["path"]: t["status"] for t in res["tests"]}
        # the base build fails (triple() is new), so nothing can be red there on its own ...
        self.assertEqual(status["tests::triples"], "proven_build")
        self.assertEqual(status["tests::adds"], "proven_build")
        self.assertEqual(status["adds_from_outside"], "proven")      # integration test: compiled alone
        self.assertEqual(json.loads(json.dumps(res))["status"], "ran")
        s = fp.combine(plan, [res])
        self.assertEqual(s["verdict"], "proven")
        self.assertFalse(fp.fails(s))
        # the worktree is gone and the repository is untouched
        self.assertEqual(self.git("worktree", "list").count("\n"), 1)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_assertion_red_without_new_api(self):
        """Same fix without the new function: the unit test itself must be red on the base."""
        self.git("checkout", "-q", "-b", "lean", self.base)
        self.write({"calc/src/lib.rs": (self.repo / "calc/src/lib.rs").read_text().replace("a - b", "a + b").replace(
            "#[test]\n    fn zero()", "#[test]\n    fn adds() { assert_eq!(add(2, 2), 4); }\n    #[test]\n    fn same() { assert_eq!(double(1), 2); }\n    #[test]\n    fn zero()")})
        self.git("commit", "-qam", "fix: add adds (lean)")
        head = self.git("rev-parse", "HEAD").strip()
        plan = fp.build_plan(self.repo, self.base, head, "fix: add adds", "")
        logs = self.tmp / "logs-lean"
        logs.mkdir(exist_ok=True)
        res = fp.run_rust(plan, self.repo, logs, timeout=600, log=lambda *_: None)
        status = {t["path"]: t["status"] for t in res["tests"]}
        self.assertEqual(status, {"tests::adds": "proven", "tests::same": "not_red"})
        self.git("checkout", "-q", "main")

    def test_non_compiling_test_file_does_not_hide_a_real_proof(self):
        """One test file needs the new API (base cannot compile it), another proves the fix with
        an assertion: the first is taken back and counts as weak, the second stays a real proof."""
        self.git("checkout", "-q", "-b", "mixed", self.base)
        lib = (self.repo / "calc/src/lib.rs").read_text()
        self.write({"calc/src/lib.rs": lib.replace("#[cfg(test)]\nmod tests {", "#[cfg(test)]\nmod checks;\n#[cfg(test)]\nmod tests {")})
        self.write({"calc/src/checks.rs": "#[test]\nfn old_check() { assert_eq!(super::double(1), 2); }\n"})
        self.git("add", "-A")
        self.git("commit", "-qm", "test: checks module")
        base = self.git("rev-parse", "HEAD").strip()
        fixed = (self.repo / "calc/src/lib.rs").read_text().replace("a - b", "a + b").replace(
            "pub fn double", "pub fn triple(a: i32) -> i32 { a * 3 }\npub fn double").replace(
            "#[test]\n    fn zero()", "#[test]\n    fn triples() { assert_eq!(triple(1), 3); }\n    #[test]\n    fn zero()")
        self.write({"calc/src/lib.rs": fixed,
                    "calc/src/checks.rs": "#[test]\nfn old_check() { assert_eq!(super::double(1), 2); }\n"
                                          "#[test]\nfn adds() { assert_eq!(super::add(2, 2), 4); }\n"})
        self.git("commit", "-qam", "fix: add adds, plus triple")
        head = self.git("rev-parse", "HEAD").strip()
        plan = fp.build_plan(self.repo, base, head, "fix: add adds", "")
        logs = self.tmp / "logs-mixed"
        logs.mkdir(exist_ok=True)
        res = fp.run_rust(plan, self.repo, logs, timeout=600, log=lambda *_: None)
        status = {t["path"]: t["status"] for t in res["tests"]}
        self.assertEqual(status, {"checks::adds": "proven", "tests::triples": "proven_build"})
        self.assertEqual(res["reverted"], ["calc/src/lib.rs"])
        self.git("checkout", "-q", "main")

    def test_fix_without_red_test_fails(self):
        self.git("checkout", "-q", "-b", "weak", self.base)
        self.write({"calc/src/lib.rs": (self.repo / "calc/src/lib.rs").read_text().replace("a - b", "a + b").replace(
            "#[test]\n    fn zero()", "#[test]\n    fn same() { assert_eq!(double(1), 2); }\n    #[test]\n    fn zero()")})
        self.git("commit", "-qam", "fix: claims a fix, proves nothing")
        head = self.git("rev-parse", "HEAD").strip()
        plan = fp.build_plan(self.repo, self.base, head, "fix: claims a fix", "")
        logs = self.tmp / "logs-weak"
        logs.mkdir(exist_ok=True)
        s = fp.combine(plan, [fp.run_rust(plan, self.repo, logs, timeout=600, log=lambda *_: None)])
        self.assertEqual(s["verdict"], "not_red")
        self.assertTrue(fp.fails(s))
        self.git("checkout", "-q", "main")


if __name__ == "__main__":
    unittest.main()
