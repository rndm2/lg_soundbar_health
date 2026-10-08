"""Heartbeat through the native LG connection, without changing device settings.

The native integration currently exposes no connection-health API. Keep the
temescal-specific adapter here and fail closed if that interface changes.
"""

from __future__ import annotations

import asyncio
import socket
import time
from typing import Any


class ParentConnectionProbe:
    """Observe replies processed by the native integration's own listener."""

    def __init__(self, device: Any) -> None:
        self.device = device
        self._loop = asyncio.get_running_loop()
        self._reply = asyncio.Event()
        self._closed = False
        self._request_started: float | None = None
        self._original_callback = device.callback

        def callback(response: Any) -> None:
            received_at = time.monotonic()
            # Preserve native processing, including its exceptions.
            self._original_callback(response)
            if (
                not self._closed
                and isinstance(response, dict)
                and response.get("msg") == "SPK_LIST_VIEW_INFO"
                and isinstance(response.get("data"), dict)
                and response["data"]
            ):
                self._loop.call_soon_threadsafe(self._record_reply, received_at)

        self._callback = callback
        device.callback = callback

    @staticmethod
    def supported(device: Any) -> bool:
        """Do not attempt recovery against an unknown native implementation."""
        return (
            callable(getattr(device, "callback", None))
            and callable(getattr(device, "encrypt_packet", None))
            and isinstance(getattr(device, "socket", None), socket.socket)
            and callable(getattr(getattr(device, "thread", None), "is_alive", None))
            and hasattr(socket, "MSG_DONTWAIT")
        )

    def _record_reply(self, received_at: float) -> None:
        if (
            not self._closed
            and self._request_started is not None
            and received_at >= self._request_started
        ):
            self._reply.set()

    def _send_query(self) -> None:
        # temescal.send_packet() can block in connect() and swallow errors.
        # Send only a read query on the existing socket, without reconnecting or
        # changing its timeout (the native reader shares this socket).
        packet = self.device.encrypt_packet(
            '{"cmd":"get","msg":"SPK_LIST_VIEW_INFO"}'
        )
        sent = self.device.socket.send(packet, socket.MSG_DONTWAIT)
        if sent != len(packet):
            raise OSError("Incomplete native LG heartbeat send")

    async def check(self, timeout: float = 5.0) -> tuple[bool, str | None]:
        if self._closed:
            return False, "Native LG heartbeat observer was closed"
        if not self.device.thread.is_alive():
            return False, "Native LG receive thread is not running"
        self._reply.clear()
        self._request_started = time.monotonic()
        try:
            await self._loop.run_in_executor(None, self._send_query)
            await asyncio.wait_for(self._reply.wait(), timeout)
        except TimeoutError:
            return False, "No native LG status reply within heartbeat timeout"
        except OSError as err:
            return False, f"Native LG heartbeat send failed: {err}"
        finally:
            self._request_started = None
        return True, None

    def close(self) -> None:
        self._closed = True
        if self.device.callback is self._callback:
            self.device.callback = self._original_callback
