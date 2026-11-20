"""Command-line interface for nano3ble.

Installed as the ``nano3ble`` console script.

Examples::

    nano3ble scan
    nano3ble status
    nano3ble wifi-scan
    nano3ble set-wifi --ssid "MyNetwork"
    nano3ble dashboard 192.168.1.175
    nano3ble dashboard                   # auto-discover IP via BLE
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import math
import sys
import warnings

from bleak.exc import BleakError

from . import __version__
from .dashboard import open_dashboard
from .device import DEFAULT_TIMEOUT, DeviceStatus, Nano3s, scan_devices
from .protocol import AUTH_OPEN, AUTH_WPA2_PSK


# ── Logging ───────────────────────────────────────────────────────────────


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )
    # Bleak's debug logs can include the Wi-Fi credentials in GATT writes.
    logging.getLogger("bleak").setLevel(logging.WARNING)


# ── Sub-commands ──────────────────────────────────────────────────────────


async def _cmd_scan(args: argparse.Namespace) -> int:
    devices = await scan_devices(timeout=args.timeout)
    if not devices:
        print("No Nano3s devices found.")
        print("Make sure the miner is powered on and BLE is active.")
        return 1
    print(f"Found {len(devices)} device(s):\n")
    for d in devices:
        print(f"  {d.name or '(unknown)':30s}  {d.address}")
    return 0


async def _cmd_status(args: argparse.Namespace) -> int:
    async with await Nano3s.connect(
        device_name=args.device, timeout=args.timeout
    ) as miner:
        print(f"Connected to {miner.name} ({miner.address})\n")
        print(await miner.get_status())
    return 0


async def _cmd_wifi_scan(args: argparse.Namespace) -> int:
    async with await Nano3s.connect(
        device_name=args.device, timeout=args.timeout
    ) as miner:
        print(f"Connected to {miner.name} ({miner.address})\n")
        print("Scanning for WiFi networks (takes ~8 s)…\n")
        networks = await miner.scan_wifi(wait=args.wait)
        if not networks:
            print("No networks found.")
            return 1
        print(f"{'SSID':30s}  {'Auth':<10s}  {'RSSI':>6s}")
        print("-" * 52)
        for net in networks:
            print(net)
    return 0


async def _cmd_set_wifi(args: argparse.Namespace) -> int:
    password = args.password
    if password is None:
        if args.auth == AUTH_OPEN:
            password = ""
        else:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                try:
                    password = getpass.getpass("Wi-Fi password: ")
                except (EOFError, getpass.GetPassWarning) as exc:
                    raise RuntimeError("Run set-wifi from an interactive terminal.") from exc

    async with await Nano3s.connect(
        device_name=args.device, timeout=args.timeout
    ) as miner:
        print(f"Connected to {miner.name} ({miner.address})\n")

        current = await miner.get_status()
        print(f"Current state: {current.conn_state_name}")
        if current.ssid:
            print(f"Current SSID:  {current.ssid}")
        print()

        print(f"Configuring WiFi: SSID={args.ssid!r}")
        status: DeviceStatus = await miner.set_wifi(
            ssid=args.ssid,
            password=password,
            auth=args.auth,
            identity=args.identity,
        )
        print(f"\n{status}")

        if status.is_connected and status.has_ip:
            print(f"\n✓ Connected!  Miner IP: {status.ip}")
            return 0
        if status.is_connected:
            print("\nWi-Fi connected. The miner did not report an IP address in time.")
            return 1
        print(f"\n✗ Connection {status.conn_state_name.lower()}.")
        return 1


async def _cmd_dashboard(args: argparse.Namespace) -> int:
    ip = args.ip

    # If no IP given, try to discover it via BLE status
    if ip is None:
        print("No IP specified — looking up via BLE…")
        try:
            async with await Nano3s.connect(
                device_name=args.device, timeout=args.timeout
            ) as miner:
                status = await miner.get_status()
                if not status.has_ip:
                    print("Miner is not connected to WiFi (no IP).")
                    return 1
                ip = status.ip
                print(f"Discovered miner IP: {ip}")
        except RuntimeError as exc:
            print(f"BLE discovery failed: {exc}")
            print("Provide the IP directly: nano3ble dashboard <IP>")
            return 1

    ok = open_dashboard(ip, scheme=args.scheme)
    return 0 if ok else 1


# ── Argument parsing ──────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nano3ble",
        description="Configure WiFi on an Avalon Nano3s miner over BLE.",
    )
    p.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s {__version__}"
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Debug output")
    p.add_argument(
        "-t", "--timeout", type=float, default=DEFAULT_TIMEOUT,
        help=f"BLE scan / connect timeout in seconds (default: {DEFAULT_TIMEOUT:g})",
    )
    p.add_argument(
        "-d", "--device", default=None,
        help="Target a specific device by name (e.g. 'nan3s_aff2')",
    )

    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("scan", help="Scan for nearby Nano3s BLE devices")
    sub.add_parser("status", help="Show current device status")

    ws = sub.add_parser("wifi-scan", help="List WiFi networks the miner can see")
    ws.add_argument(
        "--wait", type=float, default=8.0,
        help="Seconds to wait for scan results (default: 8)",
    )

    sw = sub.add_parser("set-wifi", help="Set WiFi credentials on the miner")
    sw.add_argument("--ssid", required=True, help="WiFi network name")
    sw.add_argument("--password", help="Wi-Fi password (prompted securely if omitted)")
    sw.add_argument(
        "--auth", type=int, default=AUTH_WPA2_PSK,
        help="Auth type (default: 7 = WPA2-PSK; use wifi-scan to check)",
    )
    sw.add_argument(
        "--identity", default="none",
        help="EAP identity (default: 'none' for WPA-PSK)",
    )

    db = sub.add_parser(
        "dashboard",
        help="Open the miner's web login page",
    )
    db.add_argument(
        "ip", nargs="?", default=None,
        help="Miner IP address (auto-discovered via BLE if omitted)",
    )
    db.add_argument(
        "--scheme", choices=("http", "https"), default="https",
        help="URL scheme (default: https; use http for older firmware)",
    )

    return p


# ── Entry point ───────────────────────────────────────────────────────────


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a positive, finite number of seconds")
    _setup_logging(args.verbose)

    handlers = {
        "scan": _cmd_scan,
        "status": _cmd_status,
        "wifi-scan": _cmd_wifi_scan,
        "set-wifi": _cmd_set_wifi,
        "dashboard": _cmd_dashboard,
    }

    try:
        rc = asyncio.run(handlers[args.command](args))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        rc = 130
    except (TimeoutError, asyncio.TimeoutError):
        print("Error: Bluetooth operation timed out. Try a longer --timeout.", file=sys.stderr)
        rc = 1
    except (BleakError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        rc = 1

    raise SystemExit(rc)
