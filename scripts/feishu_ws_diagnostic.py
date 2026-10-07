"""Outbound Feishu long-connection diagnostic; never prints credentials or URLs."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time

import lark_oapi as lark
import websockets


async def main() -> None:
    client = lark.ws.Client(
        os.environ["FEISHU_APP_ID"], os.environ["FEISHU_APP_SECRET"],
        auto_reconnect=False, log_level=lark.LogLevel.ERROR,
    )
    start = time.monotonic()
    try:
        url = client._get_conn_url()
        print(f"FEISHU_WS_ENDPOINT_READY hash={hashlib.sha256(url.encode()).hexdigest()[:12]}", flush=True)
        try:
            async with websockets.connect(url, proxy=None, open_timeout=10) as connection:
                print(f"FEISHU_WS_CONNECTED seconds={time.monotonic()-start:.2f}", flush=True)
                await asyncio.wait_for(connection.wait_closed(), timeout=5)
        except Exception as exc:
            print(f"FEISHU_WS_CONNECT_FAILED type={type(exc).__name__} seconds={time.monotonic()-start:.2f}", flush=True)
    except Exception as exc:
        print(f"FEISHU_WS_ENDPOINT_FAILED type={type(exc).__name__} seconds={time.monotonic()-start:.2f}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
