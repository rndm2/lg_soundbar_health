"""Offline regression tests; no Home Assistant service or physical device calls."""
import ast
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import importlib.util
import logging
from pathlib import Path
import socket
import threading
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "lg_soundbar_health"
spec = importlib.util.spec_from_file_location("parent_connection", ROOT / "parent_connection.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)
Probe = adapter.ParentConnectionProbe

# Execute the actual state and coordinator methods, with only HA framework
# boundaries substituted. No hand-copied recovery logic in these tests.
tree = ast.parse((ROOT / "__init__.py").read_text())
selected = []
for node in tree.body:
    if isinstance(node, ast.ClassDef) and node.name in (
        "SoundbarTarget", "HealthState", "LGSoundbarHealthCoordinator"
    ):
        node.bases = []
        selected.append(node)
ns = dict(__name__=__name__, callback=lambda fn: fn, asyncio=asyncio, dataclass=dataclass, datetime=datetime, UTC=UTC, Any=Any,
          DEFAULT_AUTO_RELOAD_INITIAL_FAILURES=3,
          DEFAULT_PARENT_RELOAD_COOLDOWN=timedelta(minutes=10),
          ParentConnectionProbe=Probe, DATA_INSTANCES="entity_components",
          ConfigEntryState=SimpleNamespace(LOADED="loaded"),
          _LOGGER=logging.getLogger("test"))
exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)] + selected, type_ignores=[])), "actual_coordinator", "exec"), ns)
State = ns["HealthState"]
Coordinator = ns["LGSoundbarHealthCoordinator"]


def state():
    return State(ns["SoundbarTarget"]("entry", "LG", "lg.local", 9741),
                 connected=True, auto_reload_enabled=True)


class RecoveryTests(unittest.TestCase):
    def test_same_ip_three_failures_reloads(self):
        s = state()
        s.initial_ip = s.resolved_ip = "10.0.0.1"
        s.parent_connected = False
        for count in (0, 1, 2):
            s.parent_failure_count = count
            self.assertFalse(s.auto_reload_ready)
        s.parent_failure_count = 3
        self.assertTrue(s.auto_reload_ready)
        self.assertEqual(s.auto_reload_reason, "auto_parent_unresponsive")

    def test_network_down_disabled_in_progress_do_not_reload(self):
        for field, value in (("connected", False), ("auto_reload_enabled", False),
                             ("parent_reload_in_progress", True)):
            s = state()
            s.parent_connected = False
            s.parent_failure_count = 3
            setattr(s, field, value)
            self.assertFalse(s.auto_reload_ready)

    def test_healthy_or_unknown_parent_does_not_reload(self):
        for connected in (True, None):
            s = state()
            s.parent_connected = connected
            s.parent_failure_count = 3
            self.assertFalse(s.auto_reload_ready)

    def test_existing_ip_recovery_preserved(self):
        s = state()
        s.initial_ip, s.resolved_ip = "10.0.0.1", "10.0.0.2"
        s.initial_ip_connected = False
        s.initial_ip_failure_count = 3
        self.assertTrue(s.auto_reload_ready)
        self.assertEqual(s.auto_reload_reason, "auto_ip_changed")


class HeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client, self.server = socket.socketpair()
        self.server.setblocking(False)
        self.received = []
        self.original = self.received.append
        self.device = SimpleNamespace(socket=self.client,
            callback=self.original, encrypt_packet=lambda data: data.encode(),
            thread=SimpleNamespace(is_alive=lambda: True))
        self.probe = Probe(self.device)

    async def asyncTearDown(self):
        self.probe.close()
        self.client.close()
        self.server.close()

    async def reply(self, data=None):
        packet = await asyncio.get_running_loop().sock_recv(self.server, 1024)
        self.assertEqual(packet, b'{"cmd":"get","msg":"SPK_LIST_VIEW_INFO"}')
        self.device.callback(data or {"msg":"SPK_LIST_VIEW_INFO", "data":{"b_powerstatus":False}})

    async def test_off_is_healthy_and_native_callback_preserved(self):
        task = asyncio.create_task(self.reply())
        self.assertEqual(await self.probe.check(.1), (True, None))
        await task
        self.assertEqual(self.received[0]["data"]["b_powerstatus"], False)
        self.assertIsNone(self.client.gettimeout())

    async def test_real_native_thread_callback(self):
        async def reply_thread():
            await asyncio.get_running_loop().sock_recv(self.server, 1024)
            t = threading.Thread(target=self.device.callback, args=({"msg":"SPK_LIST_VIEW_INFO", "data":{"i_vol":4}},))
            t.start()
            t.join()
        task = asyncio.create_task(reply_thread())
        self.assertTrue((await self.probe.check(.1))[0])
        await task

    async def test_open_socket_without_reply_fails(self):
        self.assertFalse((await self.probe.check(.02))[0])

    async def test_unrelated_reply_does_not_pass(self):
        task = asyncio.create_task(self.reply({"msg":"PLAY_INFO", "data":{"i_play_ctrl":0}}))
        self.assertFalse((await self.probe.check(.02))[0])
        await task

    async def test_old_reply_does_not_pass(self):
        self.device.callback({"msg":"SPK_LIST_VIEW_INFO", "data":{"b_powerstatus":False}})
        self.assertFalse((await self.probe.check(.02))[0])

    async def test_dead_thread_fails_without_query(self):
        self.device.thread.is_alive = lambda: False
        result, error = await self.probe.check(.02)
        self.assertFalse(result)
        self.assertIn("thread", error)

    async def test_broken_socket_fails(self):
        self.server.close()
        self.assertFalse((await self.probe.check(.02))[0])

    async def test_cleanup_restores_callback(self):
        self.probe.close()
        self.assertIs(self.device.callback, self.original)
        self.assertFalse((await self.probe.check(.02))[0])

    async def test_adapter_guard(self):
        self.assertTrue(Probe.supported(self.device))
        self.assertFalse(Probe.supported(SimpleNamespace()))

    async def test_cooldown_and_reload_reason(self):
        c = object.__new__(Coordinator)
        tasks = []
        c.hass = SimpleNamespace(async_create_task=lambda coro: tasks.append(asyncio.create_task(coro)) or tasks[-1])
        c._parent_reload_tasks = set()
        c.async_reload_parent = AsyncMock()
        s = state()
        s.parent_connected, s.parent_failure_count = False, 3
        s.last_parent_reload = datetime.now(UTC)
        c._maybe_schedule_auto_reload(s)
        self.assertEqual(tasks, [])
        s.last_parent_reload -= timedelta(minutes=11)
        c._maybe_schedule_auto_reload(s)
        await asyncio.gather(*tasks)
        c.async_reload_parent.assert_awaited_once_with("entry", reason="auto_parent_unresponsive")

    async def test_coordinator_failure_reset_and_reply(self):
        self.probe.close()
        c = object.__new__(Coordinator)
        c._parent_probes = {}
        c._get_source_entry = lambda _: SimpleNamespace(state="loaded")
        registered = SimpleNamespace(domain="media_player", disabled_by=None, entity_id="media_player.lg")
        ns["er"] = SimpleNamespace(async_get=lambda _: None, async_entries_for_config_entry=lambda *_: [registered])
        component = SimpleNamespace(get_entity=lambda _: SimpleNamespace(_device=self.device))
        c.hass = SimpleNamespace(data={"entity_components":{"media_player":component}})
        s = state()
        self.device.thread.is_alive = lambda: False
        for i in range(3):
            await c._check_parent_connection(s)
            self.assertEqual(s.parent_failure_count, i+1)
        self.assertTrue(s.auto_reload_ready)
        s.connected = False
        await c._check_parent_connection(s)
        self.assertEqual(s.parent_failure_count, 0)
        self.assertIsNone(s.parent_connected)
        s.connected = True
        self.device.thread.is_alive = lambda: True
        task = asyncio.create_task(self.reply())
        await c._check_parent_connection(s)
        await task
        self.assertTrue(s.parent_connected)
        self.assertIsNotNone(s.parent_last_success)
        c._close_parent_probe("entry")
        self.assertIs(self.device.callback, self.original)

    async def test_unknown_adapter_skips_recovery(self):
        c = object.__new__(Coordinator)
        c._parent_probes = {}
        c._get_source_entry = lambda _: SimpleNamespace(state="loaded")
        ns["er"] = SimpleNamespace(async_get=lambda _: None, async_entries_for_config_entry=lambda *_: [])
        c.hass = SimpleNamespace(data={})
        s = state()
        s.parent_failure_count = 3
        await c._check_parent_connection(s)
        self.assertIsNone(s.parent_connected)
        self.assertEqual(s.parent_failure_count, 0)
        self.assertFalse(s.auto_reload_ready)

    async def test_unloaded_source_skips_probe(self):
        c = object.__new__(Coordinator)
        c._parent_probes = {}
        c._get_source_entry = lambda _: SimpleNamespace(state="unloading")
        s = state()
        s.parent_failure_count = 3
        await c._check_parent_connection(s)
        self.assertEqual(s.parent_failure_count, 0)
        self.assertIsNone(s.parent_connected)

    async def test_shutdown_detaches_observer(self):
        c = object.__new__(Coordinator)
        c._parent_probes = {"entry":self.probe}
        c._source_state_unsub = {}
        c._parent_reload_tasks = set()
        c.async_shutdown()
        self.assertEqual(c._parent_probes, {})
        self.assertIs(self.device.callback, self.original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
