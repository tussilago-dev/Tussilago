import asyncio
import json
import logging
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from asyncio.subprocess import Process

logger: logging.Logger = logging.getLogger("tussilago")


async def nft(*args: str, input_data: bytes | None = None) -> str:
    """Execute an nft command and return stdout.

    Raises:
        RuntimeError: If the nft command exits with a non-zero code.
    """
    process: Process = await asyncio.create_subprocess_exec(
        "nft",
        *args,
        stdin=asyncio.subprocess.PIPE if input_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate(input=input_data)

    if process.returncode != 0:
        msg: str = f"nft command failed: {stderr.decode().strip()}"
        raise RuntimeError(msg)

    return stdout.decode()


async def setup_firewall(*, tap_name: str, guest_ip: str, external_interface: str) -> None:
    """Set up NAT and forwarding rules for a microVM."""
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


def _rule_matches_ip(rule: dict[str, Any], guest_ip: str) -> bool:
    """Check if an nftables rule AST matches the target guest IP on saddr or daddr."""
    expressions: list[dict[str, Any]] = rule.get("expr", [])

    for expr in expressions:
        match: dict[str, Any] | None = expr.get("match")
        if not match:
            continue

        left: dict[str, Any] = match.get("left", {})
        payload: dict[str, Any] = left.get("payload", {})

        # Verify that this expression inspects IPv4 source or destination address
        if payload.get("protocol") == "ip" and payload.get("field") in {"saddr", "daddr"}:  # ruff: ignore[collapsible-if]
            if match.get("right") == guest_ip:
                return True

    return False


async def teardown_firewall(*, guest_ip: str) -> None:
    """Atomically remove nftables rules matching this VM's guest IP using libnftables JSON.

    Raises:
        RuntimeError: If we failed to delete the nftables rule. Possibly already deleted?
    """
    try:
        raw_json: str = await nft("--json", "-a", "list", "table", "ip", "tussilago")
        data: dict[str, Any] = json.loads(raw_json)
    except RuntimeError as exc:
        if "No such file or directory" in str(exc) or "does not exist" in str(exc):
            logger.debug("Table ip tussilago does not exist; skipping teardown.")
            return
        raise

    # Collect delete commands for each matching rule handle
    delete_commands: list[dict[str, Any]] = []

    for item in data.get("nftables", []):
        rule: dict[str, Any] | None = item.get("rule")
        if not rule:
            continue

        chain: str = rule.get("chain", "")
        handle: int | None = rule.get("handle")

        if handle is not None and _rule_matches_ip(rule, guest_ip):
            delete_commands.append({
                "delete": {
                    "rule": {
                        "family": "ip",
                        "table": "tussilago",
                        "chain": chain,
                        "handle": handle,
                    }
                }
            })

    if not delete_commands:
        logger.debug("No nftables rules found for guest IP %s", guest_ip)
        return

    # Execute all deletions in a single atomic transaction via stdin
    batch_payload: dict[str, Any] = {"nftables": delete_commands}
    payload_bytes: bytes = json.dumps(batch_payload).encode("utf-8")

    await nft("-j", "-f", "-", input_data=payload_bytes)
    logger.info("Atomically deleted %d firewall rule(s) for guest IP %s", len(delete_commands), guest_ip)
