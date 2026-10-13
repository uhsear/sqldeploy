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
- A `GO` it cannot split with certainty is an error, not a guess.
- A script that sets `NOEXEC`, `FMTONLY`, `SHOWPLAN_*` or `IMPLICIT_TRANSACTIONS` is an error. Those
  settings make later batches stop running while the server still says OK.
- The text of a string literal is masked in every batch summary and in the server's error text for
  that batch, so a password in your script does not reach a log.

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
...
PASS  a prefix --app of --apply is REFUSED before any connection  <-- pinned defect
...
PASS  a password literal is masked in the batch summary  <-- pinned defect
...
PASS  an unknown flag is named but its value is not echoed  <-- pinned defect
...
PASS  --apply with one unparseable batch runs NOTHING  <-- pinned defect
...
--------------------------------------------------------------------
192 assertions, 0 failed
```

The count is the same on Windows (Python 3.13.2), Linux (Python 3.12.3) and Python 3.9.25.
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
| `--self-test` | off | Run the offline assertions and exit. It ignores every other argument and every path. |

Exit codes: 0 clean, 1 refused (a script problem or a batch that does not parse), 2 `--apply` stopped
part way, 3 could not connect or could not enter parse-only mode, 64 usage error or unreadable file.

The tool's own code reads no environment variable, so none can switch on `--apply`. Other code
reads some: `argparse` reads `COLUMNS` to wrap `--help`, and `pyodbc` and the ODBC driver read their
own settings. The tool uses Windows authentication only (`Trusted_Connection=yes`). It has no password
option and never reads or stores a password. It prints no string literal from your script in a batch
summary or a batch error. A literal is shown as `'***'`, and so is quoted text in a server error. A bad
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
| A `GO` line inside an open comment, string or bracketed name is refused. | The GO page does not say how the utilities treat it. The tool refuses instead of guessing. |
| A script that mentions `PARSEONLY` is refused. | The PARSEONLY page: "The setting of SET PARSEONLY is set at parse time and not at execute or run time." I could not test a real server. A script that sets it could change the mode the preview relies on. |
| A script that sets `NOEXEC`, `FMTONLY`, `SHOWPLAN_ALL`, `SHOWPLAN_TEXT`, `SHOWPLAN_XML` or `IMPLICIT_TRANSACTIONS` is refused. | The NOEXEC page: SQL Server "parses and compiles each batch of Transact-SQL statements but does not execute them". The SHOWPLAN_ALL page: "Transact-SQL statements are not executed". The FMTONLY page: a rowset comes back "without any data rows". The IMPLICIT_TRANSACTIONS page: a statement starts a transaction that you must end with `COMMIT` or `ROLLBACK`. After any of these a deploy could print `DEPLOY OK` and have changed nothing. |
| A script that holds `BEGIN TRANSACTION` is accepted. A failure message then carries a note. | The BEGIN TRANSACTION page: an explicit transaction ends only with `COMMIT` or `ROLLBACK`. Under `autocommit` a batch inside one is not committed on its own, so the "already committed" count may be too high. The note names the batches. |
| A line that starts with `:` and a letter (a sqlcmd directive) is refused. | Directives such as `:setvar` belong to sqlcmd. This tool does not run sqlcmd scripts. |
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
error: unrecognized arguments: --password (1 more not shown, in case it is a secret)
(exit 64)
```

A string literal in a script is masked in the summary:

```
$ python sqldeploy.py cred.sql --split-only
cred.sql: 2 batch(es), 2 with statements
  batch 1 (lines 1-1): 64 chars: CREATE LOGIN [svc_synth] WITH PASSWORD = '***'
  batch 2 (lines 3-3): 9 chars: SELECT 1

SPLIT ONLY - no connection was made.
```

Every parser sets `allow_abbrev=False`. The self-test pins that `--app`, `--appl`, `--ap` and `--a` are
refused before any connection opens, that `--apply=1` is refused, that a second path is refused, and that
`--sel` does not become `--self-test`.

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
| No script literal is printed | `--self-test`: a password literal, a literal that spans lines, and a server message that repeats a literal |
| A bad flag value is not echoed | `--self-test` and the real run above |
| No password option | `--self-test` checks the connection string carries no `UID` or `PWD`. The tool's own source reads no environment variable. |
| A close error, Ctrl+C and a failure on run 2 of `GO N` | `--self-test`: a fake connection whose `close()` raises, a fake batch that raises `KeyboardInterrupt`, and a fake batch that fails on its second run |
| The real `pyodbc` connect path | Two real runs in a scratch venv with `pyodbc` 5.3.0 and `ODBC Driver 17 for SQL Server`. Pointed at `tcp:127.0.0.1,1`, the driver was refused. Pointed at a local socket that accepts and closes at once, the driver failed in the handshake. Both times the tool exited 3 and ran nothing. |
| UTF-16 and UTF-8 BOM scripts | `--self-test` |

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

Coverage: `coverage run --branch sqldeploy.py --self-test` reports 100 percent (910 statements, 212
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
- **A driver error during the preview is reported as a parse error.** A lost connection or a timeout
  shows as `PARSE ERROR` for that batch and exit 1, not as exit 3. The tool sets no connect or query
  timeout, so a hung server can block a run.
- **`--apply` has no rollback.** It connects with `autocommit=True`, so each batch commits on its own.
  The parse gate cuts the chance of a half-deployed script. It does not remove it. A batch can still
  fail at run time, and then the earlier batches stay committed. The tool says how many.
- **A batch that fails part way may have partly run.** For `GO N`, the message also says that runs 1 to
  N-1 are committed. It cannot say how much of the failing run changed data.
- **A script with `BEGIN TRANSACTION` is not rolled back or committed by this tool.** After a failure
  the tool closes the connection. I did not confirm on a live server that this ends an open transaction.
  The message names the batches that hold the statement and says the commit count may be too high.
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
  with a directive is refused. A `$(var)` in a batch is sent as text and the server decides. Under
  `--apply`, `CREATE DATABASE [$(Name)]` would create a database named `$(Name)`.
- **A question mark may confuse the driver.** I did not test whether the ODBC driver reads a `?` in a
  script, even inside a comment, as a parameter marker. If it does, a legitimate script gets a false
  `PARSE ERROR`. That is the safe direction.
- **Masking is a best effort.** A string literal is masked in batch summaries. Quoted text in a server
  message is masked when the batch holds a literal. I did not test this against real server messages.
  Bracketed names are shown. A secret in a comment is blanked, not printed.
- **`--self-test` and `--split-only` ignore some flags.** `--self-test` ignores every other argument.
  `--split-only` ignores `--odbc-driver` and `--trust-server-certificate`.
- **One pragma.** 100 percent branch coverage depends on `# pragma: no branch` on the `__main__` guard.
- **The mutation harness is not shipped.** The mutation result above cannot be reproduced from this repo.
- **Windows authentication only.** On Linux this means Kerberos through the ODBC driver. I did not test it.
- **The default driver name is `ODBC Driver 18 for SQL Server`.** If you have another version, pass
  `--odbc-driver`.
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
