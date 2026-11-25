#!/usr/bin/env python3
"""Fix-Proof: proves that the tests of a change catch the bug they claim to catch.

For every new or changed test of a PR the tool runs the test twice:

  1. on the base commit WITHOUT the fix, with only the PR's test code laid over it -> must be RED
  2. on the PR head WITH the fix                                                     -> must be GREEN

A test that is red before and green after is the proof that the fix changes behaviour and that
the test would catch the bug if it ever came back. A test that is green on both sides proves
nothing about the fix. The result goes into the job summary, a JSON file (read by the PR card,
docs/ai-transparency.md) and, for `fix:` PRs, decides the `fix-proof` check. See docs/fix-proof.md.

Subcommands:
  plan     which tests are new/changed, is it a fix, is there an exemption (cheap, no build)
  run      run the planned tests of one language (rust | swift) on base and head
  mutants  phase 3: mutation testing of the changed Rust functions (cargo-mutants --in-diff)
  verdict  combine plan + results into fix-proof.json, a Markdown summary and the exit code
  check    all of it locally: `python3 -I tools/fix_proof.py check --base upstream/main`

Only the Python standard library is used.
"""
import argparse
import functools
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCHEMA = 1
FIX_TITLE_RE = re.compile(r"^\s*(fix|bugfix|hotfix)(\([^)]*\))?!?\s*:", re.I)
# `Fix-Proof: skip – <reason>` in the PR body or a commit message. The reason is required.
SKIP_RE = re.compile(r"^\s*(?:[-*]\s*)?\**Fix-Proof\**\s*:\**\s*skip\b[\s:–—-]*(.*?)\s*$", re.I | re.M)
MIN_SKIP_REASON = 8
# A PR that changes the verifier itself cannot prove anything with it (the card says so).
VERIFIER_PATHS = ("tools/fix_proof.py", ".github/workflows/ci.yml")
# Inputs of the Swift bindings: if any of these differ, the base needs its own core build.
CORE_INPUT_RE = re.compile(r"^(girocrab-core/(?!.*\.md$)|uniffi-bindgen/|Cargo\.(toml|lock)$|build-xcframework\.sh$)")
SWIFT_TEST_RE = re.compile(r"^GiroCrabApp/Tests/([^/]+)/.+\.swift$")
RUST_TEST_DIR_RE = re.compile(r"(^|/)(tests|test_support|fixtures)(/|$)|(^|/)(tests|test_support)\.rs$|_tests?\.rs$")
STRONG = ("proven",)
WEAK = ("proven_build", "proven_timeout")
STATUSES = ("proven", "proven_build", "proven_timeout", "not_red", "head_failed", "not_run")
LABELS = {
    "proven": "🟥→🟩 rot → grün",
    "proven_build": "🟧→🟩 rot (ohne Fix nicht baubar) → grün",
    "proven_timeout": "🟧→🟩 rot (Timeout) → grün",
    "not_red": "🟩→🟩 grün → grün (beweist den Fix nicht)",
    "head_failed": "❌ auf dem PR-Head rot",
    "not_run": "⚪️ nicht gelaufen",
}
VERDICTS = {
    "proven": "✅ Fix bewiesen",
    "proven_weak": "🟧 Fix schwach belegt (Test auf der Base nicht baubar oder Timeout)",
    "not_red": "❌ Fix nicht bewiesen: kein neuer Test schlägt ohne den Fix fehl",
    "missing": "❌ Fix ohne neuen oder geänderten Test",
    "not_run": "⚪️ Tests nicht gelaufen",
    "skipped": "⏭️ Ausnahme begründet (Fix-Proof: skip)",
    "not_applicable": "➖ kein Fix, keine geänderten Tests",
    "info": "ℹ️ kein Fix: Ergebnis nur zur Information",
}
FAILING_VERDICTS = ("not_red", "missing", "not_run")


# --------------------------------------------------------------------------- shell + git
def sh(cmd, cwd=None, check=True, env=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(map(str, cmd[:4]))} …: {r.stderr.strip()[:600]}")
    return r.stdout


def git(root, *args, check=True):
    return sh(["git", "-C", str(root), *args], check=check)


def rev_parse(root, rev):
    return git(root, "rev-parse", "--verify", f"{rev}^{{commit}}").strip()


class Tree:
    """Read-only view of one side of the comparison: a commit, or the working tree (rev None)."""

    def __init__(self, root, rev):
        self.root, self.rev = Path(root), rev
        self._cache = {}

    def read(self, path):
        if path in self._cache:
            return self._cache[path]
        if self.rev is None:
            p = self.root / path
            text = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None
        else:
            r = subprocess.run(["git", "-C", str(self.root), "show", f"{self.rev}:{path}"],
                               capture_output=True)
            text = r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None
        self._cache[path] = text
        return text

    def read_bytes(self, path):
        if self.rev is None:
            p = self.root / path
            return p.read_bytes() if p.is_file() else None
        r = subprocess.run(["git", "-C", str(self.root), "show", f"{self.rev}:{path}"], capture_output=True)
        return r.stdout if r.returncode == 0 else None

    def exists(self, path):
        return self.read_bytes(path) is not None


def changed_files(root, base, head):
    """[(status, path, old_path)] between base and head (head None = working tree incl. untracked)."""
    args = ["diff", "--name-status", "-M", base]
    if head:
        args.append(head)
    out = git(root, *args)
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        st = parts[0][:1]
        if st == "R":
            rows.append(("R", parts[2], parts[1]))
        elif st in "AMDT":
            rows.append(("M" if st == "T" else st, parts[1], parts[1]))
    if head is None:
        for p in git(root, "ls-files", "--others", "--exclude-standard").splitlines():
            rows.append(("A", p, p))
    return rows


def commit_messages(root, base, head):
    return git(root, "log", "--format=%B%x00", f"{base}..{head or 'HEAD'}", check=False) or ""


# --------------------------------------------------------------------------- Rust lexer
IDENT_START = re.compile(r"[A-Za-z_\u0080-￿]")
IDENT_CHAR = re.compile(r"[A-Za-z0-9_\u0080-￿]")


def lex_rust(src):
    """Tokens (kind, text, start, end); comments and whitespace are dropped.
    Enough Rust to find items and match braces: strings, raw strings, chars vs lifetimes,
    nested block comments."""
    toks, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c.isspace():
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if src.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if src.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif src.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            i = j
            continue
        if IDENT_START.match(c):
            j = i + 1
            while j < n and IDENT_CHAR.match(src[j]):
                j += 1
            word = src[i:j]
            if word in ("r", "br", "cr") and j < n and src[j] in '#"':
                k = j
                while k < n and src[k] == "#":
                    k += 1
                if k < n and src[k] == '"':  # raw string
                    hashes = k - j
                    end = src.find('"' + "#" * hashes, k + 1)
                    end = n if end < 0 else end + 1 + hashes
                    toks.append(("str", src[i:end], i, end))
                    i = end
                    continue
                if word == "r" and k == j + 1 and k < n and IDENT_START.match(src[k]):  # r#ident
                    m = k + 1
                    while m < n and IDENT_CHAR.match(src[m]):
                        m += 1
                    toks.append(("ident", src[k:m], i, m))
                    i = m
                    continue
            if word in ("b", "c") and j < n and src[j] == '"':
                end = _scan_quoted(src, j, '"')
                toks.append(("str", src[i:end], i, end))
                i = end
                continue
            if word == "b" and j < n and src[j] == "'":
                end = _scan_quoted(src, j, "'")
                toks.append(("char", src[i:end], i, end))
                i = end
                continue
            toks.append(("ident", word, i, j))
            i = j
            continue
        if c == '"':
            end = _scan_quoted(src, i, '"')
            toks.append(("str", src[i:end], i, end))
            i = end
            continue
        if c == "'":
            if i + 1 < n and src[i + 1] == "\\":
                end = _scan_quoted(src, i, "'")
                toks.append(("char", src[i:end], i, end))
                i = end
                continue
            if i + 2 < n and src[i + 2] == "'":
                toks.append(("char", src[i:i + 3], i, i + 3))
                i += 3
                continue
            j = i + 1  # lifetime or label
            while j < n and IDENT_CHAR.match(src[j]):
                j += 1
            toks.append(("lifetime", src[i:j], i, j))
            i = j
            continue
        if c.isdigit():
            j = i + 1
            while j < n and (IDENT_CHAR.match(src[j]) or (src[j] == "." and j + 1 < n and src[j + 1].isdigit())):
                j += 1
            toks.append(("num", src[i:j], i, j))
            i = j
            continue
        toks.append(("punct", c, i, i + 1))
        i += 1
    return toks


def _scan_quoted(src, i, q):
    j, n = i + 1, len(src)
    while j < n:
        if src[j] == "\\":
            j += 2
            continue
        if src[j] == q:
            return j + 1
        j += 1
    return n


OPEN, CLOSE = "([{", ")]}"


def _match(toks, i):
    """Index of the token closing the bracket at toks[i]."""
    depth = 0
    for j in range(i, len(toks)):
        t = toks[j]
        if t[0] == "punct":
            if t[1] in OPEN:
                depth += 1
            elif t[1] in CLOSE:
                depth -= 1
                if depth == 0:
                    return j
    return len(toks) - 1


def _is_p(t, ch):
    return t[0] == "punct" and t[1] == ch


def _attr_text(src, toks, i):
    """toks[i] is '#', toks[i+1] '['. -> (text inside the brackets, index after ']')."""
    end = _match(toks, i + 1)
    return src[toks[i + 1][3]:toks[end][2]], end + 1


def is_test_attr(text):
    a = re.sub(r"\s+", "", text)
    if a in ("test", "cfg(test)") or re.match(r"(tokio::)?test(\(|$)", a):
        return True
    if a.startswith("cfg(all(") and re.search(r"[(,]test[,)]", a[len("cfg(all"):]):
        return True
    return False


def is_test_fn_attr(text):
    a = re.sub(r"\s+", "", text)
    return a == "test" or bool(re.match(r"(tokio::|async_std::)?test(\(|$)", a))


def _item_end(toks, j):
    """Index of the last token of the item starting at toks[j] (depth 0)."""
    k = j
    while k < len(toks):
        t = toks[k]
        if _is_p(t, ";"):
            return k
        if t[0] == "punct" and t[1] in "([":
            k = _match(toks, k) + 1
            continue
        if _is_p(t, "{"):
            end = _match(toks, k)
            if end + 1 < len(toks) and _is_p(toks[end + 1], ";"):
                end += 1
            return end
        k += 1
    return len(toks) - 1


def rust_items(src):
    """Top-level items -> [{start, end, key, test}]; `test` = gated on cfg(test) / #[test]."""
    toks = lex_rust(src)
    items, i = [], 0
    while i < len(toks):
        start_tok, attrs = i, []
        if _is_p(toks[i], "#") and i + 1 < len(toks) and _is_p(toks[i + 1], "!"):  # inner attribute
            i = _match(toks, i + 2) + 1
            continue
        while i + 1 < len(toks) and _is_p(toks[i], "#") and _is_p(toks[i + 1], "["):
            text, i = _attr_text(src, toks, i)
            attrs.append(text)
        if i >= len(toks):
            break
        end = _item_end(toks, i)
        head = []
        k = i
        while k <= end and not (toks[k][0] == "punct" and toks[k][1] in "{;="):
            head.append(toks[k][1])
            k += 1
        items.append({"start": toks[start_tok][2], "end": toks[end][3], "key": " ".join(head),
                      "test": any(is_test_attr(a) for a in attrs)})
        i = end + 1
    return items


def splice_tests(base_src, head_src):
    """Base source with its test-only items replaced by the head's: the production code stays
    as it was before the fix, the test code is the PR's."""
    base_items = [it for it in rust_items(base_src) if it["test"]]
    head_items = [it for it in rust_items(head_src) if it["test"]]
    pool = {}
    for it in head_items:
        pool.setdefault(it["key"], []).append(it)
    out, pos, used = [], 0, set()
    for it in base_items:
        out.append(base_src[pos:it["start"]])
        cands = pool.get(it["key"]) or []
        if cands:
            h = cands.pop(0)
            used.add(id(h))
            out.append(head_src[h["start"]:h["end"]])
        pos = it["end"]
    out.append(base_src[pos:])
    rest = [h for h in head_items if id(h) not in used]
    text = "".join(out)
    if rest:
        text = text.rstrip("\n") + "\n" + "".join("\n" + head_src[h["start"]:h["end"]] + "\n" for h in rest)
    return text


def rust_tests_in(src):
    """Test functions -> {"mod::path::name": normalized source of the whole test item}."""
    toks = lex_rust(src)
    found, stack, depth, attrs, attr_start = {}, [], 0, [], None
    i = 0
    while i < len(toks):
        t = toks[i]
        if _is_p(t, "#") and i + 1 < len(toks) and _is_p(toks[i + 1], "["):
            if attr_start is None:
                attr_start = t[2]
            text, i = _attr_text(src, toks, i)
            attrs.append(text)
            continue
        if t[0] == "ident" and t[1] == "mod" and i + 2 < len(toks) and toks[i + 1][0] == "ident" \
                and _is_p(toks[i + 2], "{"):
            stack.append((toks[i + 1][1], depth + 1))
            depth += 1
            attrs, attr_start = [], None
            i += 3
            continue
        if t[0] == "ident" and t[1] == "fn" and any(is_test_fn_attr(a) for a in attrs) \
                and i + 1 < len(toks) and toks[i + 1][0] == "ident":
            name = toks[i + 1][1]
            k = i + 2
            while k < len(toks) and not _is_p(toks[k], "{") and not _is_p(toks[k], ";"):
                k = _match(toks, k) + 1 if toks[k][0] == "punct" and toks[k][1] in "([" else k + 1
            end = _match(toks, k) if k < len(toks) and _is_p(toks[k], "{") else k
            path = "::".join([m for m, _ in stack] + [name])
            body = src[attr_start if attr_start is not None else t[2]:toks[min(end, len(toks) - 1)][3]]
            found[path] = " ".join(body.split())
            attrs, attr_start = [], None
            i = end + 1
            continue
        if t[0] == "ident" and t[1] in ("pub", "async", "unsafe", "const", "extern", "crate", "in", "super", "self"):
            i += 1
            continue
        if _is_p(t, "(") and attrs:  # pub(crate)
            i = _match(toks, i) + 1
            continue
        if t[0] == "punct" and t[1] in OPEN:
            depth += 1
        elif t[0] == "punct" and t[1] in CLOSE:
            depth -= 1
            while stack and depth < stack[-1][1]:
                stack.pop()
        attrs, attr_start = [], None
        i += 1
    return found


# --------------------------------------------------------------------------- Rust layout
@functools.lru_cache(maxsize=8)
def workspace_members(root):
    """Directories of the root workspace's packages (other workspaces, e.g. the Qt port, need
    their own toolchain and are left to their CI jobs)."""
    text = (Path(root) / "Cargo.toml").read_text(encoding="utf-8", errors="replace") \
        if (Path(root) / "Cargo.toml").is_file() else ""
    m = re.search(r"^members\s*=\s*\[(.*?)\]", text, re.M | re.S)
    members = set(re.findall(r'"([^"]+)"', m.group(1))) if m else set()
    if re.search(r"^\[package\]", text, re.M):
        members.add(".")
    return members


def crate_of(path, tree_root):
    """(crate_dir, package_name) for a path in the root workspace, from the nearest Cargo.toml."""
    if Path(path).parts and Path(path).parts[0] in ("target", "generated"):
        return None
    parts = Path(path).parts
    for k in range(len(parts) - 1, -1, -1):
        d = Path(*parts[:k]) if k else Path(".")
        manifest = tree_root / d / "Cargo.toml"
        if manifest.is_file():
            text = manifest.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"^\[package\][^\[]*?^name\s*=\s*\"([^\"]+)\"", text, re.M | re.S)
            if m and str(d) in workspace_members(str(tree_root)):
                return str(d), m.group(1)
            return None
    return None


def rust_location(path, crate_dir):
    """-> (target, module_prefix) for a .rs file, or None if it is not a test location we run.
    target: "lib" | "bin" | "test:<stem>"."""
    rel = Path(path).relative_to(crate_dir) if crate_dir != "." else Path(path)
    parts = rel.parts
    if parts[0] == "tests":
        if len(parts) == 2:
            return f"test:{rel.stem}", ""
        return None  # helper modules of integration tests
    if parts[0] != "src":
        return None
    mod = list(parts[1:])
    mod[-1] = mod[-1][:-3]
    if mod[-1] == "mod":
        mod = mod[:-1]
    target = "lib"
    if mod == ["lib"]:
        mod = []
    elif mod == ["main"]:
        mod, target = [], "bin"
    return target, "::".join(mod)


def _parent_module_files(path):
    """Candidate files of the module that declares `path` (Rust 2018 layout)."""
    p = Path(path)
    stem = p.parent.name if p.name == "mod.rs" else p.stem
    d = p.parent.parent if p.name == "mod.rs" else p.parent
    cands = [str(d) + ".rs", str(d / "mod.rs")]
    if d.name == "src":
        cands = [str(d / "lib.rs"), str(d / "main.rs")]
    return stem, cands


def rust_test_only(path, tree, memo=None):
    """True if the file is compiled only in test builds: a test directory/name, or a module
    declared `#[cfg(test)] mod x;` (also transitively)."""
    memo = {} if memo is None else memo
    if path in memo:
        return memo[path]
    memo[path] = False
    result = bool(RUST_TEST_DIR_RE.search(path))
    if not result and path.endswith(".rs") and "/src/" in "/" + path \
            and Path(path).name not in ("lib.rs", "main.rs"):
        stem, parents = _parent_module_files(path)
        parent = next((p for p in parents if tree.read(p) is not None), parents[0])
        for it in rust_items(tree.read(parent) or ""):
            if re.search(rf"(^| )mod {re.escape(stem)}$", it["key"]):
                result = it["test"]
                break
        # a child of a test-only module is test-only, whatever its own declaration says
        result = result or rust_test_only(parent, tree, memo)
    memo[path] = result
    return result


# --------------------------------------------------------------------------- Swift
def lex_swift_strings(src):
    """Spans of comments and string literals (incl. multi-line, raw, interpolation)."""
    spans, i, n = [], 0, len(src)

    def scan_string(i):
        hashes = 0
        while i < n and src[i] == "#":
            hashes, i = hashes + 1, i + 1
        multi = src.startswith('"""', i)
        q = '"""' if multi else '"'
        j = i + len(q)
        close = q + "#" * hashes
        esc = "\\" + "#" * hashes
        while j < n:
            if src.startswith(esc + "(", j):
                depth, j = 1, j + len(esc) + 1
                while j < n and depth:
                    if src[j] == '"' or (src[j] == "#" and src.startswith('"', j + 1)):
                        j = scan_string(j)
                        continue
                    depth += src[j] == "("
                    depth -= src[j] == ")"
                    j += 1
                continue
            if src.startswith(esc, j) and hashes == 0:
                j += 2
                continue
            if src.startswith(close, j):
                return j + len(close)
            if not multi and src[j] == "\n":
                return j
            j += 1
        return n

    while i < n:
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            spans.append((i, j))
            i = j
        elif src.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if src.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif src.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            spans.append((i, j))
            i = j
        elif src[i] == '"' or (src[i] == "#" and re.match(r'#+"', src[i:i + 8])):
            j = scan_string(i)
            spans.append((i, j))
            i = j
        else:
            i += 1
    return spans


def _swift_code_mask(src):
    """Source with comments and string contents blanked out (same length)."""
    chars = list(src)
    for a, b in lex_swift_strings(src):
        for k in range(a, b):
            if chars[k] != "\n":
                chars[k] = " "
    return "".join(chars)


def _swift_block_end(code, open_idx):
    depth = 0
    for k in range(open_idx, len(code)):
        if code[k] == "{":
            depth += 1
        elif code[k] == "}":
            depth -= 1
            if depth == 0:
                return k
    return len(code) - 1


SWIFT_TYPE_RE = re.compile(
    r"(?P<attrs>(?:@\w+(?:\([^)]*\))?\s+)*)(?:(?:public|internal|private|fileprivate|final|open)\s+)*"
    r"(?P<kind>class|struct|extension|enum|actor)\s+(?P<name>[A-Za-z_][\w.]*)(?P<rest>[^{]*)\{")
SWIFT_FUNC_RE = re.compile(r"(?P<attrs>(?:@\w+(?:\([^)]*\))?\s+)*)(?:(?:public|internal|private|fileprivate|final|"
                           r"override|static|nonisolated|@MainActor)\s+)*func\s+(?P<name>[A-Za-z_]\w*)\s*\(")


def swift_tests_in(src):
    """-> {"Type/testName" (XCTest) | "Type/name()" (Swift Testing): normalized source}."""
    code = _swift_code_mask(src)
    found = {}
    xctest_types = set()
    for m in SWIFT_TYPE_RE.finditer(code):
        if m.group("kind") == "class" and "XCTestCase" in m.group("rest"):
            xctest_types.add(m.group("name"))
    for m in SWIFT_TYPE_RE.finditer(code):
        name, kind = m.group("name"), m.group("kind")
        xctest = name in xctest_types or (kind == "extension" and name.endswith("Tests"))
        suite = "@Suite" in m.group("attrs") or kind in ("struct", "class", "actor", "extension")
        start, end = m.end() - 1, _swift_block_end(code, m.end() - 1)
        body = code[start + 1:end]
        depth, k = 0, 0
        for f in SWIFT_FUNC_RE.finditer(body):
            # only functions directly in this type (depth 0 inside the braces)
            seg = body[k:f.start()]
            depth += seg.count("{") - seg.count("}")
            k = f.start()
            if depth != 0:
                continue
            fname = f.group("name")
            is_testing = "@Test" in f.group("attrs")
            if not ((xctest and fname.startswith("test")) or (is_testing and suite)):
                continue
            brace = body.find("{", f.end())
            if brace < 0:
                continue
            fend = _swift_block_end(body, brace)
            text = src[start + 1 + f.start():start + 1 + fend + 1]
            key = f"{name}/{fname}()" if is_testing else f"{name}/{fname}"
            found[key] = " ".join(text.split())
    return found


# --------------------------------------------------------------------------- plan
def is_fix(title, messages):
    if FIX_TITLE_RE.search(title or ""):
        return True
    subjects = [m.strip().splitlines()[0] for m in messages.split("\x00") if m.strip()]
    return bool(subjects) and not title and all(FIX_TITLE_RE.search(s) for s in subjects)


def find_exemption(body, messages):
    body = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)  # template hints are not statements
    for text in (body, (messages or "").replace("\x00", "\n")):
        for m in SKIP_RE.finditer(text):
            reason = m.group(1).strip(" *_`")
            if len(reason) >= MIN_SKIP_REASON:
                return reason[:300]
    return None


def build_plan(root, base, head=None, title="", body=""):
    root = Path(root).resolve()
    base_sha = rev_parse(root, base)
    head_sha = rev_parse(root, head) if head else None
    btree, htree = Tree(root, base_sha), Tree(root, head_sha)
    rows = changed_files(root, base_sha, head_sha)
    messages = commit_messages(root, base_sha, head_sha)
    tests, memo = [], {}
    rust_prod = []
    for status, path, old in rows:
        if status == "D":
            continue
        if path.endswith(".rs"):
            crate = crate_of(path, root)
            if not crate:
                continue
            crate_dir, pkg = crate
            loc = rust_location(path, crate_dir)
            test_only = rust_test_only(path, htree, memo)
            if not test_only:
                rust_prod.append(path)
            if not loc:
                continue
            head_src, base_src = htree.read(path) or "", btree.read(old) or ""
            before = rust_tests_in(base_src)
            target, prefix = loc
            for name, text in rust_tests_in(head_src).items():
                if before.get(name) == text:
                    continue
                full = "::".join(x for x in (prefix, name) if x)
                tests.append({"lang": "rust", "package": pkg, "crate_dir": crate_dir, "target": target,
                              "path": full, "file": path, "change": "modified" if name in before else "added"})
        m = SWIFT_TEST_RE.match(path)
        if m:
            before = swift_tests_in(btree.read(old) or "")
            for name, text in swift_tests_in(htree.read(path) or "").items():
                if before.get(name) == text:
                    continue
                tests.append({"lang": "swift", "package": "GiroCrabApp", "target": m.group(1),
                              "path": f"{m.group(1)}.{name}", "file": path,
                              "change": "modified" if name in before else "added"})
    paths = [p for _, p, _ in rows] + [o for s, _, o in rows if s == "R"]
    return {
        "schema": SCHEMA,
        "base": base_sha,
        "head": head_sha,
        "title": title or "",
        "is_fix": is_fix(title, messages),
        "exempt": find_exemption(body, messages),
        "verifier_changed": any(p in VERIFIER_PATHS for p in paths),
        "core_changed": any(CORE_INPUT_RE.search(p) for p in paths),
        "rust_production_changed": sorted(rust_prod),
        "changed": [[s, p, o] for s, p, o in rows],
        "tests": tests,
    }


# --------------------------------------------------------------------------- overlays
def apply_overlay(plan, root, wt, lang):
    """Lay the PR's test code over the base worktree `wt`. Production code stays at base."""
    root = Path(root)
    htree, btree = Tree(root, plan["head"]), Tree(root, plan["base"])
    memo, written = {}, []
    for status, path, old in plan["changed"]:
        target = Path(wt) / path
        if lang == "rust":
            if status == "D":
                if rust_test_only(path, btree, {}) and target.exists():
                    target.unlink()
                    written.append(f"-{path}")
                continue
            if rust_test_only(path, htree, memo):
                if not _in_crate(path, root):
                    continue
                data = htree.read_bytes(path)
                if data is not None:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    written.append(path)
                if status == "R" and old != path and (Path(wt) / old).exists():
                    (Path(wt) / old).unlink()
                continue
            if path.endswith(".rs") and status in ("M", "R") and _in_crate(path, root):
                base_src, head_src = btree.read(old), htree.read(path)
                if base_src is None or head_src is None:
                    continue
                spliced = splice_tests(base_src, head_src)
                if spliced != base_src:
                    (Path(wt) / old).write_text(spliced, encoding="utf-8")
                    written.append(f"~{old}")
        elif lang == "swift":
            if not path.startswith("GiroCrabApp/Tests/"):
                continue
            if status == "D":
                if target.exists():
                    target.unlink()
                    written.append(f"-{path}")
                continue
            data = htree.read_bytes(path)
            if data is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                written.append(path)
            if status == "R" and old != path and (Path(wt) / old).exists():
                (Path(wt) / old).unlink()
    return written


def _in_crate(path, root):
    return crate_of(path, Path(root)) is not None


ERROR_LOC_RE = re.compile(r"^\s*--> ([^:\s]+\.rs):\d+:\d+")


def compile_error_files(output):
    """Source files named by rustc errors (not warnings), relative to the workspace."""
    lines, files = output.splitlines(), []
    for i, line in enumerate(lines):
        if re.match(r"^error(\[E\d+\])?: ", line):
            for nxt in lines[i + 1:i + 4]:
                m = ERROR_LOC_RE.match(nxt)
                if m:
                    files.append(m.group(1))
                    break
    return list(dict.fromkeys(files))


def restore_base_file(plan, root, wt, path):
    """Undo the overlay for one file: back to the base version (or gone, if the PR added it)."""
    data = Tree(root, plan["base"]).read_bytes(path)
    target = Path(wt) / path
    if data is None:
        if target.exists():
            target.unlink()
    else:
        target.write_bytes(data)


class BaseWorktree:
    def __init__(self, root, rev, keep=False):
        self.root, self.rev, self.keep = Path(root), rev, keep
        self.path = Path(tempfile.mkdtemp(prefix="fix-proof-base-"))

    def __enter__(self):
        git(self.root, "worktree", "add", "--detach", "--force", str(self.path), self.rev)
        return self.path

    def __exit__(self, *exc):
        if self.keep:
            print(f"fix-proof: base worktree kept at {self.path}", file=sys.stderr)
            return
        git(self.root, "worktree", "remove", "--force", str(self.path), check=False)
        shutil.rmtree(self.path, ignore_errors=True)
        git(self.root, "worktree", "prune", check=False)


class HeadDir:
    """Directory holding the head side: the checkout itself when it is the planned head (CI, or
    the working tree locally), otherwise a worktree of that commit."""

    def __init__(self, root, plan, keep=False):
        self.root, self.plan, self.keep, self.wt = Path(root), plan, keep, None

    def __enter__(self):
        head = self.plan["head"]
        if head is None or rev_parse(self.root, "HEAD") == head:
            return self.root
        self.wt = BaseWorktree(self.root, head, self.keep)
        return self.wt.__enter__()

    def __exit__(self, *exc):
        if self.wt:
            self.wt.__exit__(*exc)


# --------------------------------------------------------------------------- running
def run_logged(cmd, cwd, env, log_path, timeout):
    """Run cmd in its own process group; on timeout kill the whole group.
    -> (returncode | None on timeout, output)."""
    start = time.time()
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        log.write(f"$ {' '.join(map(str, cmd))}\n  (cwd: {cwd})\n\n")
        log.flush()
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True)
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.wait()
            rc = None
        log.write(f"\n[fix-proof] exit={rc} after {time.time() - start:.0f}s\n")
    return rc, Path(log_path).read_text(encoding="utf-8", errors="replace")


CARGO_RUNNING_RE = re.compile(r"^\s*Running (unittests )?(\S+)", re.M)
CARGO_RESULT_RE = re.compile(r"^test (\S+)(?: - should panic)? \.\.\. (ok|FAILED|ignored)\b")


def parse_cargo(output):
    """-> {(target, test path): "ok" | "FAILED" | "ignored"} and whether the build failed."""
    results, target = {}, None
    for line in output.splitlines():
        m = CARGO_RUNNING_RE.match(line)
        if m:
            src = m.group(2)
            if m.group(1):
                target = "bin" if src.endswith("main.rs") or "/bin/" in src else "lib"
            elif src.startswith("tests/"):
                target = "test:" + Path(src).stem
            else:
                target = "other"
            continue
        m = CARGO_RESULT_RE.match(line)
        if m and target:
            results[(target, m.group(1))] = m.group(2)
    build_failed = bool(re.search(r"^error: could not compile|^error\[E\d+\]|^error: no (test|library|bin) target",
                                  output, re.M))
    return results, build_failed


# Cargo hashes workspace members by their path relative to the workspace, so the same crate in
# two worktrees is ONE unit in a shared target dir: the base run would reuse the head binary
# (its sources are older than the head build). A per-package profile override that changes
# nothing about the code gives each side its own unit; dependencies stay shared.
UNIT_VARIANT = {"base": 255, "head-worktree": 254}
MAX_REVERT_ROUNDS = 4  # base builds after taking back test files that need the fix to compile


def cargo_cmd(tests, package, crate_has_lib, variant=None):
    targets = sorted({t["target"] for t in tests})
    flags = []
    for tg in targets:
        if tg == "lib":
            flags.append("--lib" if crate_has_lib else "--bins")
        elif tg == "bin":
            flags.append("--bins")
        elif tg.startswith("test:"):
            flags += ["--test", tg[5:]]
    names = sorted({t["path"] for t in tests})
    cfg = []
    if variant:
        for prof in ("dev", "test"):
            cfg += ["--config", f'profile.{prof}.package."{package}".codegen-units={UNIT_VARIANT[variant]}']
    return ["cargo", *cfg, "test", "-p", package, *dict.fromkeys(flags), "--no-fail-fast", "--", "--exact", *names]


def classify(base_status, head_status, base_build_failed, base_timed_out):
    if head_status is None or head_status == "ignored":
        return "not_run"
    if head_status == "FAILED":
        return "head_failed"
    if base_status == "FAILED":
        return "proven"
    if base_status == "ok":
        return "not_red"
    if base_timed_out:
        return "proven_timeout"
    if base_build_failed or base_status is None:
        return "proven_build"
    return "not_run"  # ignored on base


def run_rust(plan, root, out_dir, timeout=1800, keep=False, log=print):
    root = Path(root).resolve()
    tests = [t for t in plan["tests"] if t["lang"] == "rust"]
    out = {"lang": "rust", "tests": [], "runs": []}
    if not tests:
        out["status"] = "no_tests"
        return out
    env = dict(os.environ, GIROCRAB_SKIP_SWIFT="1", CARGO_TERM_COLOR="never", RUST_BACKTRACE="0",
               CARGO_TARGET_DIR=os.environ.get("CARGO_TARGET_DIR", str(root / "target")))
    # One cargo invocation per test target: a base compile error in the unit tests must not
    # hide an integration test that does compile (and can be red on its own).
    by_target = {}
    for t in tests:
        by_target.setdefault((t["package"], t["crate_dir"], t["target"]), []).append(t)
    head_res, base_res = {}, {}
    with HeadDir(root, plan, keep) as hd, BaseWorktree(root, plan["base"], keep) as wt:
        written = apply_overlay(plan, root, wt, "rust")
        log(f"fix-proof: base worktree {wt}, test overlay: {', '.join(written) or '–'}")
        overlay = {w.lstrip("~-") for w in written}
        reverted = set()
        for (pkg, crate_dir, target), ts in by_target.items():
            has_lib = (root / crate_dir / "src" / "lib.rs").exists()
            slug = re.sub(r"[^\w.-]+", "_", f"{pkg}-{target}")
            for side, cwd, store in (("head", hd, head_res), ("base", wt, base_res)):
                variant = "base" if side == "base" else ("head-worktree" if Path(hd) != root else None)
                cmd = cargo_cmd(ts, pkg, has_lib, variant)
                for attempt in range(MAX_REVERT_ROUNDS + 1):
                    suffix = f"-retry{attempt}" if attempt else ""
                    log(f"fix-proof: {side}{suffix}: {' '.join(cmd)}")
                    rc, text = run_logged(cmd, cwd, env, Path(out_dir) / f"rust-{side}-{slug}{suffix}.log", timeout)
                    results, build_failed = parse_cargo(text)
                    out["runs"].append({"side": side, "package": pkg, "target": target, "exit": rc,
                                        "timed_out": rc is None, "build_failed": build_failed,
                                        "results": len(results), "attempt": attempt})
                    if side == "head" or not build_failed:
                        break
                    # Some test code needs what only the fix adds, so the base does not compile.
                    # Take back the overlay of exactly those files and build again: their tests
                    # count as weak proof, every other test still gets a real red/green answer.
                    culprits = [f for f in compile_error_files(text) if f in overlay and f not in reverted]
                    if not culprits:
                        break
                    for f in culprits:
                        restore_base_file(plan, root, wt, f)
                    reverted.update(culprits)
                    log(f"fix-proof: base does not compile with the PR's tests in {', '.join(culprits)};"
                        " those stay at base, retrying")
                store[(pkg, target)] = (results, build_failed, rc is None)
        out["reverted"] = sorted(reverted)
    for t in tests:
        hr, _, _ = head_res.get((t["package"], t["target"]), ({}, False, False))
        br, bfail, btime = base_res.get((t["package"], t["target"]), ({}, False, False))
        key = (t["target"], t["path"])
        status = classify(br.get(key), hr.get(key), bfail, btime)
        out["tests"].append({**t, "status": status, "base": br.get(key), "head": hr.get(key),
                             "overlay_reverted": t["file"] in out["reverted"]})
    out["status"] = "ran"
    return out


XCTEST_RE = re.compile(r"Test Case '-?\[?(?:(\w+)\.)?(\w+)[ .](\w+)\]?' (passed|failed|skipped)")
SWIFT_TESTING_RE = re.compile(r"^\S*\s*Test (?:\"[^\"]*\" )?(\w+)\(\) (passed|failed|skipped)", re.M)
SWIFT_TESTING_ISSUE_RE = re.compile(r"Test (?:\"[^\"]*\" )?(\w+)\(\) (?:recorded an issue|failed)")


def parse_swift(output):
    """-> {"Type/test" | "func()": "ok" | "FAILED" | "ignored"}, build_failed."""
    res = {}
    for m in XCTEST_RE.finditer(output):
        st = {"passed": "ok", "failed": "FAILED", "skipped": "ignored"}[m.group(4)]
        key = f"{m.group(2)}/{m.group(3)}"
        if res.get(key) != "FAILED":
            res[key] = st
    for m in SWIFT_TESTING_RE.finditer(output):
        st = {"passed": "ok", "failed": "FAILED", "skipped": "ignored"}[m.group(2)]
        key = f"{m.group(1)}()"
        if res.get(key) != "FAILED":
            res[key] = st
    for m in SWIFT_TESTING_ISSUE_RE.finditer(output):
        res[f"{m.group(1)}()"] = "FAILED"
    build_failed = not res and bool(re.search(r"error: |Compiling.*failed|build had \d+ command failure", output))
    return res, build_failed


def swift_filter(tests):
    parts = []
    for t in tests:
        target, rest = t["path"].split(".", 1)
        typ, name = rest.split("/", 1)
        parts.append(re.escape(f"{target}.{typ}/{name}"))
    return "^(" + "|".join(parts) + ")$"


def swift_lookup(results, t):
    rest = t["path"].split(".", 1)[1]
    if rest in results:
        return results[rest]
    name = rest.split("/", 1)[1]
    return results.get(name) if name.endswith("()") else None


def run_swift(plan, root, out_dir, timeout=2400, keep=False, log=print):
    root = Path(root).resolve()
    tests = [t for t in plan["tests"] if t["lang"] == "swift"]
    out = {"lang": "swift", "tests": [], "runs": []}
    if not tests:
        out["status"] = "no_tests"
        return out
    if shutil.which("swift") is None:
        out["status"] = "no_toolchain"
        out["tests"] = [{**t, "status": "not_run", "base": None, "head": None} for t in tests]
        return out
    env = dict(os.environ)
    cmd = ["swift", "test", "--filter", swift_filter(tests)]
    side_res, base_infra = {}, None
    with HeadDir(root, plan, keep) as hd, BaseWorktree(root, plan["base"], keep) as wt:
        written = apply_overlay(plan, root, wt, "swift")
        log(f"fix-proof: base worktree {wt}, test overlay: {', '.join(written) or '–'}")
        gen = root / "generated" / "GiroCrabCorePackage"
        if plan["core_changed"] or not gen.is_dir():
            log("fix-proof: core differs between base and head (or no head bindings) -> building the base bindings")
            benv = dict(env, GIROCRAB_TARGET_DIR=str(root / "target" / "apple"))
            rc, _ = run_logged(["./build-xcframework.sh", "--macos-only"], wt, benv,
                               Path(out_dir) / "swift-base-core.log", timeout)
            if rc != 0:  # not the tests' fault: the base cannot be judged
                base_infra = "Base-Core nicht baubar"
                out["runs"].append({"side": "base-core", "exit": rc, "build_failed": True})
        elif gen.is_dir():
            shutil.copytree(gen, Path(wt) / "generated" / "GiroCrabCorePackage", symlinks=True, dirs_exist_ok=True)
        if Path(hd) != root and gen.is_dir():
            shutil.copytree(gen, Path(hd) / "generated" / "GiroCrabCorePackage", symlinks=True, dirs_exist_ok=True)
        for side, cwd in (("head", Path(hd) / "GiroCrabApp"), ("base", Path(wt) / "GiroCrabApp")):
            if side == "base" and base_infra:
                side_res[side] = ({}, False, False)
                continue
            log(f"fix-proof: {side}: {' '.join(cmd)}")
            rc, text = run_logged(cmd, cwd, env, Path(out_dir) / f"swift-{side}.log", timeout)
            results, build_failed = parse_swift(text)
            side_res[side] = (results, build_failed, rc is None)
            out["runs"].append({"side": side, "exit": rc, "timed_out": rc is None,
                                "build_failed": build_failed, "results": len(results)})
    hr, _, _ = side_res["head"]
    br, bfail, btime = side_res["base"]
    for t in tests:
        h, b = swift_lookup(hr, t), swift_lookup(br, t)
        status = "not_run" if base_infra and h == "ok" else classify(b, h, bfail, btime)
        out["tests"].append({**t, "status": status, "base": b, "head": h})
    out["status"] = "ran" if not base_infra else "base_infra_failed"
    if base_infra:
        out["reason"] = base_infra
    return out


# --------------------------------------------------------------------------- phase 3: mutants
MUTANT_LINE_RE = re.compile(r"^(?P<file>[^:\s]+\.rs):(?P<line>\d+)(?::\d+)?:\s*(?P<what>.+)$")


def run_mutants(plan, root, out_dir, max_mutants=12, timeout=1500, in_place=None, log=print):
    """Mutation testing of the Rust functions the PR changed. Report only: a surviving mutant
    is a hint that a test is missing, not a proof (some mutants are equivalent)."""
    root = Path(root).resolve()
    res = {"status": "skipped", "total": 0, "tested": 0, "caught": 0, "missed": 0, "timeout": 0,
           "unviable": 0, "sampled": False, "missed_list": []}
    prod = [p for p in plan["rust_production_changed"] if crate_of(p, root)]
    if not prod:
        res["reason"] = "keine geänderte Rust-Produktionsdatei"
        return res
    if shutil.which("cargo-mutants") is None:
        res["status"], res["reason"] = "no_tool", "cargo-mutants nicht installiert"
        return res
    pkgs = sorted({crate_of(p, root)[1] for p in prod})
    diff = git(root, "diff", "--unified=3", plan["base"], *([plan["head"]] if plan["head"] else []), "--", *prod)
    diff_path = Path(out_dir) / "mutants.diff"
    diff_path.write_text(diff, encoding="utf-8")
    env = dict(os.environ, GIROCRAB_SKIP_SWIFT="1", CARGO_TERM_COLOR="never")
    base = ["cargo", "mutants", "--in-diff", str(diff_path), "--no-shuffle"]
    for p in pkgs:
        base += ["-p", p]
    listing = subprocess.run(base + ["--list"], cwd=root, env=env, capture_output=True, text=True)
    if listing.returncode != 0:
        res["status"], res["reason"] = "error", listing.stderr.strip()[-400:]
        return res
    total = len([line for line in listing.stdout.splitlines() if line.strip()])
    res["total"] = total
    if total == 0:
        res["status"] = "no_mutants"
        return res
    shards = max(1, math.ceil(total / max_mutants))
    cmd = base + ["--output", str(out_dir), "--timeout", "300", "--build-timeout", "600"]
    if shards > 1:
        cmd += ["--shard", f"0/{shards}", "--sharding", "round-robin"]
        res["sampled"] = True
    if in_place if in_place is not None else bool(os.environ.get("CI")):
        cmd.append("--in-place")
    cmd += ["--", "--lib"] if all((root / crate_of(p, root)[0] / "src" / "lib.rs").exists() for p in prod) else []
    log(f"fix-proof: mutants: {' '.join(cmd)}")
    rc, _ = run_logged(cmd, root, env, Path(out_dir) / "mutants.log", timeout)
    mo = Path(out_dir) / "mutants.out"
    if not mo.is_dir():
        res["status"], res["reason"] = "error", f"cargo-mutants exit {rc}, keine Ausgabe"
        return res

    def lines(name):
        f = mo / name
        return [x for x in f.read_text(encoding="utf-8").splitlines() if x.strip()] if f.exists() else []

    caught, missed, tout, unviable = lines("caught.txt"), lines("missed.txt"), lines("timeout.txt"), lines("unviable.txt")
    res.update(status="ran" if rc is not None else "timeout", caught=len(caught), missed=len(missed),
               timeout=len(tout), unviable=len(unviable), tested=len(caught) + len(missed) + len(tout) + len(unviable))
    for x in missed[:10]:
        m = MUTANT_LINE_RE.match(x)
        res["missed_list"].append({"file": m.group("file"), "line": int(m.group("line")), "mutation": m.group("what")[:160]}
                                  if m else {"file": "", "line": 0, "mutation": x[:160]})
    return res


# --------------------------------------------------------------------------- verdict
def combine(plan, results, mutants=None):
    by_lang = {r["lang"]: r for r in results}
    tests = []
    for t in plan["tests"]:
        r = by_lang.get(t["lang"])
        hit = next((x for x in (r or {}).get("tests", []) if x["path"] == t["path"] and x.get("file") == t["file"]), None)
        tests.append({"lang": t["lang"], "path": t["path"], "file": t["file"], "change": t["change"],
                      "status": hit["status"] if hit else "not_run"})
    counts = {s: sum(1 for t in tests if t["status"] == s) for s in STATUSES}
    strong = sum(counts[s] for s in STRONG)
    weak = sum(counts[s] for s in WEAK)
    if not tests:
        verdict = "missing" if plan["is_fix"] else "not_applicable"
    elif strong:
        verdict = "proven"
    elif weak:
        verdict = "proven_weak"
    elif counts["not_red"]:
        verdict = "not_red"
    else:
        verdict = "not_run"
    if plan["is_fix"] and plan["exempt"] and verdict in FAILING_VERDICTS:
        verdict = "skipped"
    if not plan["is_fix"] and verdict in FAILING_VERDICTS and tests:
        verdict = "info"
    return {
        "schema": SCHEMA,
        "verdict": verdict,
        "is_fix": plan["is_fix"],
        "exempt": plan["exempt"],
        "verifier_changed": plan["verifier_changed"],
        "base": plan["base"],
        "head": plan["head"],
        "counts": counts,
        "tests": tests[:50],
        "tests_total": len(tests),
        "languages": {r["lang"]: r.get("status") for r in results},
        "mutants": mutants,
    }


def fails(summary):
    return summary["is_fix"] and summary["verdict"] in FAILING_VERDICTS


def render_summary(s):
    lines = ["## 🧪 Fix-Proof", "",
             f"**{VERDICTS[s['verdict']]}**" + (" · `fix:`-PR" if s["is_fix"] else ""), ""]
    if s["exempt"]:
        lines += [f"Ausnahme: {s['exempt']}", ""]
    if s["verifier_changed"]:
        lines += ["⚠️ Dieser PR ändert den Prüfer selbst (`tools/fix_proof.py` oder `ci.yml`): "
                  "das Ergebnis gilt auf der PR-Card nicht als Beleg.", ""]
    if s["tests"]:
        lines += ["| Test | Sprache | Ergebnis |", "|---|---|---|"]
        for t in s["tests"]:
            lines.append(f"| `{t['path']}` | {t['lang']} | {LABELS[t['status']]} |")
        if s["tests_total"] > len(s["tests"]):
            lines.append(f"| … {s['tests_total'] - len(s['tests'])} weitere | | |")
        lines.append("")
    elif s["is_fix"]:
        lines += ["Ein Fix braucht einen Test, der ohne den Fix fehlschlägt (rot) und mit ihm besteht (grün). "
                  "Ausnahme nur mit Begründung: `Fix-Proof: skip – <Grund>` im PR-Body.", ""]
    m = s.get("mutants")
    if m:
        lines += render_mutants(m)
    lines.append("<sub>Rot auf der Base ohne den Fix, grün mit ihm: docs/fix-proof.md</sub>")
    return "\n".join(lines) + "\n"


def render_mutants(m):
    """Markdown lines for the mutation section. A timeout counts as detected (the mutant made a
    test hang), an unviable mutant (does not compile) as neither."""
    lines = ["### 🧬 Mutation Testing (geänderte Rust-Funktionen)"]
    if m["status"] in ("ran", "timeout"):
        detected = m["caught"] + m["timeout"]
        lines.append(f"{detected}/{detected + m['missed']} Mutanten von den Tests erkannt"
                     + (f", {m['unviable']} nicht baubar" if m["unviable"] else "")
                     + (f" (Stichprobe aus {m['total']})" if m["sampled"] else ""))
        for x in m["missed_list"]:
            lines.append(f"- überlebt: `{x['file']}:{x['line']}` {x['mutation']}")
    else:
        lines.append(f"nicht gelaufen: {m.get('reason') or m['status']}")
    lines.append("")
    return lines


# --------------------------------------------------------------------------- CLI
def _load(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def _dump(obj, p):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def cmd_plan(a):
    body = Path(a.body_file).read_text(encoding="utf-8") if a.body_file and Path(a.body_file).exists() else ""
    plan = build_plan(a.root, a.base, a.head, a.title or "", body)
    _dump(plan, a.out)
    langs = sorted({t["lang"] for t in plan["tests"]})
    print(f"fix-proof plan: fix={plan['is_fix']} tests={len(plan['tests'])} langs={','.join(langs) or '-'}"
          f" exempt={'yes' if plan['exempt'] else 'no'}")
    if a.github_output:
        with open(a.github_output, "a", encoding="utf-8") as f:
            f.write(f"rust={'true' if 'rust' in langs else 'false'}\n")
            f.write(f"swift={'true' if 'swift' in langs else 'false'}\n")
            f.write(f"mutants={'true' if plan['rust_production_changed'] else 'false'}\n")


def cmd_run(a):
    plan = _load(a.plan)
    Path(a.logs).mkdir(parents=True, exist_ok=True)
    fn = run_rust if a.lang == "rust" else run_swift
    res = fn(plan, a.root, a.logs, timeout=a.timeout, keep=a.keep)
    _dump(res, a.out)
    for t in res["tests"]:
        print(f"  {LABELS[t['status']]:<45} {t['path']}")


def cmd_mutants(a):
    plan = _load(a.plan)
    Path(a.logs).mkdir(parents=True, exist_ok=True)
    res = run_mutants(plan, a.root, a.logs, max_mutants=a.max, timeout=a.timeout)
    _dump(res, a.out)
    if a.summary:
        with open(a.summary, "a", encoding="utf-8") as f:
            f.write("\n".join(render_mutants(res) + ["<sub>Hinweis, kein Gate: docs/fix-proof.md</sub>", ""]))
    print(f"fix-proof mutants: {res['status']} caught={res['caught']} missed={res['missed']} total={res['total']}")


def cmd_verdict(a):
    plan = _load(a.plan)
    results = [_load(p) for p in a.results if Path(p).exists()]
    mutants = _load(a.mutants) if a.mutants and Path(a.mutants).exists() else None
    s = combine(plan, results, mutants)
    md = render_summary(s)
    if a.json:
        _dump(s, a.json)
    if a.summary:
        with open(a.summary, "a", encoding="utf-8") as f:
            f.write(md)
    sys.stdout.write(md)
    if a.enforce and fails(s):
        print("::error::Fix-Proof: " + VERDICTS[s["verdict"]] + " (docs/fix-proof.md)", file=sys.stderr)
        return 1
    return 0


def cmd_check(a):
    root = Path(a.root).resolve()
    base = git(root, "merge-base", a.base, a.head or "HEAD").strip()
    out = Path(a.out or tempfile.mkdtemp(prefix="fix-proof-"))
    out.mkdir(parents=True, exist_ok=True)
    title = a.title
    if title is None:
        ahead = git(root, "rev-list", "--count", f"{base}..{a.head or 'HEAD'}").strip() != "0"
        title = git(root, "log", "-1", "--format=%s", a.head or "HEAD").strip() if ahead else ""
    plan = build_plan(root, base, a.head, title or "", "")
    if a.head is None and git(root, "status", "--porcelain"):
        print("fix-proof: uncommitted changes are included (head = working tree)", file=sys.stderr)
    _dump(plan, out / "plan.json")
    results = [run_rust(plan, root, out, timeout=a.timeout, keep=a.keep)]
    if a.swift:
        results.append(run_swift(plan, root, out, timeout=a.timeout, keep=a.keep))
    mutants = run_mutants(plan, root, out, max_mutants=a.max_mutants, in_place=True) if a.mutants else None
    s = combine(plan, results, mutants)
    _dump(s, out / "fix-proof.json")
    sys.stdout.write(render_summary(s))
    print(f"\nLogs: {out}", file=sys.stderr)
    return 1 if fails(s) else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="repository root (default: .)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--base", required=True)
    p.add_argument("--head", help="commit (default: working tree)")
    p.add_argument("--title", default="")
    p.add_argument("--body-file")
    p.add_argument("--out", required=True)
    p.add_argument("--github-output", help="append rust/swift/mutants flags (GitHub Actions)")
    r = sub.add_parser("run")
    r.add_argument("--plan", required=True)
    r.add_argument("--lang", choices=("rust", "swift"), required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--logs", default="fix-proof-logs")
    r.add_argument("--timeout", type=int, default=1800, help="seconds per cargo/swift invocation")
    r.add_argument("--keep", action="store_true", help="keep the base worktree for inspection")
    m = sub.add_parser("mutants")
    m.add_argument("--plan", required=True)
    m.add_argument("--out", required=True)
    m.add_argument("--logs", default="fix-proof-logs")
    m.add_argument("--max", type=int, default=12, help="mutants per run (~1 min each); more are sampled")
    m.add_argument("--timeout", type=int, default=1500)
    m.add_argument("--summary", help="append the Markdown section here (e.g. $GITHUB_STEP_SUMMARY)")
    v = sub.add_parser("verdict")
    v.add_argument("--plan", required=True)
    v.add_argument("--results", nargs="*", default=[])
    v.add_argument("--mutants")
    v.add_argument("--json")
    v.add_argument("--summary", help="append the Markdown summary here (e.g. $GITHUB_STEP_SUMMARY)")
    v.add_argument("--enforce", action="store_true", help="exit 1 when a fix: PR is not proven")
    c = sub.add_parser("check", help="local run: plan + Rust (+ Swift) + verdict")
    c.add_argument("--base", default="upstream/main")
    c.add_argument("--head", help="commit (default: working tree)")
    c.add_argument("--title", help="PR title (default: last commit subject)")
    c.add_argument("--out")
    c.add_argument("--swift", action="store_true", help="also run the Swift tests (macOS)")
    c.add_argument("--mutants", action="store_true", help="also run cargo-mutants (mutates files in place)")
    c.add_argument("--max-mutants", type=int, default=12)
    c.add_argument("--timeout", type=int, default=1800)
    c.add_argument("--keep", action="store_true")
    a = ap.parse_args(argv)
    try:
        rc = {"plan": cmd_plan, "run": cmd_run, "mutants": cmd_mutants, "verdict": cmd_verdict,
              "check": cmd_check}[a.cmd](a)
    except RuntimeError as err:
        sys.exit(f"fix-proof: {err}")
    return rc or 0


if __name__ == "__main__":
    sys.exit(main())
