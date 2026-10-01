"""IP-address helpers shared across the deployment-status and
notification services.
"""

from ipaddress import ip_address
from typing import Literal


def ip_version(address: str) -> Literal[4, 6] | None:
    """Return 4 or 6 for a valid IPv4/IPv6 literal, else None."""
    try:
        return ip_address(address).version
    except ValueError:
        return None
