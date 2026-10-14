import asyncio
import logging
import os
from ipaddress import IPv4Address
from ipaddress import IPv4Interface
from secrets import randbelow
from typing import TYPE_CHECKING

from anyio import Path as AsyncPath
from litestar import Litestar
from litestar import get
from litestar.logging import LoggingConfig

from igniter.firecracker.images import download_linux_kernel
from igniter.firecracker.install import LatestFirecrackerRelease
from igniter.firecracker.install import add_metadata
from igniter.firecracker.install import download_binary
from igniter.firecracker.install import fetch_latest_firecracker_release
from igniter.firecracker.manager import VMManager

if TYPE_CHECKING:
    from igniter.firecracker.models import VirtualMachine

# TODO(TheLovinator): Add support for Aarch64
# TODO(TheLovinator): Install to /opt/tussilago/firecracker/<version> and create symlinks


logger: logging.Logger = logging.getLogger("tussilago")

if os.name != "posix":
    msg = "This script is only supported on Linux systems."
    raise OSError(msg)

# Abort if not running as root
if os.geteuid() != 0:
    msg = "This script must be run as root."
    raise PermissionError(msg)


vm_manager = VMManager()


@get("/")
async def index() -> str:
    """Return a friendly greeting."""
    return "Hello, world!"


@get("/firecracker/install")
async def firecracker_install() -> str:
    """Install Firecracker to /opt/tussilago/firecracker/<version>."""
    latest_release: LatestFirecrackerRelease | None = await fetch_latest_firecracker_release()
    if latest_release is None:
        return "No new version available."

    version: str = latest_release.version
    url: str = latest_release.download_url
    expected_hash: str = latest_release.digest

    targets: dict[str, str] = {
        f"firecracker-v{version}-x86_64": "firecracker",
        f"jailer-v{version}-x86_64": "jailer",
    }

    downloaded_binary: str | None = await download_binary(
        targets=targets,
        version=version,
        url=url,
        expected_hash=expected_hash,
    )
    if not downloaded_binary:
        return "No new version available."

    await add_metadata(targets=targets, version=version)

    return downloaded_binary


@get("/firecracker/latest-kernel")
async def firecracker_latest_kernel() -> None:
    """Fetch the latest Firecracker kernel from S3."""
    await download_linux_kernel()


@get("/create")
async def create_vm() -> dict[str, str]:
    """Create a VM with a random ID."""
    vm: VirtualMachine = await vm_manager.create(
        vm_id=randbelow(1000) + 1,
        rootfs=AsyncPath("/var/lib/tussilago/rootfs/ubuntu-24.04.ext4"),
        memory_mib=512,
        vcpus=1,
        host_ip=IPv4Interface("172.16.0.1/30"),
        guest_ip=IPv4Address("172.16.0.2"),
    )

    await asyncio.sleep(1)

    return {
        "id": str(vm.id),
        "guest_ip": str(vm.guest_ip),
        "tap_device": vm.tap_device,
    }


@get("/running")
async def running_vms() -> list[str]:
    """List running VMs."""
    vms: list[str] = await vm_manager.list_running()
    return vms


@get("/favicon.ico")
async def favicon() -> str:
    """Return the favicon."""
    return "data:image/x-icon;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQEAYAAABPYyMiAAAABmJLR0T///////8JWPfcAAAACXBIWXMAAABIAAAASABGyWs+AAAAF0lEQVRIx2NgGAWjYBSMglEwCkbBSAcACBAAAeaR9cIAAAAASUVORK5CYII="  # ruff: ignore[line-too-long]


logging_config = LoggingConfig(
    root={"level": "INFO", "handlers": ["queue_listener"]},
    formatters={"standard": {"format": "%(name)s - %(levelname)s - %(message)s"}},
    log_exceptions="always",
)


app = Litestar(
    [index, firecracker_install, firecracker_latest_kernel, favicon, create_vm, running_vms],
    logging_config=logging_config,
)
