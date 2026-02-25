"""Command-line interface for nano3ble.

Installed as the ``nano3ble`` console script.

Examples::

    nano3ble scan
    nano3ble status
    nano3ble wifi-scan
    nano3ble set-wifi --ssid "MyNetwork" --password "hunter2"
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import __version__
from .device import DeviceStatus, Nano3s, scan_devices


# ── Logging ───────────────────────────────────────────────────────────────


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )
    if not verbose:
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
            password=args.password,
            auth=args.auth,
            identity=args.identity,
        )
        print(f"\n{status}")

        if status.is_connected:
            print(f"\n✓ Connected!  Miner IP: {status.ip}")
            return 0
        print(f"\n✗ Connection {status.conn_state_name.lower()}.")
        return 1


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
        "-t", "--timeout", type=float, default=15.0,
        help="BLE scan / connect timeout in seconds (default: 15)",
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
    sw.add_argument("--password", required=True, help="WiFi password")
    sw.add_argument(
        "--auth", type=int, default=7,
        help="Auth type (default: 7 = WPA2-PSK; use wifi-scan to check)",
    )
    sw.add_argument(
        "--identity", default="none",
        help="EAP identity (default: 'none' for WPA-PSK)",
    )

    return p


# ── Entry point ───────────────────────────────────────────────────────────


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    _setup_logging(args.verbose)

    handlers = {
        "scan": _cmd_scan,
        "status": _cmd_status,
        "wifi-scan": _cmd_wifi_scan,
        "set-wifi": _cmd_set_wifi,
    }

    try:
        rc = asyncio.run(handlers[args.command](args))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        rc = 130
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        rc = 1

    raise SystemExit(rc)
