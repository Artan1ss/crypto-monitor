import logging

import httpx
import pytest

from btc_monitor.logging import configure_logging
from btc_monitor.telegram import TelegramClient


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["status", "transport", "payload"])
async def test_telegram_errors_do_not_expose_credentials(failure):
    token = "123456:private-test-token"
    client = TelegramClient(True, token, "private-chat")
    await client.client.aclose()

    def respond(request):
        if failure == "transport":
            raise httpx.ConnectError(f"Failed {request.url}", request=request)
        if failure == "payload":
            return httpx.Response(200, json={"ok": False, "description": token, "error_code": 400})
        return httpx.Response(401, json={"ok": False})

    client.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(RuntimeError) as caught:
            await client.send("test")
        assert token not in str(caught.value)
        assert "private-chat" not in str(caught.value)
        if failure != "payload":
            assert caught.value.__suppress_context__
    finally:
        await client.close()


def test_http_request_logging_is_suppressed():
    configure_logging("DEBUG")
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
