"""The `ghost-api` command line.

_common.py   the app, global options (--json, --plain, project defaults) and helpers
explore.py   run, import, refresh, login, logout
client.py    generate, verify, report, diff, mock
data.py      fetch, watch, serve, mcp, ui
manage.py    init, doctor, inspect, forget
"""

# isort: off
# Importing the command modules registers their commands, in this order in --help.
from ghost_api.cli import explore  # noqa: F401
from ghost_api.cli import client  # noqa: F401
from ghost_api.cli import data  # noqa: F401
from ghost_api.cli import manage  # noqa: F401

# isort: on
from ghost_api.cli._common import (
    GLOBAL_FLAGS,
    _invoke,
    _json_first,
    _options,
    _RunResult,
    app,
    console,
    entry,
    json_out,
    safe_output,
)
from ghost_api.cli.data import schedule_command

__all__ = [
    "GLOBAL_FLAGS",
    "_RunResult",
    "_invoke",
    "_json_first",
    "_options",
    "app",
    "console",
    "entry",
    "json_out",
    "safe_output",
    "schedule_command",
]
