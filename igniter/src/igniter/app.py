import json
import logging
import os
from ipaddress import IPv4Address
from ipaddress import IPv4Interface
from typing import TYPE_CHECKING

from litestar import Litestar
from litestar import delete
from litestar import get
from litestar import post
from litestar.logging import LoggingConfig
from litestar.status_codes import HTTP_200_OK
from pydantic import BaseModel
from pydantic import Field

from igniter.firecracker.images import download_linux_kernel
from igniter.firecracker.install import LatestFirecrackerRelease
from igniter.firecracker.install import add_metadata
from igniter.firecracker.install import download_binary
from igniter.firecracker.install import fetch_latest_firecracker_release
from igniter.firecracker.manager import VMManager
from igniter.firecracker.paths import get_console_log
from igniter.firecracker.paths import get_jail_config

if TYPE_CHECKING:
    from anyio import Path as AsyncPath

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


class CreateVMRequest(BaseModel):
    vm_id: int = Field(..., ge=1, le=9999)
    memory_mib: int = Field(default=512, ge=128)
    vcpus: int = Field(default=1, ge=1, le=32)
    disk_size_gib: int = Field(default=4, ge=2, le=100)


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


@post("/vms")
async def create_vm(data: CreateVMRequest) -> dict[str, str]:
    """Create a VM with a random ID."""
    # alloc: AllocatedNetwork = await ipam.allocate(data.vm_id)

    host_ip = IPv4Interface(f"192.168.100.{data.vm_id}/24")
    guest_ip = IPv4Address(f"192.168.100.{data.vm_id + 100}")

    try:
        vm: int = await vm_manager.create(
            vm_id=data.vm_id,
            memory_mib=data.memory_mib,
            vcpus=data.vcpus,
            disk_size_gib=data.disk_size_gib,
            host_ip=host_ip,
            guest_ip=guest_ip,
        )
    except BaseException:  # ruff: ignore[useless-try-except]
        # await ipam.release(data.vm_id)
        raise

    return {
        "id": str(vm),
        "host_ip": str(host_ip),
        "guest_ip": str(guest_ip),
        "tap_device": f"tap_{data.vm_id:04d}_01",  # TODO(TheLovinator): Get the actual TAP device name
        "unit_name": f"tussilago-vm-{data.vm_id}.service",
    }


@get("/vms/{vm_id:int}")
async def get_vm_status(vm_id: int) -> dict[str, str | int]:
    """Check VM unit status and retrieve console output."""
    running_units: list[str] = await vm_manager.list_running()
    unit_name: str = f"tussilago-vm-{vm_id}.service"
    is_running: bool = unit_name in running_units

    console_log: AsyncPath = get_console_log(vm_id)
    logs = ""
    if await console_log.exists():
        logs: str = await console_log.read_text(encoding="utf-8")
        logs: str = "\n".join(logs.splitlines()[-20:])

    # Get configuration from disk if it exists, otherwise return empty values.
    config_path: AsyncPath = get_jail_config(vm_id)
    if await config_path.exists():
        config_data: str = await config_path.read_text(encoding="utf-8")
        return {
            "id": vm_id,
            "running": is_running,
            "unit_name": unit_name,
            "latest_logs": logs,
            "config": json.loads(config_data),
        }

    return {
        "id": vm_id,
        "running": is_running,
        "unit_name": unit_name,
        "latest_logs": logs,
    }


@delete("/vms/{vm_id:int}", status_code=HTTP_200_OK)
async def delete_vm(vm_id: int) -> dict[str, str]:
    """Stop and destroy a MicroVM and release network resources."""
    await vm_manager.destroy(vm_id)
    # await ipam.release(vm_id)

    return {"status": "deleted", "id": str(vm_id)}


@get("/favicon.ico")
async def favicon() -> str:
    """Return the favicon."""
    return "data:image/x-icon;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQEAYAAABPYyMiAAAABmJLR0T///////8JWPfcAAAACXBIWXMAAABIAAAASABGyWs+AAAAF0lEQVRIx2NgGAWjYBSMglEwCkbBSAcACBAAAeaR9cIAAAAASUVORK5CYII="  # ruff: ignore[line-too-long]


logging_config = LoggingConfig(
    root={"level": "INFO", "handlers": ["queue_listener"]},
    formatters={"standard": {"format": "%(name)s - %(levelname)s - %(message)s"}},
    log_exceptions="always",
)


async def on_startup() -> None:
    """Download rootfs and kernel if they don't exist, and initialize the VM manager."""
    await vm_manager.initialize()


app = Litestar(
    route_handlers=[
        index,
        firecracker_install,
        firecracker_latest_kernel,
        favicon,
        create_vm,
        delete_vm,
        get_vm_status,
    ],
    on_startup=[on_startup],
    logging_config=logging_config,
)

if app.debug:
    os.environ["TUSSILAGO_DEBUG"] = "1"
