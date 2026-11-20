# nano3ble

Provision an **Avalon Nano3s** over Bluetooth LE without the Avalon Family app. Wi-Fi provisioning was tested on macOS with Canaan's Linux SDK `v1.1.2-alpha`.

## Install

Use Python 3.10 or newer and a computer with Bluetooth LE. `nano3ble` uses Bleak, which supports macOS, Linux with BlueZ, and Windows. macOS may ask you to allow Bluetooth access for the app running Python. [Bleak platform requirements](https://bleak.readthedocs.io/en/latest/installation.html)

```bash
git clone https://github.com/skot/nano3ble.git
cd nano3ble
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

On Windows, create the environment with `py -m venv .venv` and run `.venv\Scripts\python.exe -m pip install .`. Use `.venv\Scripts\nano3ble.exe` for the commands below.

## Set up Wi-Fi

1. Power on the miner. On the Canaan SDK, a brief press of the recessed setup button starts provisioning if it is inactive.
2. Run `nano3ble scan` and find the miner's name. It looks like `nan3s_aff2`.
3. Scan for networks and set the credentials. The password prompt does not echo your input.

```bash
nano3ble --device nan3s_aff2 status
nano3ble --device nan3s_aff2 wifi-scan
nano3ble --device nan3s_aff2 set-wifi --ssid "MyNetwork"
```

`set-wifi` waits for the miner to report a connection and an IP address. WPA2-PSK is the default. Use `--auth 1` after `set-wifi` for an open network, or use `wifi-scan` to identify another supported authentication type. For enterprise Wi-Fi, supply an identity with `--identity`. Run `nano3ble set-wifi --help` for all options.

Place global options like `--device` and `--timeout` before the command. The default Bluetooth scan and connection timeout is 30 seconds. If a connection times out, confirm the miner is in setup mode and try a longer `--timeout`.

## Open the miner's web interface

```bash
nano3ble dashboard 192.168.1.175
nano3ble --device nan3s_aff2 dashboard
nano3ble dashboard --scheme http 192.168.1.175
```

The command opens the miner's normal login page. It uses HTTPS by default for the Canaan Linux SDK. Use `--scheme http` for older firmware that serves HTTP. When Bluetooth provisioning is inactive, provide the IP shown on the miner's display.

## BLE protocol

The Nano3s exposes GATT service `FFFF` with three provisioning characteristics.

| UUID | Direction | Purpose |
| --- | --- | --- |
| `FFE1` | Read | Wi-Fi scan results |
| `FFE2` | Write | Commands |
| `FFE3` | Read | Wi-Fi status as JSON |

Commands are terminated with `$$$$`. The client splits writes to fit the negotiated BLE payload size and keeps a 512-byte command limit for older firmware. The Canaan Linux SDK allocates a 1,024-byte receive buffer. The SSID, password, and identity are encoded as hexadecimal. The SDK also returns the status SSID as hexadecimal.

```text
scan$$$$
setssid,<hex_ssid>,<hex_password>,<hex_identity>,<auth>$$$$
```

For WPA-PSK, the identity defaults to `none`. The status codes are `0` disconnected, `1` connecting, `2` connected, and `3` failed.

## Development

```bash
python -m pip install -e .
python -m unittest discover -s tests
```

The unit tests use simulated BLE devices and run without a miner.

## License

GPL-3.0-or-later
