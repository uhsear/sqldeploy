# sqldeploy

Preview a T-SQL deploy script one GO batch at a time. Run it only on `--apply`, and only after every batch has parsed.

A small Python runner reads a `.sql` file, splits it on `GO` and executes each batch. It works for a year.
Then someone saves a script with a typo in batch 9. The runner has no preview, so it executes batches 1
to 8 under autocommit, hits the typo and stops. The table exists. The view that needs it does not.
Run the script again and batch 1 fails with "already exists". Nobody can tell which half of the
deploy is live.

The same runner has a second weakness. It reads its arguments by position and ignores the rest. Someone
types `deploy.sql --dry-run`, expecting a rehearsal. The runner never saw the flag. It deploys.

Both failures come from one design: the runner trusts the file and trusts the command line. This tool
trusts neither.

- The default run is a preview. It sends every batch to the server under `SET PARSEONLY ON` and prints
  one line per batch. Nothing is written.
- `--apply` parses every batch first. If any batch fails to parse, it runs none of them.
- An unknown argument is an error. So is an abbreviated flag: `--app` is not `--apply`.
- A `GO` it cannot split with certainty is an error, not a guess. That includes a `GO` that follows code or a
  comment on the same line.
- A script that sets `NOEXEC`, `FMTONLY`, `SHOWPLAN_*` or `IMPLICIT_TRANSACTIONS` is an error. Those
  settings make later batches stop running while the server still says OK.
- The text of a string literal, and a hex literal of four bytes or more such as a `HASHED` password
  value, is masked in every batch summary and in the server's error text for that batch, so a password
  in your script does not reach a log. This is a best effort, not a guarantee: Limits lists what is not
  masked, and a reader of this bullet alone should not take it as complete.

## What exists already

- **sqlcmd and SQL Server Management Studio** run scripts and handle `GO` themselves. They are the
  reference behaviour. Microsoft documents `GO` as a command of those utilities, not of T-SQL.
  They run a script batch by batch.
  [GO](https://learn.microsoft.com/en-us/sql/t-sql/language-elements/sql-server-utilities-statements-go)
  The `sqlcmd` utility page lists no option that only parses a script. I searched that page for the
  word "parse" and found nothing.
  [sqlcmd](https://learn.microsoft.com/en-us/sql/tools/sqlcmd/sqlcmd-utility)
  SSMS has a **Parse** button (Ctrl+F5). The page says it "checks the syntax of the selected code" or
  of all the code in the editor window. It is a manual step in a window. This tool is a command you
  can run from a script, and it checks every batch one at a time.
  [SSMS Query Editor](https://learn.microsoft.com/en-us/ssms/f1-help/database-engine-query-editor-sql-server-management-studio)
- **DbUp** is a .NET library that "tracks which SQL scripts have been run already" and applies the
  ones that are still needed. This tool keeps no history. Use DbUp when you need that.
  [DbUp](https://github.com/DbUp/DbUp)
- **sqlpackage** publishes a dacpac and can write the deployment script and a deploy report to a
  file (`/DeployScriptPath`, `/DeployReportPath`). It deploys a schema model. This tool runs a
  hand-written script.
  [SqlPackage Publish](https://learn.microsoft.com/en-us/sql/tools/sqlpackage/sqlpackage-publish)
- **Flyway** is another migration tool. I did not test it, so I make no claim about it.
- **Other repositories named sqldeploy.** A `gh search repos sqldeploy` on 2026-10-13 found at least
  ten on GitHub, including a Python ODBC runner and a .NET sqlproj tool. I read only their one-line
  descriptions. This tool shares the name and nothing else.

This tool is thin on purpose. It fills one gap: a hand-written script, a runner you control, and a
preview that happens before anything runs.

## Self-test

```
$ python sqldeploy.py --self-test
sqldeploy self-test: no SQL Server, no ODBC, no network
--------------------------------------------------------------------
PASS  two GO batches split into two
PASS  each batch records its own line range
...
PASS  a GO line inside a block comment is REFUSED  <-- pinned defect
PASS  GO after a statement on the same line is REFUSED, not run as text  <-- pinned defect
PASS  GO followed by a semicolon after code on the same line is REFUSED  <-- pinned defect
...
PASS  a prefix --app of --apply is REFUSED before any connection  <-- pinned defect
...
PASS  a password literal is masked in the batch summary  <-- pinned defect
PASS  a long hex literal is masked in the batch summary, a short one stays  <-- pinned defect
PASS  a literal echoed with its apostrophe undoubled is masked whole  <-- pinned defect
PASS  a padded literal echoed with its apostrophe undoubled is masked whole  <-- pinned defect
PASS  a driver error (sqlstate, message) is masked from the message, not its tuple repr  <-- pinned defect
...
PASS  an unknown flag is named but its value is not echoed  <-- pinned defect
...
PASS  --apply with one unparseable batch runs NOTHING  <-- pinned defect
...
--------------------------------------------------------------------
274 assertions, 0 failed
```

The count is the same on Windows (Python 3.13.2), Linux (Python 3.12.3) and Linux (Python 3.9.25).
The output is identical on Windows and Linux once the carriage returns are removed.
No assertion depends on the platform. One assertion writes a file whose name holds an accented letter
to the temp directory, so it needs a file system that stores such names.

## Requirements

Python 3.9 or later and the standard library. `--self-test` and `--split-only` need nothing else.
A preview or an `--apply` needs `pyodbc` and a Microsoft ODBC driver for SQL Server:

```
pip install pyodbc
git clone https://github.com/uhsear/sqldeploy.git
```

## Usage

Look at the split first. This never connects.

```
python sqldeploy.py deploy.sql --split-only
```

Then preview against the server. This is the default and writes nothing.

```
python sqldeploy.py deploy.sql --server HOST --database DB
```

Then deploy. Only `--apply` runs anything.

```
python sqldeploy.py deploy.sql --server HOST --database DB --apply
```

| Flag | Default | What it does |
|---|---|---|
| `path` | none | The `.sql` file. Required. UTF-8, UTF-8 with BOM, or UTF-16 with BOM. |
| `--server` | none | Host or `host\instance`. Required unless `--split-only`. |
| `--database` | none | Database to run in. Required unless `--split-only`. |
| `--odbc-driver` | `ODBC Driver 18 for SQL Server` | Name of the installed ODBC driver. `--split-only` accepts it and ignores it. |
| `--trust-server-certificate` | off | Skip server certificate validation. `--split-only` accepts it and ignores it. |
| `--split-only` | off | List the batches and exit. Cannot combine with `--apply`, `--server` or `--database`. |
| `--apply` | off | Parse every batch, then run them. Without it nothing is written. |
| `--self-test` | off | Run the offline assertions and exit. It ignores a path and the valid flags next to it. An unknown flag, or a flag that lacks its value, is still refused first with exit 64. |

Exit codes: 0 clean, 1 refused (a script problem or a batch that does not parse), 2 `--apply` stopped
part way, 3 could not connect or could not enter parse-only mode, 64 usage error or unreadable file.

The tool's own code reads no environment variable, so none can switch on `--apply`. Other code
reads some: `argparse` reads `COLUMNS` to wrap `--help`, and `pyodbc` and the ODBC driver read their
own settings. The tool uses Windows authentication only (`Trusted_Connection=yes`). It has no password
option and never reads or stores a password. It prints no string literal from your script in a batch
summary or a batch error. This is a best effort, not a guarantee: Limits lists what it does not
catch. A literal is shown as `'***'`, and so is quoted text in a server error. A bad
flag is named in the error, but its value is not echoed, because it could be a password.

## How the split works

A real preview from the sample file in this section:

```
-- Synthetic demo
CREATE TABLE dbo.parcel_status (code int NOT NULL, label varchar(20) NOT NULL)
GO
INSERT dbo.parcel_status (code, label) VALUES (1, 'active'), (2, 'retired')
GO 2 -- seeded twice on purpose
SELECT COUNT(*) FROM dbo.parcel_status
GO
```

```
$ python sqldeploy.py demo_ok.sql --split-only
demo_ok.sql: 3 batch(es), 3 with statements
  batch 1 (lines 1-2): 97 chars: CREATE TABLE dbo.parcel_status (code int NOT NULL, label varchar(20) NOT NULL)
  batch 2 (lines 4-4): 76 chars x2: INSERT dbo.parcel_status (code, label) VALUES (1, '***'), (2, '***')
  batch 3 (lines 6-6): 39 chars: SELECT COUNT(*) FROM dbo.parcel_status

SPLIT ONLY - no connection was made.
```

The rules, with the source of each:

| Rule | Source or reason |
|---|---|
| `GO` is a line of its own. It is case-insensitive. Only a count and a `--` comment may follow it. | The GO page: "A Transact-SQL statement cannot occupy the same line as a GO command. However, the line can contain comments." The tool accepts only a `--` comment, so `GO /* note */` is refused. That is stricter than the Microsoft text. |
| `GO 3` runs the batch before it 3 times. The count is ASCII digits, from 1 to 10000. | The GO page: "count Is a positive integer." The cap of 10000 is mine. The page sets none, and a typo such as `GO 99999999999` would loop that many times. |
| `GO;` is refused. | The GO page: "Do not use a semicolon as a statement terminator after GO." |
| The tool strips `GO` and sends each batch alone. | The GO page: "Applications based on the ODBC or OLE DB APIs receive a syntax error if they try to execute a GO command." |
| Block comments nest. `/*` inside a comment opens a second level. | The block-comment page: "Nested comments are supported." |
| A `GO` line inside an open comment, string or bracketed name is refused. So is a `GO` that ends a line of code or follows a comment on its line, such as `SELECT 1 /* a */ GO` or `SELECT 1 GO;`. | For the open-comment case the GO page does not say how the utilities treat it, so the tool refuses instead of guessing. For the same-line case the GO page says a statement cannot occupy the same line as `GO`. |
| A script that sets or names `PARSEONLY` in code is refused. A mention inside a comment or a string is not. | The PARSEONLY page: "The setting of SET PARSEONLY is set at parse time and not at execute or run time." I could not test a real server. A script that sets it could change the mode the preview relies on. |
| A script that sets `NOEXEC`, `FMTONLY`, `SHOWPLAN_ALL`, `SHOWPLAN_TEXT`, `SHOWPLAN_XML` or `IMPLICIT_TRANSACTIONS` is refused. | The NOEXEC page: SQL Server "parses and compiles each batch of Transact-SQL statements but does not execute them". The SHOWPLAN_ALL page: "Transact-SQL statements are not executed". The FMTONLY page: a rowset comes back "without any data rows". The IMPLICIT_TRANSACTIONS page: a statement starts a transaction that you must end with `COMMIT` or `ROLLBACK`. After any of these a deploy could print `DEPLOY OK` and have changed nothing. |
| A script that holds `BEGIN TRANSACTION` is accepted. A failure message then carries a note. | The BEGIN TRANSACTION page: an explicit transaction ends only with `COMMIT` or `ROLLBACK`. Under `autocommit` a batch inside one is not committed on its own, so the "already committed" count may be too high. The note names the batches. |
| After the last batch, `--apply` runs `SELECT @@TRANCOUNT`. A value above 0 prints `DEPLOY INCOMPLETE` and exits 2, not `DEPLOY OK`. | The @@TRANCOUNT page: `BEGIN TRANSACTION` increments it, and `COMMIT TRANSACTION` or `ROLLBACK TRANSACTION` bring it down. A script that opens a transaction and never commits has run every batch and kept nothing the transaction covers, so the tool does not say OK. The tool does not confirm what the server does when the connection then closes. |
| A `--` comment ends at a carriage return, a line feed, or both. A lone CR ends it. | The comment page: "Comments inserted with -- are terminated by a new line, which is specified with a carriage return character (U+000A), line feed character (U+000D), or a combination of the two." The page swaps the two code points (U+000A is the line feed), so the tool follows the words. |
| `[ ... ]` and `" ... "` open a name that can hold `--`, `/*`, `'` and a `GO`-looking line. A `]]` inside brackets and a `""` inside double quotes are escapes. A left bracket needs no escape. | The identifiers page: "If the identifier itself contains a right bracket ( ] ), escape it by doubling it ( ]] ). A left bracket ( [ ) doesn't require escaping." and "If the identifier itself contains a double quotation mark, escape it by doubling it ( "" )." |
| A stacked statement after a bracketed name or after a lone-CR comment is scanned like any other code: `SET NOEXEC ON` is refused and `BEGIN TRAN` is seen. A comma list such as `SET NOCOUNT, NOEXEC ON` is scanned too. | The SET statements page says one `SET` can name several options, so a refused option can follow a comma. |
| A line that starts with `:` and a letter (a sqlcmd directive) is refused. | Directives such as `:setvar` belong to sqlcmd. This tool does not run sqlcmd scripts. |
| A sqlcmd variable reference `$(Name)` outside a comment, inside a string or a bracketed name too, is refused. | The sqlcmd page describes the `-x` option as one that makes sqlcmd "ignore scripting variables" for scripts with strings that "have the same format as regular variables, such as `$(<variable_name>)`". So sqlcmd expands the reference inside a string. This tool does not expand it, and `CREATE DATABASE [$(Name)]` is valid Transact-SQL that would create a database with that literal name. |
| A script with a NUL character is refused with exit 64. | UTF-16 without a BOM, and UTF-32, decode to text full of NULs. The driver may read the SQL as null-terminated text and stop at the first one. I did not test that. |
| Line endings inside a batch are sent unchanged. | A CRLF inside a multi-line string literal is part of the data. |

The preview sends `SET PARSEONLY ON` first. The PARSEONLY page says that SQL Server then "only parses
the statement" and that the statement is examined "without compiling or executing" it. If that command
fails, the tool sends no batch. It sends `SET PARSEONLY OFF` again even when a batch raises.

Pages cited:

- <https://learn.microsoft.com/en-us/sql/t-sql/language-elements/sql-server-utilities-statements-go>
- <https://learn.microsoft.com/en-us/sql/t-sql/statements/set-parseonly-transact-sql>
- <https://learn.microsoft.com/en-us/sql/t-sql/language-elements/slash-star-comment-transact-sql>
- <https://learn.microsoft.com/en-us/sql/t-sql/statements/set-noexec-transact-sql>
- <https://learn.microsoft.com/en-us/sql/t-sql/statements/set-showplan-all-transact-sql> (The page names `SHOWPLAN_TEXT` as its sibling. I did not read the `SHOWPLAN_TEXT` or `SHOWPLAN_XML` pages, so those two rest on the sibling relation.)
- <https://learn.microsoft.com/en-us/sql/t-sql/statements/set-fmtonly-transact-sql>
- <https://learn.microsoft.com/en-us/sql/t-sql/statements/set-implicit-transactions-transact-sql>
- <https://learn.microsoft.com/en-us/sql/t-sql/language-elements/begin-transaction-transact-sql>
- <https://learn.microsoft.com/en-us/sql/t-sql/functions/trancount-transact-sql> (`BEGIN TRANSACTION` increments `@@TRANCOUNT`; `COMMIT TRANSACTION` decrements it; `ROLLBACK TRANSACTION` sets it to 0.)
- <https://learn.microsoft.com/en-us/sql/t-sql/language-elements/comment-transact-sql> (`--` comments end at a carriage return, a line feed, or both.)
- <https://learn.microsoft.com/en-us/sql/relational-databases/databases/database-identifiers> (`]]` and `""` escapes; double-quote delimiters need `QUOTED_IDENTIFIER ON`.)
- <https://learn.microsoft.com/en-us/sql/t-sql/statements/set-statements-transact-sql> ("SET QUOTED_IDENTIFIER, ANSI_NULLS ON sets both"; one `SET` can name several options.)
- <https://learn.microsoft.com/en-us/sql/relational-databases/errors-events/mssqlserver-1505-database-engine-error> (The server prints the duplicate key value in its message: "The duplicate key value is %ls." The tool masks that value in every batch. The parenthesised form `(value)` of Msg 2601 and 2627 is what I expect from those messages. I did not see it on a real server, and no Microsoft page I found quotes it.)
- <https://learn.microsoft.com/en-us/sql/tools/sqlcmd/sqlcmd-utility>
- <https://learn.microsoft.com/en-us/ssms/f1-help/database-engine-query-editor-sql-server-management-studio>
- <https://learn.microsoft.com/en-us/sql/connect/odbc/dsn-connection-string-attribute> (`Encrypt` defaults to `yes` from driver 18 and to `no` before. The tool writes `Encrypt=yes` out.)
- <https://github.com/mkleehammer/pyodbc/wiki/The-pyodbc-Module> (`connect(..., autocommit=False)` is the default. The tool passes `autocommit=True`.)
- <https://github.com/mkleehammer/pyodbc/wiki/Cursor> (`nextset()` is the call that reaches later result sets.)

## Refusals

These are real runs of the script, not descriptions.

```
$ python sqldeploy.py demo_ok.sql --server h --database d --dry-run
error: unrecognized arguments: --dry-run
(exit 64)

$ python sqldeploy.py demo_ok.sql --server h --database d --app
error: unrecognized arguments: --app
(exit 64)
```

A bad flag is named. Its value is not echoed:

```
$ python sqldeploy.py demo_ok.sql --split-only --password hunter2
error: unrecognized arguments: --password (1 more not shown, in case one is a secret)
(exit 64)
```

A string literal in a script is masked in the summary. The sample `cred.sql`:

```
CREATE LOGIN [svc_synth] WITH PASSWORD = 'Sup3rS3cret!x'
GO
SELECT 1
GO
```

```
$ python sqldeploy.py cred.sql --split-only
cred.sql: 2 batch(es), 2 with statements
  batch 1 (lines 1-1): 57 chars: CREATE LOGIN [svc_synth] WITH PASSWORD = '***'
  batch 2 (lines 3-3): 9 chars: SELECT 1

SPLIT ONLY - no connection was made.
```

Every parser sets `allow_abbrev=False`. The self-test pins that `--app`, `--appl`, `--ap` and `--a` are
refused before any connection opens, that `--apply=1` is refused, that a second path is refused, and that
`--sel` does not become `--self-test`.

A stacked statement behind a bracketed name is seen, not skipped. The sample `stacked.sql`:

```
SELECT [a]]b] FROM t; SET NOEXEC ON
GO
SELECT 2
GO
```

```
$ python sqldeploy.py stacked.sql --split-only
stacked.sql: 2 batch(es), 2 with statements
  line 1: REFUSED: batch 1 sets NOEXEC; that setting stops later batches from running while the server reports success, so a script must not set it

nothing was sent to any server.
(exit 1)
```

The same refusal fires when the `SET NOEXEC ON` follows a `--` comment that ends in a lone carriage
return (`-- note<CR>SET NOEXEC ON<CR>GO<CR>`), because that comment ends at the CR.

A sqlcmd variable is refused, because this tool would send `$(DbName)` to the server as text.
The sample `var.sql`:

```
CREATE DATABASE [$(DbName)]
GO
INSERT dbo.cfg (env) VALUES ('$(Env)')
GO
```

```
$ python sqldeploy.py var.sql --server h --database d --apply
var.sql: 2 batch(es), 2 with statements
  line 1: REFUSED: batch 1 has a sqlcmd variable reference such as $(Name); this tool does not expand variables, so the server would get the text as written
  line 3: REFUSED: batch 2 has a sqlcmd variable reference such as $(Name); this tool does not expand variables, so the server would get the text as written

nothing was sent to any server.
(exit 1)
```

A `GO` that hides inside a comment:

```
SELECT 1
GO
/*
GO
*/
SELECT 2
GO
```

```
$ python sqldeploy.py hidden.sql --split-only
hidden.sql: 2 batch(es), 2 with statements
  line 4: REFUSED: a GO line sits inside an open comment or string; the Microsoft GO page does not say how the utilities treat it

nothing was sent to any server.
(exit 1)
```

## How each claim was checked

| Claim | Where it ran |
|---|---|
| The split rules in the table above | `--self-test`: the splitter section, including the documented nested-comment example from the block-comment page |
| The preview executes nothing | `--self-test`: a fake cursor logs every call. The default run shows no `exec` entry. |
| `--apply` runs nothing if one batch fails to parse | `--self-test`: end to end through `main()` with real files in a temp directory |
| `--apply` stops at the first failing batch and says what is committed | `--self-test`, same harness |
| No batch is sent if `SET PARSEONLY ON` fails | `--self-test` |
| Unknown and abbreviated arguments are refused | `--self-test` and the real runs above |
| Summaries and the script path are printable ASCII | `--self-test`: a bracketed name with an accented letter and an escape character, a DEL character, and a script path with an accented letter. The error text of a bad argument holds only flag names that match `--[A-Za-z][A-Za-z0-9-]*`. |
| No script literal is printed | `--self-test`: a password literal, a literal that spans lines, a hex literal, and server messages that repeat a literal, with its apostrophe doubled or not, and a hex token |
| A bad flag value is not echoed | `--self-test` and the real run above |
| No password option | `--self-test` checks the connection string carries no `UID` or `PWD`. The tool's own source reads no environment variable. |
| A close error, Ctrl+C and a failure on run 2 of `GO N` | `--self-test`: a fake connection whose `close()` raises, a fake batch that raises `KeyboardInterrupt`, and a fake batch that fails on its second run |
| The real `pyodbc` connect path | Two real runs on Windows (repeated on 2026-11-11) in a scratch venv with `pyodbc` 5.5.0 and `ODBC Driver 17 for SQL Server`. Pointed at `tcp:127.0.0.1,1`, the driver was refused. Pointed at a local socket that accepts and closes at once, the driver failed in the handshake. Both times the tool exited 3 and ran nothing. |
| UTF-16 and UTF-8 BOM scripts | `--self-test`, at the decode level and end to end through `main()` |
| A lone CR ends a `--` comment; bracketed and double-quoted names hide `--`, `/*`, `'` and `GO`; a stacked `SET NOEXEC ON` or `BEGIN TRAN` after them is seen | `--self-test`: the lexer section, the cases marked `pinned defect` among them |
| An open transaction at the end is not reported OK | `--self-test`: a fake cursor counts `BEGIN TRAN` and `COMMIT`; an uncommitted script exits 2, a committed one exits 0, and a failing count query exits 2 |
| A duplicate key value in parentheses is masked, with or without a literal in the batch | `--self-test`: a fake server message in the Msg 2627 shape, for a batch with a literal and for an `INSERT ... SELECT` with none |
| A `$(Name)` reference is refused | `--self-test`: one in a bracketed name, one in a string, and three texts that are not references. The `var.sql` run above is real. |

The self-test writes small files in directories named `sqldeploy-test-*` under the system temp
directory, and removes them. Nothing else in the tool writes a file. Only `--apply` writes to a server.

Mutation check: a scratch harness, not shipped with this repo, applied 143 single edits to a copy of the
tool, one at a time. 141 made the self-test fail. The other two did not matter: one stripped a trailing
carriage return that the next step ignores anyway, and one also edited the expected string in the
self-test. The edits include: `allow_abbrev` turned on, the parse gate removed from `--apply`,
`PARSEONLY OFF` outside `finally`, the PARSEONLY scan without `IGNORECASE`, each exit code changed to
another number, each refused `SET` option removed, the `]]` escape removed, a double quote not opening a
name, the driver name not validated, literal masking removed, an unknown flag value echoed, the
transaction note removed, the GO cap removed, the NUL check removed, and Ctrl+C not caught. You cannot
reproduce the run from this repo alone.

That run was made before the lexer work (lone CR, bracketed and double-quoted names) and before the
open-transaction check, so it does not cover them. For those I made five single edits by hand and ran the
self-test after each: `_LINE_RE` that ignores a lone CR (10 assertions failed), no `]]` escape (2 failed),
`--` no longer starting a comment (23 failed), `[` no longer opening a name (14 failed), and the
open-transaction branch removed (1 failed). A sixth edit removed the duplicate-key mask (2 failed).

Coverage: `coverage run --branch sqldeploy.py --self-test` reports 100 percent (1083 statements, 264
branches). One line carries a pragma: `if __name__ == "__main__":  # pragma: no branch`. The false side
of that test runs only on import, and the self-test runs the file as a script.

Linters: `pyflakes` reports nothing. `python -W error` compiles the file without a warning.

## Limits

- **No real SQL Server was available.** The preview and `--apply` ran against a fake cursor, not against
  an instance. What the real server does under `SET PARSEONLY ON` rests on the PARSEONLY page. The
  story at the top, a typo in batch 9, rests on that page and the fake cursor. It is not a recorded
  server run.
- **The network path was exercised only as far as a failed connection.** SQL Server speaks TCP, not
  HTTP, so a `http.server` stub does not apply. A local accept-and-close socket made the real driver
  fail in its handshake. No stub spoke the SQL Server protocol, so the success path never met a socket.
- **A clean preview means the Transact-SQL syntax is valid.** PARSEONLY does not compile. This tool does
  not promise that an unknown table or a permission problem shows up before `--apply`. The PARSEONLY
  page does not mention dynamic SQL, and I did not test it. Assume text inside `EXEC` or
  `sp_executesql` strings is not checked.
- **A driver error during the preview is reported as a parse error.** A non-syntax driver error that
  leaves the connection alive, such as a query timeout, shows as `PARSE ERROR` for that batch and exit
  1. Only a lost connection exits 3, because the closing `SET PARSEONLY OFF` fails too. The tool sets no connect or query
  timeout, so a hung server can block a run.
- **`--apply` has no rollback.** It connects with `autocommit=True`, so each batch commits on its own.
  The parse gate cuts the chance of a half-deployed script. It does not remove it. A batch can still
  fail at run time, and then the earlier batches stay committed. The tool says how many.
- **A batch that fails part way may have partly run.** For `GO N`, the message also says that runs 1 to
  N-1 are committed. It cannot say how much of the failing run changed data.
- **A script with `BEGIN TRANSACTION` is not rolled back or committed by this tool.** After a failure
  the tool closes the connection. I did not confirm on a live server that this ends an open transaction.
  The message names the batches that hold the statement and says the commit count may be too high.
  After a clean run, `--apply` asks the server for `@@TRANCOUNT` and exits 2 if it is above 0. The
  count query itself ran only against the fake cursor. A transaction opened inside an `EXEC` string is
  counted by the server, so it is caught here too.
- **An error that the driver reports only while rows stream may not be seen.** `--apply` drains each
  batch with `nextset()`. A real server could raise a late error in a way that call does not surface,
  and the run would then print `DEPLOY OK`. A fake cursor cannot show this either way.
- **Ctrl+C and a failed close.** Ctrl+C during `--apply` prints the unexpected-error message. It does
  not say how many batches ran. An error while closing the connection prints a warning on stderr. The
  exit status then reflects the deploy, not the close.
- **The scan for refused `SET` options reads the script text.** `EXEC('SET PARSE' + 'ONLY OFF')` and
  other text built at run time is not seen.
- **`GO` inside a comment, string or bracketed name is refused.** A script that needs it must change.
  `GO /* note */` is also refused. A count above 10000 is refused.
- **Encodings.** Scripts must be UTF-8, UTF-8 with BOM, or UTF-16 with BOM. A legacy code page such as
  cp1252 exits 64. A NUL character is refused, which also catches UTF-16 without a BOM and most UTF-32
  files. A BOM in the middle of a file, left by joining files, is not removed. It can stop a `GO` line
  from being seen.
- **Python decides what counts as whitespace.** A non-breaking space after `GO` is read as a space, and
  a batch of only Unicode whitespace counts as empty. `sqlcmd` may read them differently. Non-ASCII
  digits after `GO` are refused as malformed.
- **sqlcmd features are not run.** `:r`, `:setvar` and `$(var)` are not expanded. A line that starts
  with a directive is refused, and so is a `$(Name)` reference outside a comment. The check reads the
  characters `$(`, a letter or underscore, letters, digits or underscores, and `)`. It also refuses a
  string that only looks like a variable, such as `'$(Env)'`. That is the safe direction.
- **A question mark may confuse the driver.** I did not test whether the ODBC driver reads a `?` in a
  script, even inside a comment, as a parameter marker. If it does, a legitimate script gets a false
  `PARSE ERROR`. That is the safe direction.
- **Masking is a best effort.** A string literal, and a hex literal of four bytes or more, is masked in
  batch summaries. Quoted text in a server message is masked when the batch holds a literal, and so is
  the value in `The duplicate key value is (...)`, which is masked in every batch. A literal that holds an apostrophe is also masked
  when the server prints it with the apostrophe undoubled, with or without a space at either end of
  the literal. I did not test any of this against real
  server messages. Bracketed names are shown. A secret in a comment is blanked, not printed.
  Exceptions that are not driver errors are printed as they come, with no masking. A secret typed as an
  unknown flag name (`--SuperSecret99`) is echoed, because the error names unknown flags. Do not put a
  secret in a flag name.
- **Only quoted text and long hex are masked.** A hex literal shorter than four bytes is shown. A hex
  literal in a server message is masked only when the batch holds a literal. A server message that
  repeats any other value from table data, such as a conversion failure on a column value of an
  `INSERT ... SELECT`, is printed as the server sent it, because the tool cannot know that the value
  is secret.
- **Server error text is masked by pairing single quotes.** A literal that the server repeats after an
  apostrophe in its own wording, without a closing quote, or in double quotes is printed as sent. I
  tested this only with made-up server messages.
- **Curly quotes are not string delimiters here.** Text between U+2018 and U+2019 is read as code, so a
  literal inside them is not masked. I did not check how SQL Server reads those characters.
- **A column named after a refused `SET` option is refused as a false positive.** `UPDATE t SET noexec = 1`
  is read as `SET NOEXEC`. This is the safe direction. It affects columns named `noexec`, `fmtonly`,
  `showplan_all`, `showplan_text`, `showplan_xml` or `implicit_transactions`.
- **The comma-list form is scanned for every refused option.** `SET a, b ON` is scanned for the
  `SHOWPLAN` options too, although the SET statements page says the multi-option form does not apply to
  statistics options. Refusing it is harmless.
- **`DEPLOY OK` means no batch raised and no transaction was left open.** It does not show that the work
  persisted. A script that ends in `ROLLBACK`, or that catches an error in `TRY...CATCH` without
  `THROW`, prints `DEPLOY OK` and exits 0.
- **A failure before the first batch uses the same message as a failure during a batch.** If the
  cursor cannot be created after the connect, the tool still prints that some batches may already have
  run and are committed, and exits 2. Nothing ran in that case.
- **A batch with hundreds of thousands of blank or comment-only lines splits very slowly.** The time
  grows with the square of the line count. 20,000 comment lines split in 0.7 s on Windows and 200,000
  took about a minute. The cause is the `^\s*:[A-Za-z]` directive scan over the comment-blanked text.
- **A `--` comment ends only at CR or LF.** The tool follows the comment page. Unicode line separators
  (U+2028, U+2029, U+0085), vertical tab and form feed do not end it here. SQL Server, `sqlcmd` or SSMS
  may treat them differently. I did not test that. If one does, a `SET NOEXEC ON` after such a character
  is hidden from the scan.
- **A lone CR counts as a line break in reported line numbers.** In a file that uses CR alone, each CR
  adds one to the line number. SSMS may count lines differently for such a file.
- **A double-quoted name is a string literal when `QUOTED_IDENTIFIER` is OFF.** The identifiers page says
  so. The tool treats double-quoted text as masked either way in the batch summary. That is the safe
  direction. A server message that repeats double-quoted text is not masked.
- **A line of code that ends in the word `go` is refused.** `SELECT 1 /* a */ GO` and `SELECT 1 GO;` are refused because
  the GO page says a statement cannot share a line with `GO`. `SELECT x AS go` and `FROM t go` (an alias) are
  refused too, which is a false refusal in the safe direction. `dbo.go`, `[go]` and `go` inside a
  literal are not refused.
- **Text inside `EXEC` and `sp_executesql` strings is not parsed or scanned.** A syntax error in `@sql`
  still gives `parses clean` for the batch, and `parses clean` means the outer batch only. A refused
  `SET`, such as `SET NOEXEC ON` inside `EXEC('...')`, is not seen. I did not check whether such a `SET`
  stays in effect after the `EXEC` returns.
- **`SET STATISTICS PARSE` is not in the refused list.** I did not check its semantics.
- **Python treats VT, FF, FS, GS, RS, US, NEL and NBSP as whitespace.** A batch that holds only those
  characters counts as empty and is dropped from the batch count, not listed as skipped.
- **A line that starts with the word `go` inside an open comment or string is refused.** Prose such as
  `go live Monday` on its own line in a block comment or a multi-line literal is read as a `GO` line.
  This is a safe false refusal and exits 1. I ran it.
- **`-hSOMETHING` prints help and exits 0.** argparse reads a short flag glued to `-h` as `-h`, so the
  tool does not refuse it as an unknown argument. I ran `-hFOO`. Nothing is sent to a server.
- **A `GO` count of ten or more digits is refused even when its value is small.** `GO 0000000001` exits
  1 with the count message, because the check uses the digit count.
- **A driver error is read as `(sqlstate, message)`.** When the exception has two arguments and the second
  is text, the tool masks and prints that message. Any other exception prints as `str()` of it. I tested
  the two-argument shape with a hand-built exception, never with a real SQL Server message.
- **A script with one multi-megabyte string literal splits slowly.** A literal of 400,000 characters
  took about 3 seconds on Windows. Time grows faster than the length.
- **`SET ANSI_DEFAULTS ON` and `SET ROWCOUNT n` are not refused.** The SET ANSI_DEFAULTS page says the
  first turns `IMPLICIT_TRANSACTIONS` on, and an open transaction is reported only at the end as
  `DEPLOY INCOMPLETE`. The tool does not detect `ROWCOUNT`. I did not run either against a server.
- **A server message that repeats a literal without single quotes is printed unmasked.** SQL Server does
  not normally echo a literal that way. I made the test message up.
- **`--apply` sets no query timeout.** A batch that blocks, for example behind a lock, waits until you
  press Ctrl+C, which exits 2.
- **The text `(comments only)` is only a label.** A batch is empty when it has no visible code. A batch
  that starts with that text is not skipped, and its statements are parsed and run like any other.
- **Self-test labels that start with an acronym** (`GO`, `CRLF`) are capitalised. This is a style nit.
- **Exit 1 has two meanings.** A real finding and a driver error during the preview both exit 1, and an
  uncaught Python exception exits 1 as well. A calling script cannot tell them apart by the code.
- **`--self-test` and `--split-only` ignore some flags.** `--self-test` ignores a path and the valid
  flags next to it, but an unknown flag or a flag without its value is refused first (exit 64).
  `--split-only` ignores `--odbc-driver` and `--trust-server-certificate`.
- **One pragma.** 100 percent branch coverage depends on `# pragma: no branch` on the `__main__` guard.
- **The mutation harness is not shipped.** The mutation result above cannot be reproduced from this repo.
  The counts are a record of earlier self-test versions: the `--` edit failed 21 assertions then and 23
  now, and the `[` edit failed 13 then and 14 now. The 143-edit figure cannot be checked from here. Only
  the six hand edits can be rerun.
- **Only the listed statements are flagged as stacked.** The tool finds `SET NOEXEC`, `FMTONLY`,
  `SHOWPLAN_*`, `IMPLICIT_TRANSACTIONS`, `PARSEONLY`, `BEGIN TRAN`, sqlcmd directives and `$(var)`, also
  after a bracketed name, a quoted name or a lone-CR comment. It does not flag any other second
  statement, such as a `DROP` after a semicolon.
- **Typical splitting is linear.** About 50 microseconds per line on Windows (Python 3.13): 100,000
  `INSERT` lines in 100 batches took 4.8 s, and one 20,000-line batch took 1.1 s. Your machine will differ. The slow cases are the comment-only and
  long-literal ones above.
- **A secret written without quotes is not a literal.** If the script has `PASSWORD = Sup3rS3cret` and
  the server error repeats it, the tool prints it as the server sent it, because it masks only quoted
  text and long hex.
- **Exit 64 covers two things.** A bad command line and a script file that cannot be read both exit 64.
- **I did not test pooled ODBC connections.** If `SET PARSEONLY OFF` failed after a batch error, a pooled
  connection could keep `PARSEONLY ON`. `--apply` uses a second connection, and the driver normally
  resets session options on reuse, but I did not check either against a server.
- **Windows authentication only.** On Linux this means Kerberos through the ODBC driver. I did not test it.
- **The default driver name is `ODBC Driver 18 for SQL Server`.** If you have another version, pass
  `--odbc-driver`.
- **A script that turns a refused `SET` option OFF is refused too.** The tool reads the option name, not
  the ON or OFF word, so `SET NOEXEC OFF` and `SET IMPLICIT_TRANSACTIONS OFF` exit 1 with the same wording.
- **A GO after a lone carriage return is split as a `GO` line.** The comment page documents a lone CR only
  for `--` comments. I did not check that sqlcmd or SSMS ends a line at a lone CR for `GO`.
- **The success path of the ODBC connection never met a real server.** Connect, `SET PARSEONLY`, the
  batches and `nextset()` ran only against a fake cursor. The two real-driver runs (a refused port and an
  accept-and-close socket) need a scratch venv with `pyodbc` and an ODBC driver. They are not in this repo.
- **A connection failure prints the driver message as it came.** The text can be long and can name the
  server and the driver. A Windows-authentication connection string holds no password.
- **A partial server echo of a literal with an apostrophe is partly masked.** If SQL Server prints a
  truncated copy of such a literal, the part after the apostrophe can show, because the whole-literal
  mask needs the full literal. Example: `'it''s Sup3rS3cret'` echoed as `'it's Sup3'` prints `'***'s Sup3'`.
- **The Ctrl+F5 shortcut for SSMS Parse** comes from the SSMS Query Editor page, which I read on 2026-11-11.
- **Python 3.8 and earlier are untested.**

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [txnlint](https://github.com/uhsear/txnlint) - lints transaction scope across GO batches
- [safe-republish](https://github.com/uhsear/safe-republish) - the same "check by default, `--apply` to write" rule for a feature class
- [arcade-rule-deploy](https://github.com/uhsear/arcade-rule-deploy) - a preflight-checked deploy of attribute rules to a geodatabase
