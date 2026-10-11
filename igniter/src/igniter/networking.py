import asyncio
from typing import TYPE_CHECKING

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
        "route",
        "show",
        "default",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        msg: str = f"Failed to detect default interface: {stderr.decode().strip()}"
        raise RuntimeError(msg)

    # Output format is typically: "default via 192.168.1.1 dev enp40s0 proto dhcp ..."
    parts: list[str] = stdout.decode().split()
    if "dev" in parts:
        return parts[parts.index("dev") + 1]

    msg = "Could not determine default network interface from routing table."
    raise RuntimeError(msg)


def generate_mac_from_ip(guest_ip: ipaddress.IPv4Address) -> str:
    """Generate a Firecracker-compatible MAC address from a guest IPv4 address.

    Firecracker CI Ubuntu rootfs uses fcnet-setup.sh which expects:
    06:00:<hex-ip-byte-1>:<hex-ip-byte-2>:<hex-ip-byte-3>:<hex-ip-byte-4>
    """
    hex_ip: str = ":".join(f"{b:02x}" for b in guest_ip.packed)
    return f"06:00:{hex_ip}"
