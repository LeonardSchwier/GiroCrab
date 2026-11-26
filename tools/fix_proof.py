#!/usr/bin/env python3
"""Fix-Proof: proves that the tests of a change catch the bug they claim to catch.

For every new or changed test of a PR the tool runs the test twice:

  1. on the base commit WITHOUT the fix, with only the PR's test code laid over it -> must be RED
  2. on the PR head WITH the fix                                                     -> must be GREEN

A test that is red before and green after is the proof that the fix changes behaviour and that
the test would catch the bug if it ever came back. A test that is green on both sides proves
nothing about the fix. The result goes into the job summary and a JSON file, and for `fix:` PRs
it decides the `fix-proof` check. Covers the Rust tests of the root workspace. See docs/fix-proof.md.

Subcommands:
  plan     which tests are new/changed, is it a fix, is there an exemption (cheap, no build)
  run      run the planned tests on the base and on the head (the current checkout)
  verdict  combine plan + results into fix-proof.json, a Markdown summary and the exit code
  check    all of it locally: `python3 -I tools/fix_proof.py check --base upstream/main`

Only the Python standard library is used.
"""
import argparse
import functools
import json
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
    tests = []
    for status, path, old in rows:
        if status == "D":
            continue
        if path.endswith(".rs"):
            crate = crate_of(path, root)
            if not crate:
                continue
            crate_dir, pkg = crate
            loc = rust_location(path, crate_dir)
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
    paths = [p for _, p, _ in rows] + [o for s, _, o in rows if s == "R"]
    return {
        "schema": SCHEMA,
        "base": base_sha,
        "head": head_sha,
        "title": title or "",
        "is_fix": is_fix(title, messages),
        "exempt": find_exemption(body, messages),
        "verifier_changed": any(p in VERIFIER_PATHS for p in paths),
        "changed": [[s, p, o] for s, p, o in rows],
        "tests": tests,
    }


# --------------------------------------------------------------------------- overlays
def apply_overlay(plan, root, wt):
    """Lay the PR's test code over the base worktree `wt`. Production code stays at base."""
    root = Path(root)
    htree, btree = Tree(root, plan["head"]), Tree(root, plan["base"])
    memo, written = {}, []
    for status, path, old in plan["changed"]:
        target = Path(wt) / path
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
# nothing about the code gives the base its own unit; dependencies stay shared.
BASE_CODEGEN_UNITS = 255
MAX_REVERT_ROUNDS = 4  # base builds after taking back test files that need the fix to compile


def cargo_cmd(tests, package, crate_has_lib, base=False):
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
    if base:
        for prof in ("dev", "test"):
            cfg += ["--config", f'profile.{prof}.package."{package}".codegen-units={BASE_CODEGEN_UNITS}']
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
    if plan["head"] and rev_parse(root, "HEAD") != plan["head"]:
        raise RuntimeError(f"the planned head {plan['head'][:10]} is not checked out")
    with BaseWorktree(root, plan["base"], keep) as wt:
        written = apply_overlay(plan, root, wt)
        log(f"fix-proof: base worktree {wt}, test overlay: {', '.join(written) or '–'}")
        overlay = {w.lstrip("~-") for w in written}
        reverted = set()
        for (pkg, crate_dir, target), ts in by_target.items():
            has_lib = (root / crate_dir / "src" / "lib.rs").exists()
            slug = re.sub(r"[^\w.-]+", "_", f"{pkg}-{target}")
            for side, cwd, store in (("head", root, head_res), ("base", wt, base_res)):
                cmd = cargo_cmd(ts, pkg, has_lib, base=side == "base")
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


# --------------------------------------------------------------------------- verdict
def combine(plan, results):
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
                  "sein Ergebnis ist kein unabhängiger Beleg.", ""]
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
    lines.append("<sub>Rot auf der Base ohne den Fix, grün mit ihm: docs/fix-proof.md</sub>")
    return "\n".join(lines) + "\n"


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


def cmd_run(a):
    plan = _load(a.plan)
    Path(a.logs).mkdir(parents=True, exist_ok=True)
    res = run_rust(plan, a.root, a.logs, timeout=a.timeout, keep=a.keep)
    _dump(res, a.out)
    for t in res["tests"]:
        print(f"  {LABELS[t['status']]:<45} {t['path']}")


def cmd_verdict(a):
    plan = _load(a.plan)
    results = [_load(p) for p in a.results if Path(p).exists()]
    s = combine(plan, results)
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
    base = git(root, "merge-base", a.base, "HEAD").strip()
    out = Path(a.out or tempfile.mkdtemp(prefix="fix-proof-"))
    out.mkdir(parents=True, exist_ok=True)
    title = a.title
    if title is None:
        ahead = git(root, "rev-list", "--count", f"{base}..HEAD").strip() != "0"
        title = git(root, "log", "-1", "--format=%s").strip() if ahead else ""
    plan = build_plan(root, base, None, title or "", "")
    if git(root, "status", "--porcelain"):
        print("fix-proof: uncommitted changes are included (head = working tree)", file=sys.stderr)
    _dump(plan, out / "plan.json")
    s = combine(plan, [run_rust(plan, root, out, timeout=a.timeout, keep=a.keep)])
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
    p.add_argument("--head", help="the checked-out commit (default: working tree)")
    p.add_argument("--title", default="")
    p.add_argument("--body-file")
    p.add_argument("--out", required=True)
    p.add_argument("--github-output", help="append the `rust` flag (GitHub Actions)")
    r = sub.add_parser("run")
    r.add_argument("--plan", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--logs", default="fix-proof-logs")
    r.add_argument("--timeout", type=int, default=1800, help="seconds per cargo invocation")
    r.add_argument("--keep", action="store_true", help="keep the base worktree for inspection")
    v = sub.add_parser("verdict")
    v.add_argument("--plan", required=True)
    v.add_argument("--results", nargs="*", default=[])
    v.add_argument("--json")
    v.add_argument("--summary", help="append the Markdown summary here (e.g. $GITHUB_STEP_SUMMARY)")
    v.add_argument("--enforce", action="store_true", help="exit 1 when a fix: PR is not proven")
    c = sub.add_parser("check", help="local run: plan + run + verdict, head = working tree")
    c.add_argument("--base", default="upstream/main")
    c.add_argument("--title", help="PR title (default: last commit subject)")
    c.add_argument("--out")
    c.add_argument("--timeout", type=int, default=1800)
    c.add_argument("--keep", action="store_true")
    a = ap.parse_args(argv)
    try:
        rc = {"plan": cmd_plan, "run": cmd_run, "verdict": cmd_verdict,
              "check": cmd_check}[a.cmd](a)
    except RuntimeError as err:
        sys.exit(f"fix-proof: {err}")
    return rc or 0


if __name__ == "__main__":
    sys.exit(main())
