import logging
import re
from typing import TYPE_CHECKING

import niquests
from natsort import natsorted

from igniter.firecracker.paths import get_kernel_dir

if TYPE_CHECKING:
    from anyio import Path as AsyncPath

logger: logging.Logger = logging.getLogger("tussilago")


async def save_kernel_to_disk(session: niquests.AsyncSession, latest_kernel_key: str) -> None:
    """Download a Firecracker vmlinux kernel image from S3 and save it locally.

    Args:
        session (niquests.AsyncSession): The HTTP session used to make requests.
        latest_kernel_key (str): S3 key pointing to the target kernel binary.
    """
    latest_kernel_download_url: str = f"https://s3.amazonaws.com/spec.ccfc.min/{latest_kernel_key}"

    http_response: niquests.Response = await session.get(latest_kernel_download_url)
    http_response.raise_for_status()
    if not http_response.content:
        logger.warning("No content received when downloading the latest vmlinux file.")
        return

    # https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/20260930-a738f18a8db0-0/x86_64/vmlinux-6.18.51
    logger.info("Downloaded vmlinux file from %s", latest_kernel_download_url)

    # 27882928 bytes
    logger.info("vmlinux file size: %d bytes", len(http_response.content))

    kernel_version = "unknown"
    kernel_version_match: re.Match[str] | None = re.search(r"vmlinux-(\d+\.\d+\.\d+)", latest_kernel_key)
    if kernel_version_match:
        kernel_version: str = kernel_version_match.group(1)
        # 6.18.51
        logger.info("vmlinux version: %s", kernel_version)
    else:
        logger.warning("Could not extract version from vmlinux file name: %s", latest_kernel_key)

    # Save the vmlinux file to /var/lib/tussilago/kernels/vmlinux-{version}
    # TODO(TheLovinator): Should it be configurable?
    kernel_dir: AsyncPath = get_kernel_dir()
    await kernel_dir.mkdir(parents=True, exist_ok=True)

    # /var/lib/tussilago/kernels/vmlinux-6.18.51
    kernel_path: AsyncPath = kernel_dir / f"vmlinux-{kernel_version}"
    await kernel_path.write_bytes(http_response.content)
    logger.info("Saved vmlinux file to %s", kernel_path)

    # Save the latest kernel version to a file for future reference
    latest_version_file: AsyncPath = kernel_dir / "latest_version.txt"
    await latest_version_file.write_text(kernel_version, encoding="utf-8")


async def get_latest_kernel_key(session: niquests.AsyncSession, weekly_builds: str) -> str | None:
    """Fetch and return the latest Firecracker Linux kernel S3 key for a given build.

    Args:
        session (niquests.AsyncSession): The HTTP session used to make requests.
        weekly_builds (str): The S3 build prefix string.

    Returns:
        str | None: The S3 object key of the newest kernel binary, or None if none found.
    """
    kernel_response: niquests.Response = await session.get(
        url="https://s3.amazonaws.com/spec.ccfc.min",
        params={
            "list-type": "2",
            "prefix": f"{weekly_builds}x86_64/vmlinux-",
        },
    )
    kernel_response.raise_for_status()
    if not kernel_response.text:
        logger.warning("No response received from S3 when fetching vmlinux files.")
        return None

    kernel_keys: list[str] = re.findall(
        r"(?<=<Key>)firecracker-ci/[0-9]{8}-[^/]+/x86_64/vmlinux-[0-9]+\.[0-9]+\.[0-9]{1,3}(?=</Key>)",
        kernel_response.text,
    )

    if not kernel_keys:
        logger.warning("No vmlinux files found in the latest Firecracker CI build.")
        return None

    logger.info("Found %d vmlinux files in the latest Firecracker CI build.", len(kernel_keys))

    # Sort the kernel files by version number to get the latest one
    kernel_keys = natsorted(kernel_keys)
    for kernel_key in kernel_keys:
        # firecracker-ci/20260930-a738f18a8db0-0/x86_64/vmlinux-5.10.268
        # firecracker-ci/20260930-a738f18a8db0-0/x86_64/vmlinux-6.1.186
        # firecracker-ci/20260930-a738f18a8db0-0/x86_64/vmlinux-6.18.51
        logger.info("Found vmlinux file: %s", kernel_key)

    return kernel_keys[-1]


async def get_latest_firecracker_build(session: niquests.AsyncSession) -> str | None:
    """Get the latest firecracker build from S3.

    Args:
        session (niquests.AsyncSession): The session we use for all the web requests.

    Returns:
        str | None: S3 prefix of the newest build, or None if no builds found.
    """
    # https://s3.amazonaws.com/spec.ccfc.min?list-type=2&prefix=firecracker-ci%2F&delimiter=%2F
    s3_response: niquests.Response = await session.get(
        url="https://s3.amazonaws.com/spec.ccfc.min",
        params={
            "list-type": "2",
            "prefix": "firecracker-ci/",
            "delimiter": "/",
        },
    )
    s3_response.raise_for_status()

    if not s3_response.text:
        logger.warning("No response received from S3 when fetching Firecracker CI builds.")
        return None

    weekly_builds: list[str] = re.findall(
        pattern=r"(?<=<Prefix>)firecracker-ci/[0-9]{8}-[^/]+/(?=</Prefix>)",
        string=s3_response.text,
    )

    # Sort the builds in reverse order to get the latest one
    weekly_builds.sort(reverse=True)

    if not weekly_builds:
        logger.warning("No Firecracker CI builds found in S3.")
        return None

    latest_build: str = weekly_builds[0]

    logger.info("Latest Firecracker CI build: %s", latest_build)

    # firecracker-ci/20260930-a738f18a8db0-0/
    return latest_build


async def download_linux_kernel() -> None:
    """Fetch the latest Firecracker Linux kernel from S3."""
    session = niquests.AsyncSession()

    weekly_builds: str | None = await get_latest_firecracker_build(session)
    if not weekly_builds:
        return

    latest_kernel_key: str | None = await get_latest_kernel_key(session, weekly_builds)
    if not latest_kernel_key:
        return

    await save_kernel_to_disk(session, latest_kernel_key)
