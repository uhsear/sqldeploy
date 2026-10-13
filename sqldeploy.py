#!/usr/bin/env python
"""Preview a T-SQL deploy script one GO batch at a time, then run it only on --apply.

A deploy script is a list of batches separated by GO. GO is not T-SQL: the
SQL Server utilities strip it and send each batch on its own, and an ODBC
client that sends a GO line gets a syntax error. So a script runner has to
split on GO itself, and a runner that does so and then executes at once turns
one typo in batch 9 into eight batches already committed.

This tool checks by default. It splits the file, sends every batch to the
server under SET PARSEONLY ON (the server parses and does not compile or
execute), and prints one line per batch. Nothing is written. --apply parses
every batch first and runs none of them if any batch fails to parse.

It also refuses what it does not understand: an unknown argument, an
abbreviated flag (--app is not --apply), a GO line with trailing junk, a GO
inside a comment or string, a sqlcmd directive, and a script that touches
PARSEONLY itself.

    python sqldeploy.py --self-test
    python sqldeploy.py deploy.sql --split-only
    python sqldeploy.py deploy.sql --server HOST --database DB
    python sqldeploy.py deploy.sql --server HOST --database DB --apply

Windows authentication only (Trusted_Connection=yes). No password is read,
stored or printed. The driver is pyodbc, imported only when a connection is
needed, so --split-only and --self-test need nothing installed.
"""
import argparse
import collections
import contextlib
import io
import os
import re
import shutil
import sys
import tempfile

EXIT_OK = 0
EXIT_REFUSED = 1        # a problem in the script, or a batch that does not parse
EXIT_APPLY_FAILED = 2   # --apply stopped part way: earlier batches are committed
EXIT_CONNECT = 3        # could not connect, or could not enter parse-only mode
EXIT_USAGE = 64

DEFAULT_ODBC_DRIVER = "ODBC Driver 18 for SQL Server"
SUMMARY_WIDTH = 80
DETAIL_WIDTH = 300
COMMENTS_ONLY = "(comments only)"

Batch = collections.namedtuple(
    "Batch", "index first_line last_line text empty summary repeat")

_LINE_RE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+")
# GO as a word at the start of a line. GOTO, GO_1 and GO5 are not GO.
_GO_RE = re.compile(r"^\s*GO(?![\w@#$])(.*)$", re.IGNORECASE)
_GO_TAIL_RE = re.compile(r"^(?:\s+(\d+))?\s*(?:--.*)?$")
_PARSEONLY_RE = re.compile(r"\bPARSEONLY\b", re.IGNORECASE)
_DIRECTIVE_RE = re.compile(r"^\s*:[A-Za-z]", re.MULTILINE)
_UNSAFE_CHARS_RE = re.compile(r"[^\x20-\x7e]")
_BAD_CONN_VALUE_RE = re.compile(r"[;{}\x00-\x1f]")


# ------------------------------------------------------------ pure core

def ascii_safe(text):
    """Printable ASCII only, so output never breaks a console or injects escapes."""
    return _UNSAFE_CHARS_RE.sub("?", text)


def one_line(text, width):
    return ascii_safe(" ".join(str(text).split()))[:width]


def decode_sql(data):
    """Decode script bytes. SSMS saves UTF-16 with a BOM; most editors save UTF-8."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8")
    return data.decode("utf-8")


def _scan_line(line, depth, quote):
    """Walk one line (no terminator). Returns (visible, depth, quote).

    visible is the line with comments blanked and string and identifier text
    kept. Block comments nest, as the Microsoft block-comment page documents.
    """
    out = []
    i = 0
    n = len(line)
    while i < n:
        two = line[i:i + 2]
        c = line[i]
        if depth:
            if two == "/*":
                depth += 1
                i += 2
            elif two == "*/":
                depth -= 1
                i += 2
            else:
                i += 1
            out.append(" ")
        elif quote:
            out.append(c)
            if quote == "[":
                if c == "]":
                    if line[i + 1:i + 2] == "]":
                        out.append("]")
                        i += 1
                    else:
                        quote = None
            elif c == quote:
                quote = None
            i += 1
        elif two == "--":
            break
        elif two == "/*":
            depth = 1
            out.append(" ")
            i += 2
        else:
            if c in "'\"[":
                quote = c
            out.append(c)
            i += 1
    return "".join(out), depth, quote


def _code_only(visible):
    """Blank string and identifier text, so a word inside a literal never matches."""
    out = []
    quote = None
    for c in visible:
        if quote:
            out.append(" ")
            if c == ("]" if quote == "[" else quote):
                quote = None
        elif c in "'\"[":
            quote = c
            out.append(" ")
        else:
            out.append(c)
    return "".join(out)


def split_script(text):
    """Split a script on GO lines. Returns (batches, problems).

    problems is a list of (line_number, message). Any problem means the script
    must not run, because the split is not certain.
    """
    batches = []
    problems = []
    raw = []
    vis = []
    state = {"start": 1}
    depth = 0
    quote = None

    def flush(last_line, repeat):
        body = "".join(raw)
        visible_text = "\n".join(vis)
        del raw[:]
        del vis[:]
        first = state["start"]
        if body.strip():
            summary = COMMENTS_ONLY
            for one in visible_text.split("\n"):
                squeezed = one_line(one, SUMMARY_WIDTH)
                if squeezed:
                    summary = squeezed
                    break
            empty = summary == COMMENTS_ONLY
            index = len(batches) + 1
            batches.append(Batch(index, first, last_line, body, empty,
                                 summary, repeat))
            code = _code_only(visible_text)
            if _PARSEONLY_RE.search(code):
                problems.append((first, "batch %d mentions PARSEONLY; the "
                                 "setting takes effect at parse time, so a "
                                 "script must not change it" % index))
            if _DIRECTIVE_RE.search(code):
                problems.append((first, "batch %d has a sqlcmd directive "
                                 "(a line starting with a colon); this tool "
                                 "does not run sqlcmd scripts" % index))
        state["start"] = last_line + 2

    lines = _LINE_RE.findall(text)
    for number, line in enumerate(lines, 1):
        body = line.rstrip("\r\n")
        match = _GO_RE.match(body)
        if match and (depth or quote):
            problems.append((number, "a GO line sits inside an open comment "
                             "or string; the Microsoft GO page does not say "
                             "how the utilities treat it"))
        elif match:
            tail = _GO_TAIL_RE.match(match.group(1))
            repeat = 1
            if not tail:
                problems.append((number, "malformed GO line: only a count "
                                 "and a -- comment may follow GO"))
            elif tail.group(1):
                repeat = int(tail.group(1))
                if repeat < 1:
                    problems.append((number, "GO count must be a positive "
                                     "integer"))
                    repeat = 1
            flush(number - 1, repeat)
            continue
        raw.append(line)
        visible, depth, quote = _scan_line(body, depth, quote)
        vis.append(visible)
    if depth or quote:
        problems.append((len(lines), "the script ends inside an unclosed "
                         "comment, string or bracketed name"))
    flush(len(lines), 1)
    return batches, problems


def build_conn_string(server, database, driver, trust_server_certificate):
    """ODBC connection string, Windows authentication, encrypted.

    Encrypt=yes is written out because ODBC Driver 17 defaults to no.
    """
    for label, value in (("server", server), ("database", database),
                         ("odbc driver", driver)):
        if not value or _BAD_CONN_VALUE_RE.search(value):
            raise ValueError("%s is empty or holds a character that is not "
                             "allowed in a connection string value (a "
                             "semicolon, a brace or a control character)"
                             % label)
    parts = ["DRIVER={%s}" % driver, "SERVER=%s" % server,
             "DATABASE=%s" % database, "Trusted_Connection=yes",
             "Encrypt=yes"]
    if trust_server_certificate:
        parts.append("TrustServerCertificate=yes")
    return ";".join(parts) + ";"


def batch_label(batch):
    return "batch %d (lines %d-%d)" % (batch.index, batch.first_line,
                                       batch.last_line)


def format_split(batches):
    rows = []
    for b in batches:
        rows.append("  %s: %d chars%s: %s" % (
            batch_label(b), len(b.text),
            " x%d" % b.repeat if b.repeat > 1 else "", b.summary))
    return rows


def format_check(results):
    """results: list of (batch, status, detail). Returns output lines."""
    rows = []
    for b, status, detail in results:
        if status == "clean":
            rows.append("  %s: parses clean, %d chars%s: %s" % (
                batch_label(b), len(b.text),
                " x%d" % b.repeat if b.repeat > 1 else "", b.summary))
        elif status == "skipped":
            rows.append("  %s: skipped, nothing to run" % batch_label(b))
        else:
            rows.append("  %s: PARSE ERROR: %s" % (batch_label(b), b.summary))
            rows.append("      %s" % detail)
    return rows


# ------------------------------------------------------------ database I/O

def parse_check(cur, err, batches):
    """Send each batch under SET PARSEONLY ON. Returns (batch, status, detail).

    If SET PARSEONLY ON fails, the exception propagates before any batch is
    sent. PARSEONLY is switched off again even when a batch raises.
    """
    cur.execute("SET PARSEONLY ON")
    results = []
    try:
        for b in batches:
            if b.empty:
                results.append((b, "skipped", ""))
                continue
            try:
                cur.execute(b.text)
            except err as exc:
                results.append((b, "error", one_line(exc, DETAIL_WIDTH)))
            else:
                results.append((b, "clean", ""))
    finally:
        cur.execute("SET PARSEONLY OFF")
    return results


def run_batches(cur, err, batches):
    """Execute for real. Stops at the first failure.

    Returns (executed, failure). failure is None or (batch, run_number, detail).
    Each result set is drained with nextset(): pyodbc documents nextset() as the
    way to reach later result sets, and the loop reaches an error raised there.
    """
    executed = 0
    for b in batches:
        if b.empty:
            continue
        for run in range(1, b.repeat + 1):
            try:
                cur.execute(b.text)
                while cur.nextset():
                    pass
            except err as exc:
                return executed, (b, run, one_line(exc, DETAIL_WIDTH))
        executed += 1
    return executed, None


def _connect_pyodbc(conn_string):
    """Returns (connection, driver Error class). autocommit: no wrapping transaction."""
    try:
        import pyodbc
    except ImportError:
        raise RuntimeError("pyodbc is not installed (pip install pyodbc)")
    return pyodbc.connect(conn_string, autocommit=True), pyodbc.Error


# ------------------------------------------------------------ command line

class UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def _parser():
    ap = _Parser(
        prog="sqldeploy.py",
        allow_abbrev=False,
        description="Preview a T-SQL deploy script one GO batch at a time, "
                    "then run it only on --apply.",
        epilog="Nothing is written without --apply. Unknown arguments and "
               "abbreviated flags are refused.",
    )
    ap.add_argument("path", nargs="?", help="the .sql file")
    ap.add_argument("--server", help="SQL Server host or host\\instance")
    ap.add_argument("--database", help="database to run in")
    ap.add_argument("--odbc-driver", dest="odbc_driver",
                    default=DEFAULT_ODBC_DRIVER,
                    help="ODBC driver name (default %s)" % DEFAULT_ODBC_DRIVER)
    ap.add_argument("--trust-server-certificate", action="store_true",
                    dest="trust_server_certificate",
                    help="skip server certificate validation (default off)")
    ap.add_argument("--split-only", action="store_true", dest="split_only",
                    help="list the batches and exit; never connects")
    ap.add_argument("--apply", action="store_true",
                    help="parse every batch, then run them (default: preview)")
    ap.add_argument("--self-test", action="store_true", dest="self_test",
                    help="run the offline assertions and exit")
    return ap


def _validate(args):
    if args.self_test:
        return
    if not args.path:
        raise UsageError("a .sql file path is required (or use --self-test)")
    if args.split_only:
        if args.apply:
            raise UsageError("--split-only never connects, so it cannot "
                             "be combined with --apply")
        if args.server or args.database:
            raise UsageError("--split-only never connects; drop --server "
                             "and --database")
        return
    if not (args.server and args.database):
        raise UsageError("--server and --database are required unless "
                         "--split-only is given")
    try:
        args.conn_string = build_conn_string(
            args.server, args.database, args.odbc_driver,
            args.trust_server_certificate)
    except ValueError as exc:
        raise UsageError(str(exc))


def _fail(code, message):
    print("error: %s" % message, file=sys.stderr)
    return code


def main(argv=None, connect=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = _parser().parse_args(argv)
        _validate(args)
    except UsageError as exc:
        return _fail(EXIT_USAGE, exc)
    except SystemExit:          # --help
        return EXIT_OK
    if args.self_test:
        return self_test()

    try:
        with open(args.path, "rb") as handle:
            text = decode_sql(handle.read())
    except (OSError, ValueError) as exc:
        return _fail(EXIT_USAGE, "cannot read %s: %s" % (
            ascii_safe(args.path), one_line(exc, DETAIL_WIDTH)))

    batches, problems = split_script(text)
    runnable = [b for b in batches if not b.empty]
    print("%s: %d batch(es), %d with statements" % (
        ascii_safe(args.path), len(batches), len(runnable)))
    for number, message in problems:
        print("  line %d: REFUSED: %s" % (number, message))
    if problems:
        print("\nnothing was sent to any server.")
        return EXIT_REFUSED
    if not runnable:
        print("\nthe script has no statements. Nothing to do.")
        return EXIT_REFUSED

    if args.split_only:
        print("\n".join(format_split(batches)))
        print("\nSPLIT ONLY - no connection was made.")
        return EXIT_OK

    connect = connect or _connect_pyodbc
    results, problem, _ = _with_connection(
        connect, args.conn_string,
        lambda cur, err: parse_check(cur, err, batches))
    if problem:
        return _fail(EXIT_CONNECT, "no batch was run: %s" % problem)

    print("\n".join(format_check(results)))
    bad = len([r for r in results if r[1] == "error"])
    if bad:
        print("\n%d batch(es) failed to parse. Nothing was written." % bad)
        return EXIT_REFUSED
    if not args.apply:
        print("\nCHECK MODE - nothing was written. Re-run with --apply to "
              "deploy.")
        return EXIT_OK
    return _apply(args, connect, batches)


def _with_connection(connect, conn_string, work):
    """Open a connection, run work(cursor, Error), always close it.

    Returns (value, problem, opened). problem is None or one line of text. A
    driver raises its own classes, none of which is a base we can name before
    pyodbc is imported, so every Exception is caught here.
    """
    try:
        conn, err = connect(conn_string)
    except Exception as exc:
        return None, one_line(exc, DETAIL_WIDTH), False
    try:
        return work(conn.cursor(), err), None, True
    except Exception as exc:
        return None, one_line(exc, DETAIL_WIDTH), True
    finally:
        conn.close()


def _apply(args, connect, batches):
    value, problem, opened = _with_connection(
        connect, args.conn_string,
        lambda cur, err: run_batches(cur, err, batches))
    if problem and not opened:
        return _fail(EXIT_CONNECT, "no batch was run: %s" % problem)
    if problem:
        print("\nstopped by an unexpected error; some batches may already "
              "have run and are committed (autocommit): %s" % problem)
        return EXIT_APPLY_FAILED
    executed, failure = value
    if failure:
        b, run, detail = failure
        print("\n%s FAILED on run %d: %s\n      %s" % (
            batch_label(b), run, b.summary, detail))
        print("%d earlier batch(es) already ran and are committed "
              "(autocommit). Nothing rolls back." % executed)
        return EXIT_APPLY_FAILED
    print("\nDEPLOY OK - %d batch(es) executed." % executed)
    return EXIT_OK


# ------------------------------------------------------------ self-test

class _FakeError(Exception):
    pass


class _FakeCursor(object):
    """Stands in for a DB-API cursor.

    BADSYNTAX raises at parse and at run. RUNFAIL raises only when the batch
    really runs. MULTI gives one extra result set. NEXTFAIL raises from nextset.
    """

    def __init__(self, log, fail_on=False, fail_off=False):
        self.log = log
        self.parseonly = False
        self.fail_on = fail_on
        self.fail_off = fail_off
        self.pending = 0

    def execute(self, sql):
        if sql == "SET PARSEONLY ON":
            if self.fail_on:
                raise _FakeError("permission denied")
            self.parseonly = True
            self.log.append(("flag", "on"))
        elif sql == "SET PARSEONLY OFF":
            if self.fail_off:
                raise _FakeError("connection lost")
            self.parseonly = False
            self.log.append(("flag", "off"))
        else:
            kind = "parse" if self.parseonly else "exec"
            self.log.append((kind, sql))
            if "BADSYNTAX" in sql:
                raise _FakeError("Incorrect syntax near 'BADSYNTAX'.")
            if "RUNFAIL" in sql and not self.parseonly:
                raise _FakeError("run-time failure")
            if "BOOMPARSE" in sql:
                raise RuntimeError("not a driver error")
            if "BOOMRUN" in sql and not self.parseonly:
                raise RuntimeError("not a driver error")
            self.pending = 1 if "MULTI" in sql or "NEXTFAIL" in sql else 0
            self.next_sql = sql

    def nextset(self):
        if self.pending:
            self.pending = 0
            if "NEXTFAIL" in self.next_sql:
                raise _FakeError("error in a later statement")
            return True
        return False


class _FakeConn(object):
    def __init__(self, log, **kw):
        self.log = log
        self.kw = kw
        self.closed = False

    def cursor(self):
        return _FakeCursor(self.log, **self.kw)

    def close(self):
        self.closed = True


def _fake_connect(log, conns, fail_connect=False, **kw):
    def connect(conn_string):
        conns.append(conn_string)
        if fail_connect:
            raise _FakeError("server not found")
        conn = _FakeConn(log, **kw)
        conns.append(conn)
        return conn, _FakeError
    return connect


class _Checker(object):
    """The assertion harness. self_test() also tests it, so it can report red."""

    def __init__(self):
        self.passed = 0
        self.failed = []

    def check(self, cond, label):
        if cond:
            self.passed += 1
            print("PASS  %s" % label)
        else:
            self.failed.append(label)
            print("FAIL  %s" % label)

    def raises(self, fn, label):
        try:
            fn()
        except ValueError:
            self.check(True, label)
        except Exception as exc:
            self.check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            self.check(False, "%s (no error raised)" % label)

    def report(self):
        print("-" * 68)
        total = self.passed + len(self.failed)
        if self.failed:
            print("%d assertions, %d failed" % (total, len(self.failed)))
            for item in self.failed:
                print("  FAILED: %s" % item)
            return 1
        print("%d assertions, 0 failed" % total)
        return 0


def self_test():
    """Assertions over the split, the parse-first rule and the argument refusal.

    No SQL Server, no ODBC, no network. A fake cursor stands in for the driver.
    """
    harness = _Checker()
    check = harness.check
    raises = harness.raises

    def run(argv, connect=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv, connect)
        return code, out.getvalue(), err.getvalue()

    def kinds(log, kind):
        return [item for item in log if item[0] == kind]

    print("sqldeploy self-test: no SQL Server, no ODBC, no network")
    print("-" * 68)

    # ---- the splitter
    b, p = split_script("SELECT 1\nGO\nSELECT 2\nGO\n")
    check([x.text for x in b] == ["SELECT 1\n", "SELECT 2\n"] and not p,
          "two GO batches split into two")
    check([(x.first_line, x.last_line) for x in b] == [(1, 1), (3, 3)],
          "each batch records its own line range")
    b, p = split_script("SELECT 1\ngo\nSELECT 2")
    check(len(b) == 2 and not p, "lowercase go splits and a final batch needs no GO")
    b, p = split_script("SELECT 1\n  GO  \nSELECT 2\n")
    check(len(b) == 2 and not p, "indented GO with trailing spaces splits")
    b, p = split_script("SELECT 1\nGO -- done\nSELECT 2\n")
    check(len(b) == 2 and not p, "GO with a trailing -- comment splits")
    b, p = split_script("SELECT 1\nGO 3\nSELECT 2\n")
    check(b[0].repeat == 3 and b[1].repeat == 1 and not p,
          "GO 3 repeats the batch before it three times")
    b, p = split_script("SELECT 1\nGO 2 -- twice\n")
    check(b[0].repeat == 2 and not p, "GO count followed by a comment is read")
    b, p = split_script("GO\nGO\n\nGO\nSELECT 1\n")
    check(len(b) == 1 and b[0].first_line == 5, "empty batches between GO lines are dropped")
    b, p = split_script("")
    check(b == [] and not p, "an empty script has no batches")
    b, p = split_script("GOTO done\nSELECT 1\n")
    check(len(b) == 1 and not p, "GOTO at the start of a line is not GO")
    b, p = split_script("SELECT 1 AS GO_1\nGO5\n")
    check(len(b) == 1 and not p, "GO_1 and GO5 are not GO")
    b, p = split_script("SELECT 1 GO\nSELECT 2\n")
    check(len(b) == 1 and not p, "GO after a statement on the same line is not a separator")
    b, p = split_script("SELECT 1\r\nGO\r\nSELECT 2\r\n")
    check([x.text for x in b] == ["SELECT 1\r\n", "SELECT 2\r\n"],
          "CRLF line endings split and are kept in the batch text")
    b, p = split_script("SELECT 'a\r\nb'\r\nGO\r\n")
    check(b[0].text == "SELECT 'a\r\nb'\r\n",
          "a CRLF inside a string literal is sent unchanged  <-- pinned defect")
    b, p = split_script("SELECT 'a\x0cb'\nGO\nSELECT 2\n")
    check(len(b) == 2 and b[0].last_line == 1,
          "a form feed is not a line break  <-- pinned defect")
    b, p = split_script("SELECT 1\rGO\rSELECT 2\r")
    check(len(b) == 2, "a bare CR counts as a line break")

    # ---- GO that is not a separator, or is not certain
    b, p = split_script("/*\nGO\n*/\nSELECT 1\nGO\n")
    check(len(p) == 1 and p[0][0] == 2,
          "a GO line inside a block comment is REFUSED  <-- pinned defect")
    b, p = split_script("SELECT '\nGO\n'\nGO\n")
    check(len(p) == 1 and p[0][0] == 2,
          "a GO line inside a string literal is REFUSED  <-- pinned defect")
    b, p = split_script("SELECT [a\nGO\nb]\nGO\n")
    check(len(p) == 1, "a GO line inside a bracketed name is REFUSED")
    b, p = split_script("-- GO\nSELECT 1\nGO\n")
    check(len(b) == 1 and not p, "GO inside a line comment is not seen as GO")
    b, p = split_script("SELECT 1 -- it's /* here\nGO\nSELECT 2\n")
    check(len(b) == 2 and not p, "a quote or /* inside a line comment opens nothing")
    b, p = split_script("SELECT 'it''s'\nGO\nSELECT 2\n")
    check(len(b) == 2 and not p, "a doubled quote stays inside the string")
    b, p = split_script("SELECT [a]]b]\nGO\nSELECT 2\n")
    check(len(b) == 2 and not p, "a doubled ] stays inside the bracketed name")
    b, p = split_script("SELECT [a]\nGO\nSELECT 2\n")
    check(len(b) == 2 and not p, "a closed bracketed name ends cleanly")
    b, p = split_script('SELECT "a b"\nGO\nSELECT 2\n')
    check(len(b) == 2 and not p, "a double-quoted name is read as a name")

    # ---- nested comments: the Microsoft block-comment page documents nesting
    b, p = split_script("/* a /* b */ c */\nSELECT 1\nGO\n")
    check(len(b) == 1 and not p, "a nested block comment closes only at the outer */")
    b, p = split_script("/*\nSELECT @c = '/*';\n*/\nSELECT 1\nGO\n")
    check(p and "unclosed" in p[-1][1],
          "the documented unclosed nested comment example is REFUSED  <-- pinned defect")
    b, p = split_script("/*\nSELECT @c = '/*';\n*/ */\nSELECT 1\nGO\n")
    check(len(b) == 1 and not p, "the documented workaround script splits cleanly")
    b, p = split_script("/* open\nSELECT 1\n")
    check(len(p) == 1, "a comment left open at the end is REFUSED")
    b, p = split_script("SELECT 'open\nGO\n")
    check(len(p) >= 1, "a string left open at the end is REFUSED")
    b, p = split_script("/* a */ SELECT 1 /* b */\nGO\n")
    check(len(b) == 1 and b[0].summary == "SELECT 1" and not p,
          "inline block comments are blanked in the summary")

    # ---- malformed GO
    for text, label in (("SELECT 1\nGO;\n", "GO;"),
                        ("SELECT 1\nGO foo\n", "GO followed by a word"),
                        ("SELECT 1\nGO /* x */\n", "GO followed by a block comment"),
                        ("SELECT 1\nGO 2 3\n", "GO with two counts")):
        b, p = split_script(text)
        check(len(p) == 1 and "malformed" in p[0][1],
              "%s is REFUSED as a malformed GO" % label)
    b, p = split_script("SELECT 1\nGO 0\n")
    check(len(p) == 1 and "positive" in p[0][1], "GO 0 is REFUSED: the count must be positive")

    # ---- things this tool will not run
    b, p = split_script("SET PARSEONLY OFF\nGO\nDROP TABLE t\nGO\n")
    check(len(p) == 1 and "PARSEONLY" in p[0][1],
          "a script that sets PARSEONLY is REFUSED  <-- pinned defect")
    b, p = split_script("-- SET PARSEONLY OFF\nSELECT 'parseonly'\nGO\n")
    check(not p, "PARSEONLY in a comment or a string is not a problem")
    b, p = split_script(":setvar x 1\nSELECT 1\nGO\n")
    check(len(p) == 1 and "sqlcmd" in p[0][1], "a sqlcmd directive is REFUSED")
    b, p = split_script("SELECT 1\n::fn_x()\nGO\n")
    check(not p, "a :: function call is not a sqlcmd directive")

    # ---- summaries
    b, p = split_script("/* header\nDROP TABLE victim\n*/\n-- note\nCREATE TABLE t (a int)\nGO\n")
    check(b[0].summary == "CREATE TABLE t (a int)",
          "the summary skips a block-comment header whose prose has no marker")
    b, p = split_script("-- only a note\nGO\nSELECT 1\nGO\n")
    check(b[0].empty and b[0].summary == COMMENTS_ONLY and not b[1].empty,
          "a comment-only batch is marked empty")
    b, p = split_script("SELECT '" + "x" * 200 + "'\nGO\n")
    check(len(b[0].summary) == SUMMARY_WIDTH, "a long first statement is cut to the summary width")
    b, p = split_script("SELECT 'caf\u00e9\x1b[31m'\nGO\n")
    check(all(32 <= ord(ch) < 127 for ch in b[0].summary),
          "a non-ASCII or escape character in a summary becomes ?  <-- pinned defect")

    # ---- decoding
    check(decode_sql(b"SELECT 1") == "SELECT 1", "plain UTF-8 decodes")
    check(decode_sql(b"\xef\xbb\xbfSELECT 1") == "SELECT 1", "a UTF-8 BOM is dropped")
    check(decode_sql("SELECT 1".encode("utf-16")) == "SELECT 1", "UTF-16 LE with a BOM decodes")
    check(decode_sql(b"\xfe\xff" + "SELECT 1".encode("utf-16-be")) == "SELECT 1",
          "UTF-16 BE with a BOM decodes")
    raises(lambda: decode_sql(b"\xff\xfe\x00"), "a truncated UTF-16 file raises")
    raises(lambda: decode_sql(b"SELECT \x80"), "bytes that are not UTF-8 raise")

    # ---- connection string
    s = build_conn_string("HOST\\INST,1433", "db1", DEFAULT_ODBC_DRIVER, False)
    check(s == "DRIVER={ODBC Driver 18 for SQL Server};SERVER=HOST\\INST,1433;"
               "DATABASE=db1;Trusted_Connection=yes;Encrypt=yes;",
          "the connection string is Windows auth with Encrypt=yes spelled out")
    check("TrustServerCertificate" not in s, "certificate trust is off by default")
    check(build_conn_string("h", "d", "x", True).endswith("TrustServerCertificate=yes;"),
          "--trust-server-certificate adds the keyword")
    check("PWD" not in s.upper() and "UID" not in s.upper(),
          "the connection string carries no user or password keyword")
    raises(lambda: build_conn_string("h;Trusted_Connection=no", "d", "x", False),
           "a semicolon in the server name is REFUSED  <-- pinned defect")
    raises(lambda: build_conn_string("h", "d}", "x", False), "a brace in a value is REFUSED")
    raises(lambda: build_conn_string("h", "d\nx", "x", False), "a newline in a value is REFUSED")
    raises(lambda: build_conn_string("", "d", "x", False), "an empty server is REFUSED")

    # ---- parse_check and run_batches against the fake cursor
    script = "CREATE TABLE a (x int)\nGO\nINSERT a VALUES (1)\nGO 2\n-- note\nGO\n"
    batches, problems = split_script(script)
    log = []
    res = parse_check(_FakeCursor(log), _FakeError, batches)
    check(len(res) == 3, "parse_check returns one result per batch")
    check([r[1] for r in res] == ["clean", "clean", "skipped"],
          "a comment-only batch is skipped, the others parse clean")
    check(log[0] == ("flag", "on") and log[-1] == ("flag", "off"),
          "PARSEONLY goes on before the first batch and off after the last")
    check(len(kinds(log, "parse")) == 2 and not kinds(log, "exec"),
          "the check sent each batch once, as parse only, and executed none")
    check("\n".join(format_check(res)).count("parses clean") == 2,
          "the check report has a clean line per statement batch")
    check("x2" in "\n".join(format_check(res)), "a GO count shows as x2 in the report")
    check("x2" in "\n".join(format_split(batches)), "a GO count shows as x2 in the split listing")

    bad, _ = split_script("SELECT 1\nGO\nBADSYNTAX here\nGO\nSELECT 3\nGO\n")
    log = []
    res = parse_check(_FakeCursor(log), _FakeError, bad)
    check([r[1] for r in res] == ["clean", "error", "clean"],
          "one bad batch is reported and the rest are still checked")
    check("Incorrect syntax" in res[1][2], "the server message is kept")
    check(log[-1] == ("flag", "off"), "PARSEONLY is switched off after a failing batch")
    text = "\n".join(format_check(res))
    check("batch 2 (lines 3-3): PARSE ERROR: BADSYNTAX here" in text,
          "the report names the failing batch, its lines and its first statement")

    log = []
    raises_in = []
    try:
        parse_check(_FakeCursor(log), _FakeError,
                    split_script("SELECT 1\nGO\nBOOMPARSE\nGO\n")[0])
    except RuntimeError:
        raises_in.append(1)
    check(raises_in and log[-1] == ("flag", "off"),
          "PARSEONLY is switched off even when a batch raises a non-driver error")

    log = []
    raises_in = []
    try:
        parse_check(_FakeCursor(log, fail_on=True), _FakeError, batches)
    except _FakeError:
        raises_in.append(1)
    check(raises_in and not kinds(log, "parse"),
          "if SET PARSEONLY ON fails no batch is sent  <-- pinned defect")
    raises_in = []
    try:
        parse_check(_FakeCursor([], fail_off=True), _FakeError, batches)
    except _FakeError:
        raises_in.append(1)
    check(raises_in, "if SET PARSEONLY OFF fails the error is not swallowed")

    log = []
    n, fail = run_batches(_FakeCursor(log), _FakeError, batches)
    check(n == 2 and fail is None, "run_batches executes every statement batch")
    check(len(kinds(log, "exec")) == 3, "GO 2 executes the batch twice")
    log = []
    n, fail = run_batches(_FakeCursor(log), _FakeError,
                          split_script("SELECT 1\nGO\nRUNFAIL\nGO 3\nSELECT 3\nGO\n")[0])
    check(n == 1 and fail[0].index == 2 and fail[1] == 1,
          "run_batches stops at the first run-time failure")
    check(len(kinds(log, "exec")) == 2, "nothing after the failing batch ran")
    n, fail = run_batches(_FakeCursor([]), _FakeError,
                          split_script("SELECT 1\nGO\nNEXTFAIL\nGO\n")[0])
    check(fail is not None and fail[0].index == 2,
          "an error raised from a later result set is caught  <-- pinned defect")
    n, fail = run_batches(_FakeCursor([]), _FakeError,
                          split_script("MULTI\nGO\n")[0])
    check(n == 1 and fail is None, "a batch with extra result sets drains them")

    # ---- end to end through main(), real files in a temp directory
    tmp = tempfile.mkdtemp(prefix="sqldeploy-test-")
    try:
        def write(name, data):
            path = os.path.join(tmp, name)
            with open(path, "wb") as handle:
                handle.write(data)
            return path

        good = write("good.sql", b"\xef\xbb\xbfCREATE TABLE a (x int)\nGO\nINSERT a VALUES (1)\nGO\n")
        broken = write("broken.sql", b"CREATE TABLE a (x int)\nGO\nBADSYNTAX\nGO\nDROP TABLE a\nGO\n")
        runfail = write("runfail.sql", b"SELECT 1\nGO\nRUNFAIL\nGO\nSELECT 3\nGO\n")
        junk = write("junk.sql", b"SELECT 1\nGO;\n")
        blank = write("blank.sql", b"-- nothing here\nGO\n")
        binary = write("binary.sql", b"SELECT \x80\n")
        server = ["--server", "HOST", "--database", "db1"]

        # ---- the argument parser
        ok = _parser().parse_args([good, "--split-only"])
        check(ok.path == good and ok.split_only and not ok.apply,
              "a path and --split-only parse")
        refused = []
        for argv in ([good, "--split-only", "--dry-run"],
                     [good, "--split-only", "extra.sql"],
                     [good, "--split-only", "--apply=1"],
                     [good, "--split"],
                     [good, "--serv", "h", "--database", "d"]):
            code, out, err = run(argv, _fake_connect([], []))
            refused.append((code, out, err.startswith("error:") and "cannot read" not in err))
        check(all(c == EXIT_USAGE and o == "" and e for c, o, e in refused),
              "an unknown flag, a second path, --apply=1 and abbreviated flags are REFUSED")
        log, conns = [], []
        for flag in ("--app", "--appl", "--ap", "--a"):
            code, out, err = run([good, "--server", "h", "--database", "d", flag],
                                 _fake_connect(log, conns))
            check(code == EXIT_USAGE and not conns and not log and "cannot read" not in err,
                  "a prefix %s of --apply is REFUSED before any connection  <-- pinned defect" % flag)
        code, out, err = run([good, "--server", "h", "--database", "d", "--dry-run"],
                             _fake_connect(log, conns))
        check(code == EXIT_USAGE and not conns and "cannot read" not in err,
              "--dry-run is REFUSED, not ignored  <-- pinned defect")
        check(_parser().allow_abbrev is False, "the parser sets allow_abbrev=False")
        sel_refused = False
        try:
            _parser().parse_args(["--sel"])
        except UsageError:
            sel_refused = True
        check(sel_refused, "--sel is not accepted as --self-test  <-- pinned defect")

        # ---- usage rules in main()
        code, out, err = run([])
        check(code == EXIT_USAGE and "path" in err, "no path and no --self-test is a usage error")
        code, out, err = run([good])
        check(code == EXIT_USAGE and "--server" in err and "cannot read" not in err,
              "no --server and no --split-only is a usage error")
        code, out, err = run([good, "--server", "h"])
        check(code == EXIT_USAGE and "--database" in err and "cannot read" not in err,
              "--server without --database is a usage error")
        code, out, err = run([good, "--split-only", "--apply"])
        check(code == EXIT_USAGE and "--apply" in err and "cannot read" not in err,
              "--split-only with --apply is a usage error")
        code, out, err = run([good, "--split-only", "--server", "h"])
        check(code == EXIT_USAGE and "--server" in err and "cannot read" not in err,
              "--split-only with --server is a usage error")
        code, out, err = run([good, "--server", "h;x", "--database", "d"])
        check(code == EXIT_USAGE and "semicolon" in err and "cannot read" not in err,
              "a bad connection value is a usage error")
        code, out, err = run(["-h"])
        check(code == EXIT_OK and "--apply" in out, "--help prints the flags and exits 0")

        code, out, err = run([good, "--split-only"])
        check(code == EXIT_OK and "2 batch(es)" in out and "SPLIT ONLY" in out,
              "--split-only lists the batches and exits 0")

        log, conns = [], []
        code, out, err = run([good] + server, _fake_connect(log, conns))
        check(code == EXIT_OK and "CHECK MODE" in out and "nothing was written" in out,
              "the default run is a check and says nothing was written")
        check(not kinds(log, "exec"), "the default run executes no batch  <-- pinned defect")
        check(conns[1].closed, "the connection is closed after a check")
        check(conns[0].startswith("DRIVER={ODBC Driver 18 for SQL Server};SERVER=HOST;"),
              "main() connects with the built connection string")

        log, conns = [], []
        code, out, err = run([broken] + server, _fake_connect(log, conns))
        check(code == EXIT_REFUSED and "1 batch(es) failed to parse" in out,
              "a check with a bad batch exits 1")
        log, conns = [], []
        code, out, err = run([broken] + server + ["--apply"], _fake_connect(log, conns))
        check(code == EXIT_REFUSED and not kinds(log, "exec"),
              "--apply with one unparseable batch runs NOTHING  <-- pinned defect")

        log, conns = [], []
        code, out, err = run([good] + server + ["--apply"], _fake_connect(log, conns))
        check(code == EXIT_OK and "DEPLOY OK - 2 batch(es)" in out and len(kinds(log, "exec")) == 2,
              "--apply on a clean script runs every batch")
        flags = [i[1] for i in log if i[0] == "flag"]
        first_exec = [i for i, item in enumerate(log) if item[0] == "exec"][0]
        check(flags == ["on", "off"] and log.index(("flag", "off")) < first_exec,
              "--apply parses everything and turns PARSEONLY off before the first real run")
        check(conns[1].closed, "the connection is closed after --apply")

        log, conns = [], []
        code, out, err = run([runfail] + server + ["--apply"], _fake_connect(log, conns))
        check(code == EXIT_APPLY_FAILED and "already ran and are committed" in out,
              "a run-time failure under --apply exits 2 and says what is committed")
        check(len(kinds(log, "exec")) == 2, "--apply stops at the failing batch")

        code, out, err = run([junk] + server, _fake_connect([], conns))
        check(code == EXIT_REFUSED and "REFUSED" in out and "nothing was sent" in out,
              "a script with a split problem exits 1 before connecting")
        code, out, err = run([blank, "--split-only"])
        check(code == EXIT_REFUSED and "no statements" in out, "a script with no statements exits 1")
        code, out, err = run([binary, "--split-only"])
        check(code == EXIT_USAGE and "cannot read" in err, "a file that is not text exits 64")
        code, out, err = run([os.path.join(tmp, "missing.sql"), "--split-only"])
        check(code == EXIT_USAGE and "cannot read" in err, "a missing file exits 64")

        code, out, err = run([good] + server, _fake_connect([], [], fail_connect=True))
        check(code == EXIT_CONNECT and "no batch was run" in err,
              "a failed connection exits 3")
        log, conns = [], []
        code, out, err = run([good] + server + ["--apply"],
                             _fake_connect(log, conns, fail_on=True))
        check(code == EXIT_CONNECT and not log[1:] and not kinds(log, "exec"),
              "if parse-only mode cannot start, --apply exits 3 and runs nothing  <-- pinned defect")
        check(conns[1].closed, "the connection is closed after a parse-only failure")
        log, conns = [], []
        code, out, err = run([good] + server + ["--apply"],
                             _fake_connect(log, conns, fail_off=True))
        check(code == EXIT_CONNECT and not kinds(log, "exec"),
              "if PARSEONLY cannot be switched off, --apply runs nothing")

        class _DeadOnApply(object):
            """Parse check passes, then the second connection is refused."""
            def __init__(self):
                self.calls = 0

            def __call__(self, conn_string):
                self.calls += 1
                if self.calls == 2:
                    raise _FakeError("server went away")
                return _FakeConn([]), _FakeError

        code, out, err = run([good] + server + ["--apply"], _DeadOnApply())
        check(code == EXIT_CONNECT and "no batch was run" in err,
              "a connection lost between the check and the run exits 3")

        # the real default connector, with a fake pyodbc module
        class _FakePyodbc(object):
            Error = _FakeError

            def __init__(self):
                self.seen = []

            def connect(self, conn_string, autocommit=False):
                self.seen.append((conn_string, autocommit))
                return _FakeConn([])

        fake = _FakePyodbc()
        saved = sys.modules.pop("pyodbc", None)
        try:
            sys.modules["pyodbc"] = fake
            conn, err_cls = _connect_pyodbc("DRIVER={x};")
            check(fake.seen == [("DRIVER={x};", True)] and err_cls is _FakeError,
                  "the default connector passes autocommit=True and returns the driver Error class")
            sys.modules["pyodbc"] = None
            code, out, err = run([good] + server)
            check(code == EXIT_CONNECT and "pyodbc is not installed" in err,
                  "a missing pyodbc exits 3 with an install hint")
        finally:
            sys.modules.pop("pyodbc", None)
            sys.modules.update({"pyodbc": saved} if saved else {})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- an error that is not a driver error
    log, conns = [], []
    tmp2 = tempfile.mkdtemp(prefix="sqldeploy-test-")
    try:
        boom_path = os.path.join(tmp2, "boom.sql")
        with open(boom_path, "wb") as handle:
            handle.write(b"SELECT 1\nGO\nBOOMRUN\nGO\nSELECT 3\nGO\n")
        code, out, err = run([boom_path, "--server", "h", "--database", "d",
                              "--apply"], _fake_connect(log, conns))
        check(code == EXIT_APPLY_FAILED and "unexpected error" in out
              and "may already have run" in out and conns[1].closed,
              "an unexpected error during --apply exits 2 and does not claim nothing ran")
        check(len(kinds(log, "exec")) == 2, "--apply stopped at the unexpected error")
        with open(boom_path, "wb") as handle:
            handle.write(b"SELECT 1\nGO\nBOOMPARSE\nGO\n")
        code, out, err = run([boom_path, "--server", "h", "--database", "d"],
                             _fake_connect([], []))
        check(code == EXIT_CONNECT and "no batch was run" in err,
              "an unexpected error during a check exits 3")
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)

    # ---- the harness itself can go red
    meta = _Checker()
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink):
        meta.check(True, "a")
        meta.check(False, "b")
        meta.raises(lambda: int("x"), "c")
        meta.raises(lambda: {}["k"], "d")
        meta.raises(lambda: None, "e")
        code = meta.report()
    check(code == 1 and meta.passed == 2 and len(meta.failed) == 3,
          "the harness counts a failing check, a wrong exception and a missing one")
    check("3 failed" in sink.getvalue() and "FAILED: e" in sink.getvalue(),
          "the harness report names every failed label")

    # ---- nothing else may write, and no secret path exists
    with io.open(os.path.abspath(__file__), "rb") as handle:
        source_bytes = handle.read()
    source = source_bytes.decode("ascii")
    check(all(byte < 128 for byte in source_bytes), "the source is pure ASCII")
    check(("envi" + "ron") not in source and ("get" + "env") not in source,
          "no process setting is read, so none can switch on --apply")
    import ast
    tree = ast.parse(source)
    check(not [n for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)],
          "the source has no f-strings")

    return harness.report()


if __name__ == "__main__":  # pragma: no branch
    sys.exit(main())
