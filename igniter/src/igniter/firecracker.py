import asyncio
import ipaddress
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

import anyio
import niquests
from anyio import Path as AsyncPath
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from dbus_fast import Message
from dbus_fast import MessageType
from dbus_fast.aio import MessageBus
from dbus_fast.constants import BusType
from natsort import natsorted
from platformdirs import site_log_path

if TYPE_CHECKING:
    from asyncio.subprocess import Process
    from io import FileIO

logger: logging.Logger = logging.getLogger("tussilago")
s3_url = "https://s3.amazonaws.com/spec.ccfc.min"

DATA_DIR: AsyncPath = AsyncPath("/var/lib/tussilago")
LOG_DIR: Path = site_log_path(appname="Tussilago", appauthor=False, ensure_exists=True)


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


async def start_unit(unit: str) -> None:
    """Start a systemd unit using D-Bus.

        StartUnit(
            in  s name,
            in  s mode,
            out o job
        );

    For more information on the systemd D-Bus API:
        `busctl introspect org.freedesktop.systemd1 /org/freedesktop/systemd1`
        https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.systemd1.html

    Args:
        unit (str): The name of the systemd unit to start.

    Raises:
        RuntimeError: If the D-Bus call to start the unit fails.
    """
    bus: MessageBus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    message = Message(
        destination="org.freedesktop.systemd1",
        path="/org/freedesktop/systemd1",
        interface="org.freedesktop.systemd1.Manager",
        member="StartUnit",
        signature="ss",
        body=[unit, "replace"],
    )

    reply: Message = await bus.call(message)

    if reply.message_type == MessageType.ERROR:
        msg: str = f"systemd failed to start {unit}: {reply.body}"
        raise RuntimeError(msg)

    bus.disconnect()


async def stop_unit(unit: str) -> None:
    """Stop a systemd unit using D-Bus.

    Enqueues a start job and possibly depending jobs. It takes the unit to activate and a mode string as arguments.

    Modes:
        - "replace":
            The method will start the unit and its dependencies, possibly replacing already queued jobs that conflict with it.
        - "fail":
            The method will start the unit and its dependencies, but will fail if this would change an already queued job.
        - "isolate":
            The method will start the unit in question and terminate all units that are not dependencies of it.
        - "ignore-dependencies": (not recommended)
            It will start a unit but ignore all its dependencies.
        - "ignore-requirements": (not recommended)
            It will start a unit but only ignore the requirement dependencies.

    For more information:
        `busctl introspect org.freedesktop.systemd1 /org/freedesktop/systemd1`
        https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.systemd1.html


    Args:
        unit (str): The name of the systemd unit to stop.

    Raises:
        RuntimeError: If the D-Bus call to stop the unit fails.
    """  # ruff: ignore[line-too-long]
    bus: MessageBus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    reply: Message = await bus.call(
        Message(
            destination="org.freedesktop.systemd1",
            path="/org/freedesktop/systemd1",
            interface="org.freedesktop.systemd1.Manager",
            member="StopUnit",
            signature="ss",  # The signature "ss" indicates that the method expects two string arguments.
            body=[
                unit,
                "Replace",  # "replace", "fail", "isolate", "ignore-dependencies", "ignore-requirements",
            ],
        )
    )

    if reply.message_type == MessageType.ERROR:
        msg: str = f"systemd failed to stop {unit}: {reply.body}"
        raise RuntimeError(msg)

    bus.disconnect()


def generate_mac_from_ip(guest_ip: ipaddress.IPv4Address) -> str:
    """Generate a Firecracker-compatible MAC address from a guest IPv4 address.

    Firecracker CI Ubuntu rootfs uses fcnet-setup.sh which expects:
    06:00:<hex-ip-byte-1>:<hex-ip-byte-2>:<hex-ip-byte-3>:<hex-ip-byte-4>
    """
    hex_ip: str = ":".join(f"{b:02x}" for b in guest_ip.packed)
    return f"06:00:{hex_ip}"


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

    latest_ubuntu_response: niquests.Response | None = await get_s3_listing(session, weekly_builds)
    if not latest_ubuntu_response or not latest_ubuntu_response.text:
        return

    latest_ubuntu_keys: str | None = get_latest_ubuntu_key(
        weekly_builds,
        latest_ubuntu_response_text=latest_ubuntu_response.text,
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


async def get_s3_listing(session: niquests.AsyncSession, weekly_builds: str) -> niquests.Response | None:
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

    return latest_ubuntu_response


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
    latest_ubuntu_download_url: str = f"{s3_url}/{latest_ubuntu_key}"
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
    latest_kernel_download_url: str = f"{s3_url}/{latest_kernel_key}"

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


@dataclass(slots=True)
class VirtualMachine:
    """Configuration and state container for a Firecracker microVM instance.

    Attributes:
        id (str): Unique identifier for the microVM.
        rootfs (AsyncPath): Filesystem path to the rootfs disk image.
        tap_device (str): Name of the host TAP network device.
        socket_path (AsyncPath): Path to the Firecracker control UNIX domain socket.
        memory_mib (int): Amount of RAM in MiB allocated to the microVM.
        vcpus (int): Number of virtual CPUs allocated to the microVM.
        host_ip (ipaddress.IPv4Interface): Host IP and subnet mask on the TAP interface.
        guest_ip (ipaddress.IPv4Address): IPv4 address assigned to the guest microVM.
    """

    id: str
    rootfs: AsyncPath
    tap_device: str
    socket_path: AsyncPath
    memory_mib: int
    vcpus: int
    host_ip: ipaddress.IPv4Interface
    guest_ip: ipaddress.IPv4Address


class VMManager:
    """Manager for lifecycle and resource management of Firecracker microVMs."""

    async def _configure_firecracker(self, vm: VirtualMachine) -> None:
        """Start and initialize the Firecracker process and configure its resources.

        Args:
            vm (VirtualMachine): Target virtual machine configuration.
        """
        if await vm.socket_path.exists():
            await vm.socket_path.unlink()

        await self.start_firecracker(vm)

        # As the API socket was created by the Firecracker binary outside the code we
        # have to verify that is was created before starting things.
        with anyio.fail_after(5.0):
            while not await vm.socket_path.exists():  # ruff: ignore[async-busy-wait]
                await anyio.sleep(0.05)

        encoded_socket: str = quote(str(vm.socket_path), safe="")
        async with niquests.AsyncSession(base_url=f"http+unix://{encoded_socket}") as client:
            await self.set_log_file(client, vm)
            await self.set_machine_config(client, vm)
            await self.set_boot_source(client, vm)
            await self.set_drives_rootfs(client, vm)
            await self.set_network_interfaces(client, vm)
            await self.start_microvm(client)

            logger.info("MicroVM %s successfully started", vm.id)

        # private_key_path: AsyncPath = DATA_DIR / "rootfs" / "id_ed25519"
        # await self.configure_guest_networking(vm, private_key_path)

        logger.info("Configured guest networking for %s", vm.id)

    async def start_firecracker(self, vm: VirtualMachine) -> None:
        """Spawn the Firecracker microVM process with stdout directed to a log file.

        Args:
            vm (VirtualMachine): The microVM instance being started.
        """
        console_log_path: AsyncPath = vm.socket_path.parent / "console.log"
        console_file: FileIO = Path(console_log_path).open("wb", buffering=0)  # ruff: ignore[blocking-open-call-in-async-function, open-file-with-context-handler]

        process: Process = await asyncio.create_subprocess_exec(
            "/usr/local/bin/firecracker",
            "--api-sock",
            str(vm.socket_path),
            "--enable-pci",
            stdout=console_file,
            stderr=asyncio.subprocess.DEVNULL,
        )

        logger.info("Started Firecracker process with PID %d", process.pid)

    async def set_log_file(self, client: niquests.AsyncSession, vm: VirtualMachine) -> None:
        """Configure the Firecracker logging endpoint via its API socket.

        Args:
            client (niquests.AsyncSession): HTTP client connected to the API socket.
            vm (VirtualMachine): Virtual machine instance.
        """
        log_file = Path(LOG_DIR / vm.id / "firecracker.log")
        log_file.parent.mkdir(parents=True, exist_ok=True)

        res: niquests.Response = await client.put(
            "/logger",
            json={
                "log_path": str(log_file),
                "level": "Debug",
                "show_level": True,
                "show_log_origin": True,
            },
        )
        res.raise_for_status()

        logger.info("Log file set to: %s", log_file)

    async def start_microvm(self, client: niquests.AsyncSession) -> None:
        """Send the InstanceStart action to the Firecracker microVM.

        Args:
            client (niquests.AsyncSession): HTTP client connected to the API socket.
        """
        res: niquests.Response = await client.put(
            "/actions",
            json={
                "action_type": "InstanceStart",
            },
        )
        res.raise_for_status()

    async def set_network_interfaces(self, client: niquests.AsyncSession, vm: VirtualMachine) -> None:
        """Attach a TAP network interface to the microVM via the Firecracker API.

        Args:
            client (niquests.AsyncSession): HTTP client connected to the API socket.
            vm (VirtualMachine): Virtual machine instance with network definitions.
        """
        guest_mac: str = generate_mac_from_ip(vm.guest_ip)
        res: niquests.Response = await client.put(
            "/network-interfaces/eth0",
            json={
                "iface_id": "eth0",
                "host_dev_name": vm.tap_device,
                "guest_mac": guest_mac,
            },
        )
        res.raise_for_status()

    async def set_drives_rootfs(self, client: niquests.AsyncSession, vm: VirtualMachine) -> None:
        """Attach the root filesystem drive via the Firecracker API.

        Args:
            client (niquests.AsyncSession): HTTP client connected to the API socket.
            vm (VirtualMachine): Virtual machine instance defining the rootfs path.
        """
        res: niquests.Response = await client.put(
            "/drives/rootfs",
            json={
                "drive_id": "rootfs",
                "path_on_host": str(vm.rootfs),
                "is_root_device": True,
                "is_read_only": False,
            },
        )
        res.raise_for_status()

    async def set_boot_source(self, client: niquests.AsyncSession, vm: VirtualMachine) -> None:
        """Configure the kernel image and kernel boot arguments for the microVM.

        Args:
            client (niquests.AsyncSession): HTTP client connected to the API socket.
            vm (VirtualMachine): Virtual machine instance.
        """
        kernel_version: str = await self.get_latest_kernel_version()
        boot_args = f"console=ttyS0 reboot=k panic=1 ip={vm.guest_ip}::{vm.host_ip.ip}:255.255.255.252::eth0:off"

        res: niquests.Response = await client.put(
            "/boot-source",
            json={
                "kernel_image_path": f"{DATA_DIR}/kernels/vmlinux-{kernel_version}",
                "boot_args": boot_args,
            },
        )
        res.raise_for_status()

    async def set_machine_config(self, client: niquests.AsyncSession, vm: VirtualMachine) -> None:
        """Set the machine config.

        Args:
            vm (VirtualMachine): The Firecracker MicroVM we want to modify.
            client (niquests.AsyncSession): The AsyncSession client.
        """
        res: niquests.Response = await client.put(
            url="/machine-config",
            json={
                "vcpu_count": vm.vcpus,
                "mem_size_mib": vm.memory_mib,
            },
        )
        res.raise_for_status()
        logger.info("Machine configured with %s vCPUs and %s mib", vm.vcpus, vm.memory_mib)

    async def get_latest_kernel_version(self) -> str:
        """Retrieve the latest kernel version string from disk or download it if absent.

        Returns:
            str: Kernel version string (e.g. '6.18.51').
        """
        kernel_dir: AsyncPath = DATA_DIR / "kernels"
        latest_version_file: AsyncPath = kernel_dir / "latest_version.txt"
        if not await latest_version_file.exists():
            logger.warning("Latest kernel version file not found. Downloading the latest kernel.")
            await download_linux_kernel()

        kernel_version: str = await latest_version_file.read_text(encoding="utf-8")

        logger.info("Latest kernel version is %s", kernel_version)
        return kernel_version.strip()

    async def setup_network_interface(self, tap_device: str, tap_ip: str = "172.16.0.1/30") -> None:
        """Create and configure TAP device.

        Args:
            tap_device (str): Name of the TAP interface to create.
            tap_ip (str, optional): Host IP and CIDR for the TAP device. Defaults to "172.16.0.1/30".

        Raises:
            RuntimeError: If device creation, configuration, or NAT rules fail.
        """
        interface: ipaddress.IPv4Interface = self.validate_and_get_interface(tap_ip)

        del_proc: Process = await asyncio.create_subprocess_exec(
            "ip",
            "link",
            "del",
            tap_device,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await del_proc.communicate()
        logger.info("Deleted %s tap (if available)", tap_device)

        # Add tap device
        add_proc: Process = await asyncio.create_subprocess_exec(
            "ip",
            "tuntap",
            "add",
            tap_device,
            "mode",
            "tap",
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await add_proc.communicate()
        if add_proc.returncode != 0:
            msg = f"Failed to create TAP device {tap_device}: {stderr.decode().strip()}"
            raise RuntimeError(msg)
        logger.info("Created tap for %s", tap_device)

        # Assign host IP to tap
        add_ip_proc: Process = await asyncio.create_subprocess_exec(
            "ip",
            "addr",
            "add",
            str(interface),
            "dev",
            tap_device,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await add_ip_proc.communicate()
        if add_ip_proc.returncode != 0:
            msg = f"Failed to assign IP {interface} to {tap_device}: {stderr.decode().strip()}"
            raise RuntimeError(msg)
        logger.info("Assigned %s host IP to %s", interface, tap_device)

        # Bring tap interface up
        bring_up_proc: Process = await asyncio.create_subprocess_exec(
            "ip",
            "link",
            "set",
            tap_device,
            "up",
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await bring_up_proc.communicate()
        if bring_up_proc.returncode != 0:
            msg = f"Failed to bring up TAP device {tap_device}: {stderr.decode().strip()}"
            raise RuntimeError(msg)
        logger.info("Brought up tap %s", tap_device)

        # Set up microVM internet access
        # TODO(TheLovinator): Remove this as it is unnecessary to remove it without reason,
        #   we should remove everything when we remove the VMs.
        host_iface: str = await get_default_host_interface()
        setup_internet_proc: Process = await asyncio.create_subprocess_exec(
            "iptables",
            "-t",
            "nat",
            "-D",
            "POSTROUTING",
            "-o",
            host_iface,
            "-j",
            "MASQUERADE",
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await setup_internet_proc.communicate()
        if setup_internet_proc.returncode != 0:
            err_msg = stderr.decode().strip()
            # Ignore if rule wasn't there in the first place
            if "Bad rule" not in err_msg and "does a matching rule exist" not in err_msg:
                msg = f"Failed to delete outbound NAT via {host_iface}: {err_msg}"
                raise RuntimeError(msg)
            logger.debug("NAT rule via %s did not exist, nothing to delete.", host_iface)
        else:
            logger.info("Removed outbound NAT rule via %s", host_iface)

        # TODO(TheLovinator): Move to nftables
        # TODO(TheLovinator): Get host_iface instead of rawdogging enp40s0
        setup_internet_proc: Process = await asyncio.create_subprocess_exec(
            "iptables",
            "-t",
            "nat",
            "-A",
            "POSTROUTING",
            "-o",
            host_iface,
            "-j",
            "MASQUERADE",
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await setup_internet_proc.communicate()
        if setup_internet_proc.returncode != 0:
            msg = f"Failed to create outbound NAT via {host_iface}: {stderr.decode().strip()}"
            raise RuntimeError(msg)
        logger.info("Added outbound NAT rule via %s", host_iface)

    def validate_and_get_interface(self, tap_ip: str) -> ipaddress.IPv4Interface:
        """Validate IP.

        Args:
            tap_ip (str): IP with optional subnet. For example 172.16.0.1/30

        Returns:
            ipaddress.IPv4Interface: A single IPv4 Addresses.

        Raises:
            ValueError: If not valid IPv4.
        """
        try:
            interface = ipaddress.IPv4Interface(tap_ip)
        except ValueError as e:
            msg: str = f"'{tap_ip}' is not a valid IPv4 address/interface."
            raise ValueError(msg) from e
        if not interface.is_private:
            msg: str = f"'{tap_ip}' is not a private IPv4 address."
            raise ValueError(msg)
        return interface

    async def create(
        self,
        *,
        vm_id: str,
        rootfs: AsyncPath,
        host_ip: ipaddress.IPv4Interface,
        guest_ip: ipaddress.IPv4Address,
        memory_mib: int = 512,
        vcpus: int = 1,
    ) -> VirtualMachine:
        """Create and start a new Firecracker microVM instance.

        Args:
            vm_id (str): Unique identifier for the microVM.
            rootfs (AsyncPath): Filesystem path to the rootfs disk image.
            host_ip (ipaddress.IPv4Interface): Host network interface configuration for the TAP device.
            guest_ip (ipaddress.IPv4Address): IPv4 address assigned to the microVM.
            memory_mib (int, optional): Memory in MiB. Defaults to 512.
            vcpus (int, optional): Number of virtual CPUs. Defaults to 1.

        Returns:
            VirtualMachine: The created and running microVM instance.
        """
        vm_dir: AsyncPath = AsyncPath("/var/lib/tussilago/vms") / vm_id
        await vm_dir.mkdir(parents=True, exist_ok=True)

        socket_path: AsyncPath = vm_dir / "firecracker.sock"
        tap_device: str = f"tap-{vm_id[:8]}"
        await self.setup_network_interface(tap_device, str(host_ip))

        kernel_dir: AsyncPath = DATA_DIR / "kernels"
        latest_version_file: AsyncPath = kernel_dir / "latest_version.txt"
        if not await latest_version_file.exists():
            logger.warning("Latest kernel version file not found. Downloading the latest kernel.")
            await download_linux_kernel()

        vm = VirtualMachine(
            id=vm_id,
            rootfs=rootfs,
            tap_device=tap_device,
            socket_path=socket_path,
            memory_mib=memory_mib,
            vcpus=vcpus,
            host_ip=host_ip,
            guest_ip=guest_ip,
        )

        await self._configure_firecracker(vm)

        return vm

    async def configure_guest_networking(
        self,
        vm: VirtualMachine,
        key_path: AsyncPath | Path,
        timeout: float = 30.0,  # ruff: ignore[async-function-with-timeout]
    ) -> None:
        """Poll the microVM over SSH until reachable and configure guest routing and DNS.

        Args:
            vm (VirtualMachine): The microVM instance to configure.
            key_path (AsyncPath | Path): Path to the private SSH key for authenticating with the guest.
            timeout (float, optional): Maximum time in seconds to wait for SSH connectivity. Defaults to 30.0.

        Raises:
            TimeoutError: If the microVM cannot be reached via SSH within the timeout window.
        """
        guest_ip = str(vm.guest_ip)
        gateway_ip = str(vm.host_ip.ip)

        await anyio.Path(key_path).chmod(0o600)

        guest_command: str = (
            f"ip route replace default via {gateway_ip} dev eth0 && "
            "printf 'nameserver 1.1.1.1\\noptions single-request-reopen\\n' > /etc/resolv.conf"
        )

        ssh_args: list[str] = [
            "ssh",
            "-i",
            str(key_path),
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=2",
            f"root@{guest_ip}",
            guest_command,
        ]

        logger.info("Waiting for SSH on guest %s (%s)...", vm.id, guest_ip)

        last_error = "No attempt made"
        try:  # ruff: ignore[too-many-statements-in-try-clause]
            with anyio.fail_after(timeout):
                while True:
                    proc = await asyncio.create_subprocess_exec(
                        *ssh_args,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, stderr = await proc.communicate()

                    if proc.returncode == 0:
                        logger.info("Guest networking configured successfully on %s", vm.id)
                        return

                    last_error = stderr.decode().strip()
                    logger.warning("SSH to %s failed: %s (retrying...)", guest_ip, last_error or "exit code non-zero")
                    await anyio.sleep(0.5)
        except TimeoutError:
            msg = f"Failed to connect to guest {vm.id} ({guest_ip}) after {timeout}s. Last SSH error: '{last_error}'"
            raise TimeoutError(msg) from None
