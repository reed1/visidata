# vdsql Fork Changes

Fork-local changes to `vdsql`. See the upstream [README.md](README.md) for everything else — it is deliberately left untouched, so this file is the only documentation of these changes.

Tested working against **PostgreSQL** and **MySQL**.

## New Feature: Changing Databases

`vdsql` connects to one database and stays there — there was no way to reach another database on the same server without quitting and relaunching.

| Key | Command | Sheet | Description |
|-----|---------|-------|-------------|
| `o` | `open-databases` | tables index | open a sheet of the databases on this connection |
| `Enter` | `set-database` | databases | reconnect to the database in the current row |
| (unbound) | `set-database` | tables index | reconnect to a database entered by name |

The databases sheet lists `con.list_catalogs()`, falling back to `con.list_databases()` on engines with no catalog level (MySQL, SQLite), and marks the connection's current database in the `current?` column.

`set-database` rewrites the database in the connection URL, opens a fresh index sheet on a new connection pool, and removes every other open sheet — the same state as launching `vdsql` against that database directly. Server, credentials, and URL options are untouched; only the first path segment changes, so anything after it (such as a Snowflake schema) is kept.

`o` **shadows VisiData's `open-file`** on the tables index sheet only; `o` still opens files everywhere else. The databases sheet is pushed rather than replacing the index sheet, so `q` backs out without switching.

This operates one level above the `postgres_schema` option: the databases sheet lists whole databases (a *catalog* in Ibis terms), while `postgres_schema` selects schemas within the current database.

Not supported for file-backed sources (`sqlite://`, `duckdb://`) — there is no server to reconnect to, and `set-database` fails with a status message instead.

### Per-engine behaviour

**PostgreSQL** has a catalog level, so the sheet lists real databases and marks the connected one:

    vdsql postgresql://user:pass@host:5432/postgres

**MySQL** has no catalog level, so the fallback to `list_databases()` lists the databases directly, and it works the same way:

    vdsql 'mysql://user:pass@127.0.0.1:7001'

MySQL needs no database in the URL. Ibis then reports no current database, so the `current?` column is empty and the initial index sheet lists ~1500 `information_schema` tables — press `o` immediately and pick one, or put the database in the URL to start somewhere useful.

**Files modified**: `_ibis.py`

## Bugfixes

### `database_name` never reached the query

With `--postgres-schema` set, table sheets could not query tables outside the connection's default search path. Two independent breaks:

- `IbisTableIndexSheet.iterload()` passed `database_name=self.database_name` (always `None`) to each table sheet instead of the `dbname` it had just iterated over.
- `baseQuery()` qualified the table through `fqtblname()`, which relied on `con._fully_qualified_name` — removed from Ibis in 9.0 — and so silently fell back to the bare table name.

Now the listed schema is propagated, and qualification goes through Ibis's namespace API (`con.table(name, database=...)` and `ibis.table(..., catalog=, database=)`). Verified against PostgreSQL: `--postgres-schema=information_schema` compiles to `SELECT * FROM "information_schema"."tables"` and loads; previously it raised `TableNotFound`.

The `dbname` column on the index sheet now reads from `database_name` rather than a separately-assigned attribute, so it always reflects what the query actually uses.

**Files modified**: `_ibis.py`

### `postgresql://` URLs bypassed vdsql entirely

`__main__.py` overrides `vd.openurl_<backend>` using Ibis's backend entry-point names, which include `postgres` but not `postgresql`. VisiData's builtin `openurl_postgresql` therefore won, and `vdsql postgresql://…` silently loaded a builtin `PgTablesSheet` with no Ibis, no SQL sidebar, and none of the vdsql commands. Only `postgres://` reached vdsql.

Fixed with a scheme-alias map, so both spellings route to the vdsql loader.

**Files modified**: `__main__.py`

### `con.set_database()` no longer exists in Ibis

`IbisTableIndexSheet.iterload()` called `con.set_database()` whenever `database_name` was set. That method was removed from Ibis, so it raised `AttributeError` — reachable via the BigQuery dataset path. The call was redundant (the connection is already scoped) and has been removed.

**Files modified**: `_ibis.py`

### BigQuery passed a dotted namespace

`BigqueryDatabaseIndexSheet.openRow()` passed `database_name='project.dataset'`. Under the namespace API that would be quoted as a single identifier. Now passed as `catalog=project` and `database_name=dataset`.

Untested — no BigQuery access here.

**Files modified**: `bigquery.py`

## Notes

- The upstream `README.md` and `CHANGELOG.md` are intentionally unmodified — everything is recorded here instead.
- `test.sh` fails on every replay (exit 1, no output) on the **unmodified upstream tree** as well, so it was not usable as a regression check. Verification was manual: SQLite, PostgreSQL, and MySQL loads, schema-qualified queries, and the full keystroke flow driven through tmux.
