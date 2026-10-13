import asyncio
import logging
import re
import shutil

import anyio
import niquests
from anyio import Path as AsyncPath
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from natsort import natsorted

from igniter.firecracker.settings import DATA_DIR

logger: logging.Logger = logging.getLogger("tussilago")


async def get_s3_listing(session: niquests.AsyncSession, weekly_builds: str) -> str | None:
    """Fetch the S3 XML listing for Ubuntu rootfs images in a build prefix.

    Args:
        session (niquests.AsyncSession): The HTTP session used to make requests.
        weekly_builds (str): The S3 key prefix for the weekly Firecracker build.

    Returns:
        niquests.Response | None: The HTTP response containing the S3 XML listing,
            or None if the response body is empty.
    """
    latest_ubuntu_response: niquests.Response = await session.get(
        "https://s3.amazonaws.com/spec.ccfc.min",
        params={
            "list-type": "2",
            "prefix": f"{weekly_builds}x86_64/ubuntu-",
        },
    )
    latest_ubuntu_response.raise_for_status()

    if not latest_ubuntu_response.text:
        logger.warning("No response received from S3 when fetching Ubuntu rootfs files.")
        return None

    return latest_ubuntu_response.text


async def save_ssh_keys(rootfs_dir: AsyncPath, extracted_rootfs: AsyncPath) -> None:
    """Save generated SSH private and public keys to the host and guest rootfs.

    Args:
        rootfs_dir (AsyncPath): Directory on the host where keys should be saved.
        extracted_rootfs (AsyncPath): Directory containing the extracted rootfs files.
    """
    ssh_dir: AsyncPath = extracted_rootfs / "root" / ".ssh"
    await ssh_dir.mkdir(parents=True, exist_ok=True)
    await ssh_dir.chmod(0o700)

    key: ed25519.Ed25519PrivateKey = ed25519.Ed25519PrivateKey.generate()
    logger.info("Generated new Ed25519 SSH key pair for rootfs access.")
    public_key: bytes = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    )

    authorized_keys_path: AsyncPath = ssh_dir / "authorized_keys"
    await authorized_keys_path.write_bytes(public_key + b"\n")
    await authorized_keys_path.chmod(0o600)
    logger.info("Added SSH public key to %s", authorized_keys_path)

    private_key: bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    private_key_path: AsyncPath = rootfs_dir / "id_ed25519"
    public_key_path: AsyncPath = rootfs_dir / "id_ed25519.pub"

    await private_key_path.write_bytes(private_key)
    await public_key_path.write_bytes(public_key + b"\n")

    logger.info("Saved SSH private key to %s", private_key_path)
    logger.info("Saved SSH public key to %s", public_key_path)

    await AsyncPath("id_ed25519").write_bytes(private_key)
    logger.info("Saved SSH private key to %s", AsyncPath("id_ed25519"))


async def validate_that_everything_is_correct(rootfs_dir: AsyncPath, extracted_rootfs: AsyncPath) -> None:  # ruff: ignore[complex-structure, too-many-branches, too-many-statements]
    """Validate kernel, rootfs image, and SSH key configuration on disk.

    Performs filesystem integrity checks with e2fsck and verifies that public keys
    exist and match the authorized_keys entry in the root filesystem.

    Args:
        rootfs_dir (AsyncPath): Directory containing rootfs images and SSH keys.
        extracted_rootfs (AsyncPath): Directory containing the extracted rootfs files.
    """
    logger.info("The following files were downloaded and set up:")

    kernel_files: list[AsyncPath] = natsorted([f async for f in (DATA_DIR / "kernels").glob("vmlinux-*")])
    if kernel_files:
        logger.info("Kernel: %s", kernel_files[-1])
    else:
        logger.error("ERROR: No kernel files found in %s", DATA_DIR / "kernels")

    rootfs_files: list[AsyncPath] = natsorted([f async for f in rootfs_dir.glob("*.ext4")])
    if rootfs_files:
        logger.info("Rootfs: %s", rootfs_files[-1])
    else:
        logger.error("ERROR: No rootfs files found in %s", rootfs_dir)

    ssh_keys: list[AsyncPath] = [f async for f in rootfs_dir.glob("id_ed25519*")]
    if ssh_keys:
        logger.info("SSH Key: %s", ssh_keys[-1])
    else:
        logger.error("ERROR: No SSH key files found in %s", rootfs_dir)

    # Check if the kernel and rootfs files are valid
    if kernel_files and rootfs_files:
        kernel_file: AsyncPath = kernel_files[-1]
        rootfs_file: AsyncPath = rootfs_files[-1]

        if not await kernel_file.exists():
            logger.error("ERROR: Kernel file %s does not exist", kernel_file)
        else:
            logger.info("Kernel file %s exists", kernel_file)

        if not await rootfs_file.exists():
            logger.error("ERROR: Rootfs file %s does not exist", rootfs_file)
        else:
            logger.info("Rootfs file %s exists", rootfs_file)

    # Check if rootfs is a valid ext4 filesystem
    if rootfs_files:
        rootfs_file: AsyncPath = rootfs_files[-1]
        e2fsck_process: asyncio.subprocess.Process = await asyncio.create_subprocess_exec(
            "e2fsck",
            "-fn",
            str(rootfs_file),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await e2fsck_process.communicate()
        if e2fsck_process.returncode == 0:
            logger.info("Rootfs %s is a valid ext4 filesystem", rootfs_file)
        else:
            logger.error(
                "ERROR: Rootfs %s is not a valid ext4 filesystem. e2fsck output:\n%s", rootfs_file, stderr.decode()
            )

    # Check if the SSH key file exists
    if ssh_keys:
        ssh_key_file: AsyncPath = ssh_keys[-1]
        if not await ssh_key_file.exists():
            logger.error("ERROR: SSH key file %s does not exist", ssh_key_file)
        else:
            logger.info("SSH key file %s exists", ssh_key_file)

    # Check if the SSH public key is in the rootfs authorized_keys
    if ssh_keys and rootfs_files:
        ssh_key_file: AsyncPath = ssh_keys[-1]
        rootfs_file: AsyncPath = rootfs_files[-1]
        authorized_keys_path: AsyncPath = extracted_rootfs / "root" / ".ssh" / "authorized_keys"
        if not await authorized_keys_path.exists():
            logger.error("ERROR: authorized_keys file %s does not exist in the rootfs", authorized_keys_path)
        else:
            authorized_keys_content: str = await authorized_keys_path.read_text(encoding="utf-8")
            public_key: bytes = await ssh_key_file.with_suffix(".pub").read_bytes()
            if public_key.decode() not in authorized_keys_content:
                logger.error(
                    "ERROR: SSH public key %s is not in the rootfs authorized_keys file %s",
                    ssh_key_file.with_suffix(".pub"),
                    authorized_keys_path,
                )
            else:
                logger.info(
                    "SSH public key %s is in the rootfs authorized_keys file %s",
                    ssh_key_file.with_suffix(".pub"),
                    authorized_keys_path,
                )


async def create_ext4_fs(ubuntu_version: str, rootfs_dir: AsyncPath, extracted_rootfs: AsyncPath) -> None:
    """Create a 5GB ext4 filesystem image populated with extracted rootfs files.

    Args:
        ubuntu_version (str): The Ubuntu release version (e.g. '24.04').
        rootfs_dir (AsyncPath): Directory where the resulting .ext4 image will be saved.
        extracted_rootfs (AsyncPath): Directory containing unpacked rootfs contents.
    """
    create_ext4_process: asyncio.subprocess.Process = await asyncio.create_subprocess_exec(
        "truncate",
        "-s",
        "5G",
        str(rootfs_dir / f"ubuntu-{ubuntu_version}.ext4"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await create_ext4_process.wait()

    mkfs_process: asyncio.subprocess.Process = await asyncio.create_subprocess_exec(
        "mkfs.ext4",
        "-d",
        str(extracted_rootfs),
        "-F",
        str(rootfs_dir / f"ubuntu-{ubuntu_version}.ext4"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await mkfs_process.wait()


async def extract_rootfs(ubuntu_version: str, rootfs_dir: AsyncPath, rootfs_path: AsyncPath) -> AsyncPath | None:
    """Extract a squashfs rootfs image to a temporary directory for customization.

    Args:
        ubuntu_version (str): The Ubuntu release version (e.g. '24.04').
        rootfs_dir (AsyncPath): Directory where rootfs images and extractions reside.
        rootfs_path (AsyncPath): Path to the source .squashfs file.

    Returns:
        AsyncPath | None: Path to the extracted rootfs directory, or None on failure.

    Raises:
        RuntimeError: If attempting to clean up a directory outside rootfs_dir.
    """
    # Extract rootfs for customization.
    extracted_rootfs: AsyncPath = rootfs_dir / f"ubuntu-{ubuntu_version}"
    extracted_rootfs = await extracted_rootfs.resolve()

    # Clean up any existing extracted rootfs directory before extraction
    if await extracted_rootfs.exists():
        if rootfs_dir not in extracted_rootfs.parents:
            msg: str = f"Refusing to delete directory outside rootfs directory: {extracted_rootfs}"
            raise RuntimeError(msg)

        await anyio.to_thread.run_sync(shutil.rmtree, extracted_rootfs)
        logger.info("Cleaned up existing extracted rootfs directory %s", extracted_rootfs)

    logger.info("Extracting Ubuntu rootfs to %s", extracted_rootfs)
    await extracted_rootfs.mkdir(parents=True, exist_ok=True)
    subprocess: asyncio.subprocess.Process = await asyncio.create_subprocess_exec(
        "unsquashfs",
        "-d",
        str(extracted_rootfs),
        str(rootfs_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    _stdout, stderr = await subprocess.communicate()
    if subprocess.returncode != 0:
        logger.error("Failed to extract Ubuntu rootfs: %s", stderr.decode())
        return None

    logger.info("Extracted Ubuntu rootfs to %s", extracted_rootfs)
    return extracted_rootfs


async def download_ubuntu_rootfs(
    session: niquests.AsyncSession,
    latest_ubuntu_key: str,
    rootfs_path: AsyncPath,
) -> niquests.Response | None:
    """Download the Ubuntu squashfs rootfs image from S3 to disk.

    Args:
        session (niquests.AsyncSession): The HTTP session used to make requests.
        latest_ubuntu_key (str): S3 object key pointing to the rootfs squashfs file.
        rootfs_path (AsyncPath): Destination path where the file will be saved.

    Returns:
        niquests.Response | None: The response object, or None if response body is empty.
    """
    latest_ubuntu_download_url: str = f"https://s3.amazonaws.com/spec.ccfc.min/{latest_ubuntu_key}"
    ubuntu_response: niquests.Response = await session.get(latest_ubuntu_download_url)
    ubuntu_response.raise_for_status()
    if not ubuntu_response.content:
        logger.warning("No content received when downloading the latest Ubuntu rootfs file.")
        return None

    # Save the Ubuntu rootfs file to /var/lib/tussilago/rootfs/ubuntu-{version}.squashfs
    await rootfs_path.write_bytes(ubuntu_response.content)

    # /var/lib/tussilago/rootfs/ubuntu-24.04.squashfs
    logger.info("Saved Ubuntu rootfs file to %s", rootfs_path)

    return ubuntu_response


def get_ubuntu_version(filename: str) -> str | None:
    """Extract Ubuntu version string from a filename.

    Args:
        filename (str): The filename to inspect (e.g. 'ubuntu-24.04.squashfs').

    Returns:
        str | None: The extracted version string (e.g. '24.04'), or None if not found.
    """
    ubuntu_version_match: re.Match[str] | None = re.search(r"\d+\.\d+", filename)
    ubuntu_version: str | None = ubuntu_version_match.group(0) if ubuntu_version_match else None
    if not ubuntu_version:
        logger.warning("Could not extract version from Ubuntu rootfs file name: %s", filename)
        return None

    # 24.04
    logger.info("Latest Ubuntu rootfs version: %s", ubuntu_version)
    return ubuntu_version


def get_latest_ubuntu_key(weekly_builds: str, latest_ubuntu_response_text: str) -> str | None:
    """Parse S3 listing XML to find the latest Ubuntu rootfs object key.

    Args:
        weekly_builds (str): The S3 prefix for the weekly Firecracker CI build.
        latest_ubuntu_response_text (str): Raw XML body of the S3 listing response.

    Returns:
        str | None: The S3 key of the highest Ubuntu version rootfs, or None if none found.
    """
    latest_ubuntu_keys: list[str] = re.findall(
        rf"(?<=<Key>){weekly_builds}x86_64/ubuntu-[0-9]+\.[0-9]+\.squashfs(?=</Key>)",
        latest_ubuntu_response_text,
    )
    if not latest_ubuntu_keys:
        logger.warning("No Ubuntu rootfs files found in the latest Firecracker CI build.")
        return None

    logger.info("Found %d Ubuntu rootfs files in the latest Firecracker CI build.", len(latest_ubuntu_keys))

    # Sort the Ubuntu rootfs files by version number to get the latest one
    latest_ubuntu_keys_sorted: list[str] = natsorted(latest_ubuntu_keys)
    for ubuntu_key in latest_ubuntu_keys_sorted:
        # firecracker-ci/20260930-a738f18a8db0-0/x86_64/ubuntu-24.04.squashfs
        logger.info("Found Ubuntu rootfs file: %s", ubuntu_key)

    return latest_ubuntu_keys_sorted[-1]


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
    kernel_dir: AsyncPath = DATA_DIR / "kernels"
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


async def download_linux_kernel() -> None:  # ruff: ignore[too-many-return-statements]
    """Fetch the latest Firecracker Linux kernel from S3.

    Raises:
        RuntimeError: If we tried to delete directory outside rootfs directory
    """
    session = niquests.AsyncSession()

    weekly_builds: str | None = await get_latest_firecracker_build(session)
    if not weekly_builds:
        return

    latest_kernel_key: str | None = await get_latest_kernel_key(session, weekly_builds)
    if not latest_kernel_key:
        return

    await save_kernel_to_disk(session, latest_kernel_key)

    latest_ubuntu_response: str | None = await get_s3_listing(session, weekly_builds)
    if not latest_ubuntu_response or not latest_ubuntu_response:
        return

    latest_ubuntu_keys: str | None = get_latest_ubuntu_key(
        weekly_builds,
        latest_ubuntu_response_text=latest_ubuntu_response,
    )
    if not latest_ubuntu_keys:
        return

    filename: str = AsyncPath(latest_ubuntu_keys).name
    ubuntu_version: str | None = get_ubuntu_version(filename)
    if not ubuntu_version:
        return

    rootfs_dir: AsyncPath = DATA_DIR / "rootfs"
    rootfs_dir = await rootfs_dir.resolve()

    await rootfs_dir.mkdir(parents=True, exist_ok=True)
    rootfs_path: AsyncPath = rootfs_dir / f"ubuntu-{ubuntu_version}.squashfs"

    ubuntu_response: niquests.Response | None = await download_ubuntu_rootfs(session, latest_ubuntu_keys, rootfs_path)
    if not ubuntu_response:
        return

    extracted_rootfs: AsyncPath | None = await extract_rootfs(ubuntu_version, rootfs_dir, rootfs_path)
    if not extracted_rootfs:
        return

    await save_ssh_keys(rootfs_dir, extracted_rootfs)
    await create_ext4_fs(ubuntu_version, rootfs_dir, extracted_rootfs)
    await validate_that_everything_is_correct(rootfs_dir, extracted_rootfs)

    # Clean up extracted rootfs directory
    if await extracted_rootfs.exists():
        if rootfs_dir not in extracted_rootfs.parents:
            msg: str = f"Refusing to delete directory outside rootfs directory: {extracted_rootfs}"
            raise RuntimeError(msg)

        await anyio.to_thread.run_sync(shutil.rmtree, extracted_rootfs)
        logger.info("Cleaned up extracted rootfs directory %s", extracted_rootfs)
