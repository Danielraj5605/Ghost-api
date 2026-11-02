"""Hide a harmless traceback the local servers print on Windows.

When a browser closes a connection (a reload, a closed tab, a finished download), the
Windows asyncio event loop logs "Exception in callback
_ProactorBasePipeTransport._call_connection_lost()" with ConnectionResetError [WinError
10054]. Nothing failed: the reply was already sent. It is a known CPython issue
(https://github.com/python/cpython/issues/83191). Only that exact case is dropped.
"""

from __future__ import annotations

import logging


class _ResetFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not (
            isinstance(exc, ConnectionResetError) and "_call_connection_lost" in record.getMessage()
        )


_FILTER = _ResetFilter()


def quiet_connection_resets() -> None:
    """Install the filter on the asyncio logger (once; safe to call again)."""
    logger = logging.getLogger("asyncio")
    if _FILTER not in logger.filters:
        logger.addFilter(_FILTER)
