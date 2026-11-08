import asyncio
import json
from typing import TYPE_CHECKING
from typing import Any

from igniter.nftables import nft

if TYPE_CHECKING:
    import ipaddress
    from asyncio.subprocess import Process


async def get_default_host_interface() -> str:
    """Find the host network interface used for default outbound traffic.

    Raises:
        RuntimeError: If we fail to detect the default interface.
    """
    proc: Process = await asyncio.create_subprocess_exec(
        "ip",
        "-4",
        "--json",
        "route",
        "get",
        "8.8.8.8",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        msg: str = f"Failed to detect default interface: {stderr.decode().strip()}"
        raise RuntimeError(msg)

    routes: list[dict[str, Any]] = json.loads(stdout)

    for route in routes:
        dev = route.get("dev")
        if isinstance(dev, str):
            return dev

    msg = "Could not determine default network interface from routing table."
    raise RuntimeError(msg)


def generate_mac_from_ip(guest_ip: ipaddress.IPv4Address) -> str:
    """Generate a Firecracker-compatible MAC address from a guest IPv4 address.

    Firecracker CI Ubuntu rootfs uses fcnet-setup.sh which expects:
    06:00:<hex-ip-byte-1>:<hex-ip-byte-2>:<hex-ip-byte-3>:<hex-ip-byte-4>
    """
    hex_ip: str = ":".join(f"{b:02x}" for b in guest_ip.packed)
    return f"06:00:{hex_ip}"


async def delete_tap(vm_id: int) -> str:
    """Delete a TAP device for a given VM ID.

    Raises:
        RuntimeError: If the TAP device deletion fails.
    """
    tap_name: str = f"tap_{vm_id:04d}_01"  # TODO(TheLovinator): Get the actual TAP device name
    proc: Process = await asyncio.create_subprocess_exec(
        "ip",
        "link",
        "delete",
        tap_name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        msg: str = f"Failed to delete TAP device {tap_name}: {stderr.decode().strip()}"
        raise RuntimeError(msg)

    return tap_name


async def teardown_firewall(vm_id: int) -> None:
    """Remove nftables rules for a given VM ID."""
    tap_name: str = f"tap_{vm_id:04d}_01"  # TODO(TheLovinator): Get the actual TAP device name
    await nft("delete", "rule", "ip", "tussilago", "postrouting", "oifname", tap_name, "counter", "accept")
