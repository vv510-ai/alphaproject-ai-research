"""Local interface discovery, used by the app and its launcher."""

from ipaddress import IPv4Address, IPv4Network
import socket

import psutil

PRIVATE_NETWORKS = tuple(IPv4Network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"
))


def lan_addresses() -> list[str]:
    found = set()
    states = psutil.net_if_stats()
    for name, addresses in psutil.net_if_addrs().items():
        if name in states and not states[name].isup:
            continue
        for item in addresses:
            if item.family == socket.AF_INET:
                ip = IPv4Address(item.address)
                if any(ip in network for network in PRIVATE_NETWORKS):
                    found.add(str(ip))
    return sorted(found)
