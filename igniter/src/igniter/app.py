import asyncio
import hashlib
import hmac
import io
import logging
import os
import re
import tarfile
from dataclasses import dataclass
from ipaddress import IPv4Address
from ipaddress import IPv4Interface
from pathlib import Path
from secrets import randbelow
from typing import IO
from typing import TYPE_CHECKING
from typing import Any

import httpx
import niquests
from anyio import Path as AsyncPath
from litestar import Litestar
from litestar import get
from litestar.logging import LoggingConfig
from platformdirs import site_bin_path
from platformdirs import site_cache_path
from platformdirs import site_data_path

from igniter.firecracker.images import download_linux_kernel
from igniter.firecracker.manager import VMManager

if TYPE_CHECKING:
    import pathlib
    from _hashlib import HASH

    from igniter.firecracker.models import VirtualMachine

# TODO(TheLovinator): Add support for Aarch64
# TODO(TheLovinator): Install to /opt/tussilago/firecracker/<version> and create symlinks


if os.name != "posix":
    msg = "This script is only supported on Linux systems."
    raise OSError(msg)

# Abort if not running as root
if os.geteuid() != 0:
    msg = "This script must be run as root."
    raise PermissionError(msg)


vm_manager = VMManager()

cache_location: pathlib.Path = site_cache_path(appname="Tussilago", appauthor=False, ensure_exists=True)
site_data_location: pathlib.Path = site_data_path(appname="Tussilago", appauthor=False, ensure_exists=True)
install_dir: Path = site_bin_path()

logger: logging.Logger = logging.getLogger("tussilago")


FIRECRACKER_ASSET_PATTERN: re.Pattern[str] = re.compile(r"firecracker-v(?P<version>\d+\.\d+\.\d+)-x86_64\.tgz")


@dataclass(slots=True)
class LatestFirecrackerRelease:
    version: str
    download_url: str
    digest: str


async def parse_firecracker_asset(asset: dict[str, Any]) -> LatestFirecrackerRelease | None:
    """Validate and convert the GitHub asset dictionary into LatestFirecrackerRelease."""
    # name = "firecracker-v1.17.0-x86_64.tgz"
    name: str = asset.get("name", "")
    if not name:
        return None

    match: re.Match[str] | None = FIRECRACKER_ASSET_PATTERN.fullmatch(name)
    if match is None:
        logger.debug("Skipping asset %s: does not match pattern", name)
        return None

    # download_url = "https://github.com/firecracker-microvm/firecracker/releases/download/v1.17.0/firecracker-v1.17.0-x86_64.tgz"
    download_url: str = asset.get("browser_download_url", "")
    if not download_url.startswith("https://"):
        logger.warning("Aborting as '%s' does not start with https://", download_url)
        return None

    # digest = "06094a1108ae9e82aa4c23a775aa92758f53f1175d422270d9d6162cb9ade558"
    digest: str = asset.get("digest", "")
    if not digest.startswith("sha256:"):
        logger.warning("Missing SHA-256 digest for '%s', (Digest: '%s')", name, digest)
        return None

    return LatestFirecrackerRelease(
        version=match.group("version"),
        download_url=download_url,
        digest=digest.removeprefix("sha256:"),
    )


async def fetch_latest_firecracker_release() -> LatestFirecrackerRelease | None:
    """Fetch the latest Firecracker release from GitHub.

    Returns:
        LatestFirecrackerRelease | None: Version, download URL, and expected hash.
    """
    url = "https://api.github.com/repos/firecracker-microvm/firecracker/releases/latest"
    async with httpx.AsyncClient() as client:
        r: httpx.Response = await client.get(url)
        r.raise_for_status()

    json_data: dict[str, Any] = r.json()
    assets: list[dict[str, Any]] = json_data.get("assets", [])

    for asset in assets:
        release: LatestFirecrackerRelease | None = await parse_firecracker_asset(asset=asset)
        if release:
            return release

    return None


async def download_binary() -> str | None:
    """Download the Firecracker and Jailer binary to /usr/bin/.

    Raises:
        RuntimeError: If the checksum does not match.
    """
    release: LatestFirecrackerRelease | None = await fetch_latest_firecracker_release()
    if release is None:
        return "No new version available."

    version: str = release.version
    url: str = release.download_url
    expected_hash: str = release.digest

    logger.info("Downloading Firecracker v%s from %s", version, url)

    response: niquests.AsyncResponse = await niquests.aget(url, stream=True)
    response.raise_for_status()

    tar_stream = io.BytesIO()
    hasher: HASH = hashlib.sha256()
    logger.info("Calculating checksum and writing to tar stream...")

    async for chunk in await response.iter_content(chunk_size=8192):
        if chunk:
            hasher.update(chunk)
            tar_stream.write(chunk)

    calculated_hash: str = hasher.hexdigest()
    if not hmac.compare_digest(calculated_hash, expected_hash):
        msg: str = f"Firecracker checksum mismatch: expected {expected_hash}, got {calculated_hash}"
        raise RuntimeError(msg)

    tar_stream.seek(0)

    targets: dict[str, str] = {
        f"firecracker-v{version}-x86_64": "firecracker",
        f"jailer-v{version}-x86_64": "jailer",
    }

    logger.info("Extracting binaries to %s", install_dir)
    with tarfile.open(fileobj=tar_stream, mode="r:gz") as tar:
        for member in tar.getmembers():
            base_name: str = Path(member.name).name
            if base_name in targets:
                logger.info("Found target: %s", base_name)
                target_name: str = targets[base_name]
                dest_path: pathlib.Path = install_dir / target_name

                # Extract the binary file
                src: IO[bytes] | None = tar.extractfile(member)
                if src:
                    dest_path.write_bytes(src.read())
                    dest_path.chmod(0o755)
                    logger.info("Installed %s -> %s", target_name, dest_path)
                else:
                    logger.warning("Failed to extract %s", base_name)

    # Append metadata to the installed binaries
    for target_name in targets.values():
        dest_path: pathlib.Path = install_dir / target_name
        metadata_path: pathlib.Path = dest_path.with_suffix(".metadata")
        metadata_content: str = f"version={version}\n"
        metadata_path.write_text(metadata_content, encoding="utf-8")
        logger.info("Wrote metadata to %s", metadata_path)

    return f"Firecracker v{version} installed to {install_dir}"


@get("/")
async def index() -> str:
    """Return a friendly greeting."""
    return "Hello, world!"


@get("/firecracker/install")
async def firecracker_install() -> str:
    """Install Firecracker to /opt/tussilago/firecracker/<version>."""
    downloaded_binary: str | None = await download_binary()
    if not downloaded_binary:
        return "No new version available."

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
