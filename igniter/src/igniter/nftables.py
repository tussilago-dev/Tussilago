import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from asyncio.subprocess import Process


async def nft(*args: str) -> None:
    process: Process = await asyncio.create_subprocess_exec(
        "nft",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    _stdout, stderr = await process.communicate()

    if process.returncode != 0:
        msg: str = f"nft command failed: {stderr.decode().strip()}"
        raise RuntimeError(msg)


async def setup_firewall(*, tap_name: str, guest_ip: str, external_interface: str) -> None:
    await nft("add", "table", "ip", "tussilago")

    await nft(
        "add",
        "chain",
        "ip",
        "tussilago",
        "postrouting",
        "{",
        "type",
        "nat",
        "hook",
        "postrouting",
        "priority",
        "srcnat",
        ";",
        "policy",
        "accept",
        ";",
        "}",
    )

    await nft(
        "add",
        "chain",
        "ip",
        "tussilago",
        "forward",
        "{",
        "type",
        "filter",
        "hook",
        "forward",
        "priority",
        "filter",
        ";",
        "policy",
        "drop",
        ";",
        "}",
    )

    # Allow replies to connections originating from the VM.
    await nft(
        "add",
        "rule",
        "ip",
        "tussilago",
        "forward",
        "iifname",
        external_interface,
        "oifname",
        tap_name,
        "ip",
        "daddr",
        guest_ip,
        "ct",
        "state",
        "established,related",
        "accept",
    )

    # Allow the VM to access the outside network.
    await nft(
        "add",
        "rule",
        "ip",
        "tussilago",
        "forward",
        "iifname",
        tap_name,
        "oifname",
        external_interface,
        "ip",
        "saddr",
        guest_ip,
        "ct",
        "state",
        "new,established",
        "accept",
    )

    # NAT traffic leaving the host.
    await nft(
        "add",
        "rule",
        "ip",
        "tussilago",
        "postrouting",
        "ip",
        "saddr",
        guest_ip,
        "oifname",
        external_interface,
        "masquerade",
    )
