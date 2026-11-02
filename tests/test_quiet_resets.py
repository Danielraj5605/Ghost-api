"""The harmless Windows ConnectionResetError traceback is hidden; other errors are not."""

import logging

import pytest

from ghost_api.quiet_resets import quiet_connection_resets

LOST = "Exception in callback _ProactorBasePipeTransport._call_connection_lost()"


def _log(exc: BaseException, message: str) -> None:
    try:
        raise exc
    except BaseException:
        logging.getLogger("asyncio").error(message, exc_info=True)


def test_only_the_connection_lost_reset_is_dropped(caplog: pytest.LogCaptureFixture) -> None:
    quiet_connection_resets()
    quiet_connection_resets()  # twice is fine
    with caplog.at_level(logging.ERROR, logger="asyncio"):
        _log(ConnectionResetError(10054, "forcibly closed"), LOST)
        _log(ConnectionResetError(10054, "x"), "Exception in callback something_else()")
        _log(
            ValueError("real"),
            "Exception in callback _ProactorBasePipeTransport._call_connection_lost()",
        )
    messages = [
        (r.getMessage(), type(r.exc_info[1]).__name__) for r in caplog.records if r.exc_info
    ]
    assert messages == [
        ("Exception in callback something_else()", "ConnectionResetError"),
        ("Exception in callback _ProactorBasePipeTransport._call_connection_lost()", "ValueError"),
    ]
