import hashlib
import hmac
import io
import logging
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO
from typing import TYPE_CHECKING
from typing import Any

import httpx
import niquests
from anyio import Path as AsyncPath

from igniter.firecracker.settings import INSTALL_DIR

if TYPE_CHECKING:
    from _hashlib import HASH

logger: logging.Logger = logging.getLogger("tussilago")


async def extract_binary_if_available(targets: dict[str, str], tar: tarfile.TarFile, member: tarfile.TarInfo) -> None:
    """Extract the binaries in targets if available.

    Args:
        targets (dict[str, str]): Firecracker and jailer name with version number.
        tar (tarfile.TarFile): The tar file we are extracting the binaries from.
        member (tarfile.TarInfo): The current file we are looking at in the tar file.
    """
    base_name: str = Path(member.name).name
    if base_name in targets:
        logger.info("Found target: %s", base_name)
        target_name: str = targets[base_name]
        binary_dest_path: AsyncPath = AsyncPath(INSTALL_DIR / target_name)

        # Extract the binary file
        src: IO[bytes] | None = tar.extractfile(member)
        if src:
            await binary_dest_path.write_bytes(src.read())
            await binary_dest_path.chmod(0o755)
            logger.info("Installed %s -> %s", target_name, binary_dest_path)
        else:
            logger.warning("Failed to extract %s", base_name)


async def download_binary(targets: dict[str, str], version: str, url: str, expected_hash: str) -> str | None:
    """Download the Firecracker and Jailer binary to /usr/bin/.

    Raises:
        RuntimeError: If the checksum does not match.
    """
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

    logger.info("Extracting binaries to %s", INSTALL_DIR)
    with tarfile.open(fileobj=tar_stream, mode="r:gz") as tar:
        for member in tar.getmembers():
            await extract_binary_if_available(targets, tar, member)

    return f"Firecracker v{version} installed to {INSTALL_DIR}"


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


async def add_metadata(targets: dict[str, str], version: str) -> None:
    """Save version besides binaries.

    Args:
        targets (dict[str, str]): Our binaries.
        version (str): The current version.
    """
    for target_name in targets.values():
        dest_path: AsyncPath = AsyncPath(INSTALL_DIR / target_name)
        metadata_path: AsyncPath = dest_path.with_suffix(".metadata")
        metadata_content: str = f"version={version}\n"
        await metadata_path.write_text(metadata_content, encoding="utf-8")
        logger.info("Wrote metadata to %s", metadata_path)


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
