# VisiData Fork

## Overview

This is a personal fork of [VisiData](https://github.com/saulpw/visidata).

## Changes from Upstream

### Clipboard Key Binding Reorganization

Swapped uppercase/lowercase key bindings for clipboard operations.

| Action | Old Binding | New Binding |
|--------|-------------|-------------|
| Copy row to **system** clipboard | `Y` | `y` |
| Copy row to **internal** clipboard | `y` | `Y` |
| Copy selected to **system** clipboard | `gY` | `gy` |
| Copy selected to **internal** clipboard | `gy` | `gY` |
| Copy cell to **system** clipboard | `zY` | `zy` |
| Copy cell to **internal** clipboard | `zy` | `zY` |
| Copy column cells to **system** | `gzY` | `gzy` |
| Copy column cells to **internal** | `gzy` | `gzY` |

**Note**: All paste commands (`p`, `P`, etc.) remain unchanged.

**File Modified**: `visidata/clipboard.py`

### vdsql (Ibis/database app)

Ongoing work on the bundled `vdsql` app: a command to switch databases on a live connection, plus fixes for schema-qualified queries and URL scheme dispatch. Tested against PostgreSQL and MySQL.

See **[visidata/apps/vdsql/FORK_README.md](visidata/apps/vdsql/FORK_README.md)** for the details.

**Files Modified**: `visidata/apps/vdsql/_ibis.py`, `visidata/apps/vdsql/__main__.py`, `visidata/apps/vdsql/bigquery.py`

### Suppressed Startup Messages

Disabled version info display and MOTD (message of the day) on startup.

### Disabled stderr Status Output

Status messages no longer print to stderr when running VisiData in non-curses mode.
