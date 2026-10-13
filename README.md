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

## What exists already

- **sqlcmd and SQL Server Management Studio** run scripts and handle `GO` themselves. They are the
  reference behaviour. Microsoft documents `GO` as a command of those utilities, not of T-SQL.
  They run a script batch by batch. I did not check whether either offers a parse-only pass over a
  whole file, so I make no claim about that.
  [GO](https://learn.microsoft.com/en-us/sql/t-sql/language-elements/sql-server-utilities-statements-go)
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
PASS  --apply with one unparseable batch runs NOTHING  <-- pinned defect
...
--------------------------------------------------------------------
125 assertions, 0 failed
```

The count is the same on Windows (Python 3.13.2), Linux (Python 3.12.3) and Python 3.9.25.
No assertion depends on the platform.

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
| `--odbc-driver` | `ODBC Driver 18 for SQL Server` | Name of the installed ODBC driver. |
| `--trust-server-certificate` | off | Skip server certificate validation. |
| `--split-only` | off | List the batches and exit. Cannot combine with `--apply`, `--server` or `--database`. |
| `--apply` | off | Parse every batch, then run them. Without it nothing is written. |
| `--self-test` | off | Run the offline assertions and exit. |

Exit codes: 0 clean, 1 refused (a script problem or a batch that does not parse), 2 `--apply` stopped
part way, 3 could not connect or could not enter parse-only mode, 64 usage error or unreadable file.

The tool reads no environment variable, so none can switch on `--apply`. It uses Windows
authentication only (`Trusted_Connection=yes`). It never reads, stores or prints a password.

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
  batch 2 (lines 4-4): 76 chars x2: INSERT dbo.parcel_status (code, label) VALUES (1, 'active'), (2, 'retired')
  batch 3 (lines 6-6): 39 chars: SELECT COUNT(*) FROM dbo.parcel_status

SPLIT ONLY - no connection was made.
```

The rules, with the source of each:

| Rule | Source or reason |
|---|---|
| `GO` is a line of its own. It is case-insensitive. Only a count and a `--` comment may follow it. | The GO page: "A Transact-SQL statement cannot occupy the same line as a GO command. However, the line can contain comments." |
| `GO 3` runs the batch before it 3 times. The count must be a positive integer. | The GO page: "count Is a positive integer." |
| `GO;` is refused. | The GO page: "Do not use a semicolon as a statement terminator after GO." |
| The tool strips `GO` and sends each batch alone. | The GO page: "Applications based on the ODBC or OLE DB APIs receive a syntax error if they try to execute a GO command." |
| Block comments nest. `/*` inside a comment opens a second level. | The block-comment page: "Nested comments are supported." |
| A `GO` line inside an open comment, string or bracketed name is refused. | The GO page does not say how the utilities treat it. The tool refuses instead of guessing. |
| A script that mentions `PARSEONLY` is refused. | The PARSEONLY page: "The setting of SET PARSEONLY is set at parse time and not at execute or run time." I could not test a real server. A script that sets it could change the mode the preview relies on. |
| A line that starts with `:` and a letter (a sqlcmd directive) is refused. | Directives such as `:setvar` belong to sqlcmd. This tool does not run sqlcmd scripts. |
| Line endings inside a batch are sent unchanged. | A CRLF inside a multi-line string literal is part of the data. |

The preview sends `SET PARSEONLY ON` first. The PARSEONLY page says that SQL Server then "only parses
the statement" and that the statement is examined "without compiling or executing" it. If that command
fails, the tool sends no batch. It sends `SET PARSEONLY OFF` again even when a batch raises.

Pages cited:

- <https://learn.microsoft.com/en-us/sql/t-sql/language-elements/sql-server-utilities-statements-go>
- <https://learn.microsoft.com/en-us/sql/t-sql/statements/set-parseonly-transact-sql>
- <https://learn.microsoft.com/en-us/sql/t-sql/language-elements/slash-star-comment-transact-sql>
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
| Output is printable ASCII | `--self-test`: a summary with an accented letter and an escape character |
| No password path | `--self-test` checks the connection string carries no `UID` or `PWD`. The source reads no environment variable. |
| The real `pyodbc` connect path | A real run in a scratch venv with `pyodbc` 5.3.0 and `ODBC Driver 17 for SQL Server`, pointed at `tcp:127.0.0.1,1`. It failed in the driver and the tool exited 3 and ran nothing. |
| UTF-16 and UTF-8 BOM scripts | `--self-test` |

The self-test writes small temp files and removes them. It writes nothing else.

Mutation check: 16 deliberate breaks were applied to a copy, one at a time. Each made the self-test fail.
They were: `allow_abbrev` turned on, the parse gate removed from `--apply`, check mode applying,
`PARSEONLY OFF` outside `finally`, the comment-GO refusal removed, nesting removed, `splitlines`-style
line breaks, the PARSEONLY scan removed, semicolons allowed in a connection value, stop-at-first-failure
removed, `nextset()` not drained, GO count ignored, ASCII scrubbing removed, certificate trust on by
default, `Encrypt=yes` dropped, and `error()` ignoring unknown arguments.

Coverage: `coverage run --branch sqldeploy.py --self-test` reports 100 percent (708 statements, 158
branches). One line carries a pragma: `if __name__ == "__main__":  # pragma: no branch`. The false side
of that test runs only on import, and the self-test runs the file as a script.

Linters: `pyflakes` reports nothing. `python -W error` compiles the file without a warning.

## Limits

- **No real SQL Server was available.** The preview and `--apply` ran against a fake cursor, not against
  an instance. What the real server does under `SET PARSEONLY ON` rests on the PARSEONLY page. The
  real `pyodbc` call was exercised only as far as a refused connection. There is no HTTP or other
  network service to stub, so the "local stub server" step does not apply. The fake cursor stands in
  for it.
- **A clean preview means the syntax is valid.** PARSEONLY does not compile. This tool does not promise
  that an unknown table or a permission problem shows up before `--apply`.
- **`--apply` has no rollback.** It connects with `autocommit=True`, so each batch commits on its own.
  The parse gate cuts the chance of a half-deployed script. It does not remove it. A batch can still
  fail at run time, and then the earlier batches stay committed. The tool says how many.
- **`GO` inside a comment, string or bracketed name is refused.** A script that needs it must change.
- **sqlcmd features are not run.** `:r`, `:setvar` and `$(var)` are not expanded. A line that starts
  with a directive is refused. A `$(var)` in a batch is sent as text and the server decides.
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
