import hashlib
import hmac
import io
import logging
import os
import re
import tarfile
from pathlib import Path
from typing import IO
from typing import TYPE_CHECKING
from typing import Any

import niquests
from litestar import Litestar
from litestar import get
from litestar.logging import LoggingConfig
from platformdirs import site_bin_path
from platformdirs import site_cache_path
from platformdirs import site_data_path

if TYPE_CHECKING:
    from _hashlib import HASH

# TODO(TheLovinator): Add support for Aarch64


if os.name != "posix":
    msg = "This script is only supported on Linux systems."
    raise OSError(msg)

# Abort if not running as root
if os.geteuid() != 0:
    msg = "This script must be run as root."
    raise PermissionError(msg)

cache_location: Path = site_cache_path(appname="Tussilago", appauthor=False, ensure_exists=True)
site_data_location: Path = site_data_path(appname="Tussilago", appauthor=False, ensure_exists=True)
install_dir: Path = site_bin_path()

logger: logging.Logger = logging.getLogger("tussilago")


def fetch_latest_firecracker_release() -> tuple[str, str, str] | None:
    """Fetch the latest Firecracker release from GitHub.

    Returns:
        tuple[str, str, str] | None: Version, download URL, and expected hash.
    """
    url = "https://api.github.com/repos/firecracker-microvm/firecracker/releases/latest"
    r: niquests.Response = niquests.get(url)
    r.raise_for_status()

    json_data: dict[str, Any] = r.json()
    assets: list[dict[str, Any]] = json_data.get("assets", [])

    for asset in assets:
        name: str = asset.get("name", "")
        if not name:
            continue

        pattern = r"firecracker-v(\d+\.\d+\.\d+)-x86_64\.tgz"
        match: re.Match[str] | None = re.fullmatch(pattern=pattern, string=name)
        if not match:
            logger.debug("Skipping asset %s: does not match pattern", name)
            continue

        version = str(match.group(1))
        download_url = str(asset.get("browser_download_url", ""))
        expected_hash: str = str(asset.get("digest", "")).replace("sha256:", "")

        return version, download_url, expected_hash
    return None


def download_binary() -> str:
    """Download the Firecracker and Jailer binary to /usr/bin/.

    Raises:
        RuntimeError: If the checksum does not match.
    """
    release: tuple[str, str, str] | None = fetch_latest_firecracker_release()
    if release is None:
        return "No new version available."

    version, url, expected_hash = release
    logger.info("Downloading Firecracker v%s from %s", version, url)

    response: niquests.Response = niquests.get(url, stream=True)
    response.raise_for_status()

    tar_stream = io.BytesIO()
    hasher: HASH = hashlib.sha256()
    logger.info("Calculating checksum and writing to tar stream...")

    for chunk in response.iter_content(1024 * 1024):
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
                dest_path: Path = install_dir / target_name

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
        dest_path: Path = install_dir / target_name
        metadata_path: Path = dest_path.with_suffix(".metadata")
        metadata_content: str = f"version={version}\n"
        metadata_path.write_text(metadata_content, encoding="utf-8")
        logger.info("Wrote metadata to %s", metadata_path)

    return f"Firecracker v{version} installed to {install_dir}"


def get_tar(response: niquests.Response) -> io.BytesIO:
    """Get tar content.

    Args:
        response (niquests.Response): The request.

    Returns:
        io.BytesIO: Return the tar.
    """
    tar_stream = io.BytesIO()
    for chunk in response.iter_content(chunk_size=1024 * 1024):
        if chunk:
            tar_stream.write(chunk)
    tar_stream.seek(0)
    return tar_stream


@get("/")
async def index() -> str:
    """Return a friendly greeting."""
    return "Hello, world!"


@get("/firecracker/install")
async def firecracker_install() -> str:
    """Install Firecracker to /opt/tussilago/firecracker/<version>."""
    return str(download_binary() or "No new version available.")


logging_config = LoggingConfig(
    root={"level": "INFO", "handlers": ["queue_listener"]},
    formatters={"standard": {"format": "%(name)s - %(levelname)s - %(message)s"}},
    log_exceptions="always",
)


app = Litestar([index, firecracker_install])
