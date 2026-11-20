"""Open the miner's web interface in the default browser."""

import ipaddress
import webbrowser


def open_dashboard(ip: str, *, scheme: str = "https") -> bool:
    """Open the miner's login page for an IPv4 or IPv6 address."""
    if scheme not in ("http", "https"):
        raise ValueError("Dashboard scheme must be http or https.")
    address = ipaddress.ip_address(ip)
    host = f"[{address}]" if address.version == 6 else str(address)
    url = f"{scheme}://{host}/"
    print(f"Opening {url} in your browser…")
    return webbrowser.open(url)
