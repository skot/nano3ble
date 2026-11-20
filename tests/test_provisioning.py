import logging
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from nano3ble.cli import _build_parser, _cmd_set_wifi, _setup_logging, main
from nano3ble.dashboard import open_dashboard
from nano3ble.device import DEFAULT_TIMEOUT, DeviceStatus, Nano3s
from nano3ble.protocol import CHAR_COMMAND, build_setssid_command


class ProvisioningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.device = SimpleNamespace(name="nan3s_test", address="test-address")
        self.client = Mock(mtu_size=23)
        self.client.write_gatt_char = AsyncMock()
        self.client.read_gatt_char = AsyncMock()
        self.characteristic = Mock(properties=["write"])
        self.client.services.get_characteristic.return_value = self.characteristic
        self.miner = Nano3s(self.client, self.device)

    async def test_connection_uses_advertised_name_and_default_timeout(self):
        async def find_device(predicate, *, timeout):
            self.assertEqual(timeout, DEFAULT_TIMEOUT)
            advertisement = SimpleNamespace(local_name="nan3s_TEST")
            cached_device = SimpleNamespace(name="Old name", address="test-address")
            self.assertTrue(predicate(cached_device, advertisement))
            self.assertFalse(predicate(self.device, SimpleNamespace(local_name="nan3s_other")))
            return cached_device

        with patch("nano3ble.device.BleakScanner.find_device_by_filter", side_effect=find_device), \
             patch("nano3ble.device.BleakClient") as client_factory:
            client_factory.return_value.connect = AsyncMock()
            await Nano3s.connect(device_name="nan3s_test")
            client_factory.return_value.connect.assert_awaited_once()

    async def test_sdk_status_decodes_hex_ssid(self):
        self.client.read_gatt_char.return_value = (
            b'{"device":"nan3s_test","ip":"192.0.2.2","ssid":"4c61622057694669",'
            b'"auth":7,"conn_state":2}\x00'
        )
        status = await self.miner.get_status()
        self.assertEqual(status.ssid, "Lab WiFi")
        self.assertEqual(status.raw["ssid"], "4c61622057694669")
        self.assertTrue(status.has_ip)

    async def test_invalid_status_is_reported(self):
        self.client.read_gatt_char.return_value = b'{"conn_state":2,"ssid":null}'
        with self.assertRaisesRegex(RuntimeError, "invalid Wi-Fi status"):
            await self.miner.get_status()

    async def test_long_command_is_written_in_att_chunks(self):
        command = build_setssid_command("Lab WiFi", "test password")
        await self.miner._write_command(command)

        writes = self.client.write_gatt_char.await_args_list
        self.assertGreater(len(writes), 1)
        self.assertEqual(b"".join(write.args[1] for write in writes), command)
        self.assertTrue(all(len(write.args[1]) <= 20 for write in writes))
        self.assertTrue(all(write.kwargs["response"] for write in writes))
        self.client.services.get_characteristic.assert_called_once_with(CHAR_COMMAND)

    async def test_failed_write_is_not_repeated(self):
        self.client.write_gatt_char.side_effect = TimeoutError
        with self.assertRaises(TimeoutError):
            await self.miner._write_command(b"scan$$$$")
        self.client.write_gatt_char.assert_awaited_once()

    async def test_provisioning_waits_for_an_ip(self):
        pending = DeviceStatus("nan3s_test", "0.0.0.0", "Lab", 7, 2, {})
        ready = DeviceStatus("nan3s_test", "192.0.2.2", "Lab", 7, 2, {})
        with patch.object(self.miner, "_write_command", new_callable=AsyncMock), \
             patch.object(self.miner, "get_status", side_effect=[pending, ready]), \
             patch("nano3ble.device.asyncio.sleep", new_callable=AsyncMock):
            result = await self.miner.set_wifi("Lab", "test password")
        self.assertIs(result, ready)

    async def test_cli_prompts_when_the_password_is_omitted(self):
        args = _build_parser().parse_args(["set-wifi", "--ssid", "Lab"])
        status = DeviceStatus("nan3s_test", "192.0.2.2", "Lab", 7, 2, {})
        with patch("nano3ble.cli.getpass.getpass", return_value="test password") as prompt, \
             patch("nano3ble.cli.Nano3s.connect", new_callable=AsyncMock) as connect, \
             redirect_stdout(StringIO()):
            miner = connect.return_value.__aenter__.return_value
            miner.get_status = AsyncMock(return_value=status)
            miner.set_wifi = AsyncMock(return_value=status)
            self.assertEqual(await _cmd_set_wifi(args), 0)
            prompt.assert_called_once()
            self.assertEqual(miner.set_wifi.await_args.kwargs["password"], "test password")


class CommandTests(unittest.TestCase):
    def test_verbose_mode_keeps_bleak_payload_logs_disabled(self):
        bleak_log = logging.getLogger("bleak")
        self.addCleanup(bleak_log.setLevel, bleak_log.level)
        with patch("nano3ble.cli.logging.basicConfig"):
            _setup_logging(verbose=True)
        self.assertEqual(bleak_log.level, logging.WARNING)

    def test_dashboard_opens_the_normal_login_page(self):
        with patch("nano3ble.dashboard.webbrowser.open", return_value=True) as browser, \
             redirect_stdout(StringIO()):
            self.assertTrue(open_dashboard("192.0.2.2"))
        browser.assert_called_once_with("https://192.0.2.2/")

    def test_timeout_is_reported_without_a_traceback(self):
        error = StringIO()
        with patch("sys.argv", ["nano3ble", "status"]), redirect_stderr(error), \
             patch("nano3ble.cli.Nano3s.connect", side_effect=TimeoutError):
            with self.assertRaises(SystemExit) as exc:
                main()
        self.assertEqual(exc.exception.code, 1)
        self.assertIn("Try a longer --timeout", error.getvalue())
        self.assertNotIn("Traceback", error.getvalue())
