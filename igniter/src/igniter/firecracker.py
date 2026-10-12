from __future__ import annotations

import asyncio
import enum
import ipaddress
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any
from urllib.parse import quote

import anyio
import niquests
from anyio import Path as AsyncPath
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from dbus_fast import BusType
from dbus_fast import Message
from dbus_fast import MessageType
from dbus_fast import Variant
from dbus_fast.aio import MessageBus
from dbus_fast.constants import BusType
from natsort import natsorted
from platformdirs import site_log_path

from igniter.networking import generate_mac_from_ip
from igniter.networking import get_default_host_interface

if TYPE_CHECKING:
    from asyncio.subprocess import Process
    from pathlib import Path


logger: logging.Logger = logging.getLogger("tussilago")
s3_url = "https://s3.amazonaws.com/spec.ccfc.min"

DATA_DIR: AsyncPath = AsyncPath("/var/lib/tussilago")
LOG_DIR: Path = site_log_path(appname="Tussilago", appauthor=False, ensure_exists=True)

JAILER_ROOT: AsyncPath = AsyncPath("/srv/jailer")
FIRECRACKER_PATH = "/usr/local/bin/firecracker"
JAILER_PATH = "/usr/local/bin/jailer"
VM_DATA_DIR: AsyncPath = DATA_DIR / "vms"
KERNEL_DIR: AsyncPath = DATA_DIR / "kernels"

TAP_NAME_PATTERN: re.Pattern[str] = re.compile(r"[A-Za-z0-9_.-]{1,15}")
VM_UID_GID_BASE = 20000


class VMRole(enum.StrEnum):
    """Workload classification for tenant microVM instances."""

    CELERY = "celery"
    DJANGO = "django"
    NGINX = "nginx"
    POSTGRES = "postgres"
    REDIS = "redis"


@dataclass(slots=True)
class VMBuildConfig:
    """Declarative configuration for a Firecracker microVM."""

    organization_id: int
    """Unique numeric identifier of the organization that owns the VM."""

    vm_id: str
    """Unique numeric identifier for the VM."""

    name: str
    """Unique VM name within the organization (e.g. 'web-1', 'pg-primary')."""

    role: VMRole
    """Service role determining the VM's network tier and default configuration."""

    vcpus: int
    """Number of virtual CPUs allocated to the VM."""

    memory_mib: int
    """Amount of memory allocated to the VM, in MiB."""

    guest_ip: ipaddress.IPv4Address
    """Static IPv4 address assigned to the VM."""

    gateway_ip: ipaddress.IPv4Address
    """IPv4 address of the VM's network gateway."""

    kernel_path: AsyncPath
    """Host path to the Linux kernel image."""

    rootfs_path: AsyncPath
    """Host path to the VM root filesystem image."""

    description: str = ""
    """Optional description of the organization."""

    smt: bool = False
    """Whether to enable Simultaneous Multithreading."""

    netmask: str = "255.255.255.0"
    """IPv4 subnet mask for the VM network."""

    dns_ip: ipaddress.IPv4Address | None = None
    """DNS server address. Uses the network gateway when unset."""

    network_rate_limiter: RateLimiterConfig | None = None
    """Optional network bandwidth and packet-rate limiter."""

    persistent_data_path: AsyncPath | None = None
    """Optional path to a persistent data disk, such as PostgreSQL data."""

    disk_rate_limiter: RateLimiterConfig | None = None
    """Optional disk I/O bandwidth and IOPS limiter."""

    enable_vsock: bool = True
    """Whether to attach a Firecracker AF_VSOCK device."""

    vsock_cid: int = 3
    """Firecracker Guest CID used for vsock communication."""

    tap_device: str = field(init=False)
    """Host TAP interface name, limited to Linux's 15-character interface-name limit."""

    mac_address: str = field(init=False)
    """Deterministic MAC address derived from the VM's IPv4 address."""

    def __post_init__(self) -> None:
        """Derive identifiers, TAP interface names, and MAC addresses."""
        # Linux IFNAMSIZ limit is 16 chars (including null terminator)
        self.tap_device = f"tap{self.vm_id}"

        # Generate predictable MAC: 06:00:<hex-ip-1>:<hex-ip-2>:<hex-ip-3>:<hex-ip-4>
        hex_ip: str = ":".join(f"{b:02x}" for b in self.guest_ip.packed)
        self.mac_address = f"06:00:{hex_ip}"

    @property
    def vm_dir(self) -> AsyncPath:
        """Directory containing the VM's runtime state and sockets."""
        return AsyncPath(f"/var/lib/tussilago/organizations/{self.organization_id}/vms/{self.vm_id}")

    @property
    def socket_path(self) -> AsyncPath:
        """Host filesystem path to the Firecracker management UNIX domain socket."""
        return self.vm_dir / "firecracker.sock"

    @property
    def vsock_uds_path(self) -> AsyncPath:
        """Host filesystem path to the AF_VSOCK backend UNIX domain socket."""
        return self.vm_dir / "vsock.sock"

    @property
    def config_json_path(self) -> AsyncPath:
        """Host filesystem path to the declarative Firecracker configuration file."""
        return self.vm_dir / "config.json"

    def build_boot_args(self) -> str:
        """Generate Linux kernel command line for zero-delay boot autoconfiguration.

        Returns:
            str: Kernel command line containing serial console parameters and
                ip= autoconfiguration string.
        """
        dns: str = str(self.dns_ip) if self.dns_ip else str(self.gateway_ip)

        return (
            f"console=ttyS0 reboot=k panic=1 quiet "
            f"ip={self.guest_ip}::{self.gateway_ip}:{self.netmask}:{self.name}:eth0:off:{dns}"
        )

    def to_firecracker_dict(self) -> dict[str, Any]:
        """Convert the build configuration into Firecracker's declarative JSON schema.

        Returns:
            dict[str, Any]: Dictionary matching Firecracker's declarative resources schema.
        """
        config: dict[str, Any] = {
            "boot-source": {
                "kernel_image_path": str(self.kernel_path),
                "boot_args": self.build_boot_args(),
            },
            "machine-config": {
                "vcpu_count": self.vcpus,
                "mem_size_mib": self.memory_mib,
                "smt": self.smt,
            },
        }

        drives: list[dict[str, Any]] = [
            {
                "drive_id": "rootfs",
                "path_on_host": str(self.rootfs_path),
                "is_root_device": True,
                "is_read_only": False,
            }
        ]

        if self.disk_rate_limiter:
            drives[0]["rate_limiter"] = self.disk_rate_limiter.to_firecracker_dict()

        if self.persistent_data_path:
            persistent_drive: dict[str, Any] = {
                "drive_id": "data",
                "path_on_host": str(self.persistent_data_path),
                "is_root_device": False,
                "is_read_only": False,
            }
            if self.disk_rate_limiter:
                persistent_drive["rate_limiter"] = self.disk_rate_limiter.to_firecracker_dict()
            drives.append(persistent_drive)

        config["drives"] = drives

        net_iface: dict[str, Any] = {
            "iface_id": "eth0",
            "host_dev_name": self.tap_device,
            "guest_mac": self.mac_address,
        }
        if self.network_rate_limiter:
            net_iface["rx_rate_limiter"] = self.network_rate_limiter.to_firecracker_dict()
            net_iface["tx_rate_limiter"] = self.network_rate_limiter.to_firecracker_dict()

        config["network-interfaces"] = [net_iface]

        if self.enable_vsock:
            config["vsock"] = {
                "guest_cid": self.vsock_cid,
                "uds_path": str(self.vsock_uds_path),
            }

        return config


@dataclass(slots=True, frozen=True)
class RateLimiterConfig:
    """Token bucket rate limiter configuration for disk I/O and network traffic.

    Attributes:
        bandwidth_bytes_per_sec (int | None): Maximum allowed bandwidth budget
            in bytes per second. Refilled every 1000ms.
        ops_per_sec (int | None): Maximum allowed operations budget per second
            (IOPS or network packet rate). Refilled every 1000ms.
    """

    bandwidth_bytes_per_sec: int | None = None
    """Maximum allowed bandwidth budget in bytes per second."""

    ops_per_sec: int | None = None
    """Maximum allowed operations budget per second (IOPS or packet rate)."""

    def to_firecracker_dict(self) -> dict[str, Any]:
        """Convert rate limiter settings to the Firecracker JSON schema."""
        result: dict[str, Any] = {}
        if self.bandwidth_bytes_per_sec is not None:
            result["bandwidth"] = {
                "size": self.bandwidth_bytes_per_sec,
                "refill_time": 1000,
            }
        if self.ops_per_sec:
            result["ops"] = {
                "size": self.ops_per_sec,
                "refill_time": 1000,
            }
        return result


async def provision_vm(cfg: VMBuildConfig) -> AsyncPath:
    config_path: AsyncPath = cfg.config_json_path
    await config_path.mkdir(parents=True, exist_ok=True)

    raw_config: dict[str, Any] = cfg.to_firecracker_dict()
    await config_path.write_text(json.dumps(raw_config, indent=2))

    return config_path


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
    """Configuration and runtime paths for one Firecracker microVM."""

    id: int
    rootfs: AsyncPath
    tap_device: str
    memory_mib: int
    vcpus: int
    host_ip: ipaddress.IPv4Interface
    guest_ip: ipaddress.IPv4Address

    @property
    def jail_dir(self) -> AsyncPath:
        """Host-visible root directory of the Jailer chroot."""
        return JAILER_ROOT / "firecracker" / str(self.id) / "root"

    @property
    def jail_socket_path(self) -> AsyncPath:
        """Host-visible path to Firecracker's API socket."""
        return self.jail_dir / "run" / "firecracker.sock"

    @property
    def unit_name(self) -> str:
        return f"tussilago-vm-{self.id}.service"

    @property
    def console_log_path(self) -> AsyncPath:
        return VM_DATA_DIR / str(self.id) / "console.log"

    @property
    def firecracker_log_path(self) -> AsyncPath:
        return self.jail_dir / "run" / "firecracker.log"


class VMManager:  # ruff: ignore[too-many-public-methods]
    """Manage Firecracker microVMs through Jailer and systemd."""

    def __init__(self) -> None:
        self._bus: MessageBus | None = None

    async def _get_bus(self) -> MessageBus:
        if self._bus is None:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

        return self._bus

    async def _systemd_call(
        self,
        member: str,
        signature: str,
        body: list[str | list[tuple[str, Variant]]],
    ) -> list[str]:
        bus: MessageBus = await self._get_bus()
        reply: Message = await bus.call(
            Message(
                destination="org.freedesktop.systemd1",
                path="/org/freedesktop/systemd1",
                interface="org.freedesktop.systemd1.Manager",
                member=member,
                signature=signature,
                body=body,
            )
        )

        if reply.message_type == MessageType.ERROR:
            msg: str = f"systemd {member} failed: {reply.error_name}: {reply.body}"
            raise RuntimeError(msg)

        return reply.body

    async def start_transient_unit(self, unit: str, properties: list[tuple[str, Variant]]) -> None:
        """Transient systemd unit are units without a persistent .service file.

            StartTransientUnit(in  s name,
                        in  s mode,
                        in  a(sv) properties,
                        in  a(sa(sv)) aux,
                        out o job);

        Args:
            unit (str): _description_
            properties (list[tuple[str, Variant]]): _description_
        """
        await self._systemd_call(
            "StartTransientUnit",
            "ssa(sv)a(sa(sv))",
            [unit, "replace", properties, []],
        )

    async def _configure_firecracker(self, vm: VirtualMachine) -> None:
        """Prepare, start, and configure a microVM."""
        await self.prepare_jailer(vm)

        # TODO(TheLovinator): Remove a stale socket only after confirming this VM isn't running.
        await vm.jail_socket_path.unlink(missing_ok=True)

        await self.start_firecracker(vm)
        await self.wait_for_socket(vm)

        encoded_socket: str = quote(str(vm.jail_socket_path), safe="")
        async with niquests.AsyncSession(base_url=f"http+unix://{encoded_socket}") as client:
            await self.set_log_file(client, vm)
            await self.set_machine_config(client, vm)
            await self.set_boot_source(client, vm)
            await self.set_drives_rootfs(client, vm)
            await self.set_network_interfaces(client, vm)
            await self.start_microvm(client)

        logger.info("MicroVM %s started", vm.id)

    async def stop_unit(self, unit: str) -> None:
        await self._systemd_call(
            member="StopUnit",
            signature="ss",
            body=[unit, "replace"],
        )

    def validate_vm_id(self, vm_id: int) -> None:
        if not 1 <= vm_id <= 9999:  # ruff: ignore[magic-value-comparison]
            msg_0: str = f"Invalid VM ID: {vm_id}"
            raise ValueError(msg_0)

    def jailer_uid(self, vm_id: int) -> int:
        self.validate_vm_id(vm_id)
        return VM_UID_GID_BASE + vm_id

    def jailer_gid(self, vm_id: int) -> int:
        return self.jailer_uid(vm_id)

    async def prepare_jailer(self, vm: VirtualMachine) -> None:
        """Stage an immutable kernel and a private writable rootfs.

        Raises:
            ValueError: If VM ID is invalid.
            ValueError: If TAP name is invalid.
            ValueError: If memory or vCPUs values too low.
            RuntimeError: Failed to create guest rootfs.
            FileNotFoundError: If kernel was not found.
        """
        self.validate_vm_id(vm.id)

        if not TAP_NAME_PATTERN.fullmatch(vm.tap_device):
            msg: str = f"Invalid TAP name: {vm.tap_device!r}"
            raise ValueError(msg)

        if vm.memory_mib < 128 or vm.vcpus < 1:  # ruff: ignore[magic-value-comparison]
            msg: str = "VM must have at least 128 MiB RAM and one vCPU"
            raise ValueError(msg)

        jail_dir: AsyncPath = vm.jail_dir
        run_dir: AsyncPath = jail_dir / "run"
        vm_dir: AsyncPath = VM_DATA_DIR / str(vm.id)

        await jail_dir.mkdir(parents=True, exist_ok=True)
        await run_dir.mkdir(parents=True, exist_ok=True)
        await vm_dir.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)

        kernel_version: str = await self.get_latest_kernel_version()
        source_kernel: AsyncPath = KERNEL_DIR / f"vmlinux-{kernel_version}"
        if not await source_kernel.is_file():
            raise FileNotFoundError(source_kernel)

        # cp uses a reflink when supported and falls back to copying.
        # The guest rootfs must never share writable blocks with the base.
        for source, destination in (
            (source_kernel, jail_dir / "vmlinux"),
            (vm.rootfs, jail_dir / "rootfs.ext4"),
        ):
            if await destination.exists():
                continue

            process: Process = await asyncio.create_subprocess_exec(
                "cp",
                "--reflink=auto",
                "--sparse=always",
                "--",
                str(source),
                str(destination),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
            if process.returncode:
                await destination.unlink(missing_ok=True)
                msg = f"Failed to stage {source}: {stderr.decode().strip()}"
                raise RuntimeError(msg)

        # The jailer runs privileged initially, then drops to this VM's UID/GID.
        jailer_gid: int = self.jailer_gid(vm.id)
        jailer_uid: int = self.jailer_uid(vm.id)
        os.chown(run_dir, uid=jailer_uid, gid=jailer_gid)
        await AsyncPath(run_dir).chmod(0o700)

        os.chown(jail_dir / "vmlinux", uid=jailer_uid, gid=jailer_gid)
        await AsyncPath(jail_dir / "vmlinux").chmod(0o400)

        rootfs: AsyncPath = jail_dir / "rootfs.ext4"
        os.chown(rootfs, uid=jailer_uid, gid=jailer_gid)
        await AsyncPath(rootfs).chmod(0o600)

    async def start_firecracker(self, vm: VirtualMachine) -> None:
        """Start Jailer as a systemd-supervised process."""
        self.validate_vm_id(vm.id)
        if not await vm.jail_socket_path.parent.is_dir():
            msg = "Jailer resources have not been prepared"
            raise RuntimeError(msg)

        jail_kernel: AsyncPath = vm.jail_dir / "vmlinux"
        jail_rootfs: AsyncPath = vm.jail_dir / "rootfs.ext4"
        if not await jail_kernel.is_file() or not await jail_rootfs.is_file():
            msg = "Kernel or rootfs missing from Jailer root"
            raise FileNotFoundError(msg)

        await vm.console_log_path.parent.mkdir(parents=True, exist_ok=True)

        # argv[0] must be the executable path. Firecracker options follow "--"
        # and their file paths are interpreted from inside the chroot.
        jailer_gid: int = self.jailer_gid(vm.id)
        jailer_uid: int = self.jailer_uid(vm.id)
        argv: list[str | int] = [
            JAILER_PATH,
            "--id",
            str(vm.id),
            "--exec-file",
            FIRECRACKER_PATH,
            "--uid",
            str(jailer_uid),
            "--gid",
            str(jailer_gid),
            "--chroot-base-dir",
            str(JAILER_ROOT),
            "--",
            "--api-sock",
            "/run/firecracker.sock",
        ]

        # TODO(TheLovinator): We should add resource limits.
        await self.start_transient_unit(
            vm.unit_name,
            [
                ("Description", Variant("s", f"Tussilago VM {vm.id}")),
                (
                    "ExecStart",
                    Variant(
                        "a(sasb)",
                        [(JAILER_PATH, argv, False)],
                    ),
                ),
                ("Restart", Variant("s", "no")),
                ("KillMode", Variant("s", "control-group")),
                (
                    "StandardOutputFileToAppend",
                    Variant("s", str(vm.console_log_path)),
                ),
                ("StandardError", Variant("s", "journal")),
                ("LimitNOFILE", Variant("t", 65536)),
                ("TasksMax", Variant("t", 64)),
            ],
        )

    async def list_running(self) -> list[str]:
        """Return active Tussilago VM systemd units."""
        response = await self._systemd_call("ListUnits", "", [])

        logger.info(
            "ListUnits reply: type=%s, arguments=%d, body=%r",
            type(response).__name__,
            len(response),
            response,
        )

        if len(response) != 1 or not isinstance(response[0], (list, tuple)):
            logger.error("Unexpected ListUnits reply structure: %r", response)
            return []

        units: str | list[tuple[str]] = response[0]
        logger.info("systemd returned %d loaded units", len(units))

        running: list[str] = []

        for index, unit in enumerate(units):
            if not isinstance(unit, (list, tuple)) or len(unit) < 10:  # ruff: ignore[magic-value-comparison]
                logger.warning("Malformed unit at index %d: %r", index, unit)
                continue

            name, load_state, active_state, sub_state = (unit[0], unit[2], unit[3], unit[4])

            if name.startswith("tussilago-vm-"):
                logger.info(
                    "VM unit: name=%s load=%s active=%s sub=%s",
                    name,
                    load_state,
                    active_state,
                    sub_state,
                )

                if name.endswith(".service") and active_state == "active" and sub_state == "running":
                    running.append(name)

        logger.info("Found %d running Tussilago VMs: %s", len(running), running)
        return running

    async def wait_for_socket(
        self,
        vm: VirtualMachine,
        timeout: float = 5.0,
    ) -> None:
        """Wait for the API socket, failing if Jailer exits or times out."""
        with anyio.fail_after(timeout):
            while not await vm.jail_socket_path.exists():
                await anyio.sleep(0.05)

    async def stop_firecracker(self, vm: VirtualMachine) -> None:
        """Stop the systemd unit supervising this VM.

        Raises:
            RuntimeError: If no VM with that ID was found.
        """
        try:
            await self.stop_unit(vm.unit_name)
        except RuntimeError as exc:
            if "NoSuchUnit" not in str(exc):
                raise

    async def close(self) -> None:
        """Disconnect from the system bus."""
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None

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

    async def set_boot_source(
        self,
        client: niquests.AsyncSession,
        vm: VirtualMachine,
    ) -> None:
        """Configure the kernel and guest network boot arguments."""
        boot_args = (
            f"console=ttyS0 reboot=k panic=1 ip={vm.guest_ip}::{vm.host_ip.ip}:{vm.host_ip.network.netmask}::eth0:off"
        )

        response = await client.put(
            "/boot-source",
            json={
                "kernel_image_path": "/vmlinux",
                "boot_args": boot_args,
            },
        )
        response.raise_for_status()

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

    async def set_drives_rootfs(
        self,
        client: niquests.AsyncSession,
        vm: VirtualMachine,
    ) -> None:
        """Attach this VM's private writable root filesystem."""
        response = await client.put(
            "/drives/rootfs",
            json={
                "drive_id": "rootfs",
                "path_on_host": "/rootfs.ext4",
                "is_root_device": True,
                "is_read_only": False,
            },
        )
        response.raise_for_status()

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

    async def set_log_file(
        self,
        client: niquests.AsyncSession,
        vm: VirtualMachine,
    ) -> None:
        """Configure logging inside the Jailer chroot."""
        response = await client.put(
            "/logger",
            json={
                "log_path": "/run/firecracker.log",
                "level": "Info",
                "show_level": True,
                "show_log_origin": True,
            },
        )
        response.raise_for_status()

    async def create(
        self,
        *,
        vm_id: int,
        rootfs: AsyncPath,
        host_ip: ipaddress.IPv4Interface,
        guest_ip: ipaddress.IPv4Address,
        memory_mib: int = 512,
        vcpus: int = 1,
    ) -> VirtualMachine:
        """Create and start a microVM, rolling back on failure."""
        vm_dir: AsyncPath = VM_DATA_DIR / str(vm_id)
        await vm_dir.mkdir(parents=True, exist_ok=True)

        vm = VirtualMachine(
            id=vm_id,
            rootfs=rootfs,
            tap_device=f"tap-{vm_id!s}",
            memory_mib=memory_mib,
            vcpus=vcpus,
            host_ip=host_ip,
            guest_ip=guest_ip,
        )

        try:
            await self.setup_network_interface(
                vm.tap_device,
                str(host_ip),
            )
            await self._configure_firecracker(vm)
        except BaseException:
            # Roll back partial setup, including cancellation.
            try:
                await self.stop_firecracker(vm)
            except Exception:
                logger.exception("Failed to stop partially started VM %s", vm.id)

            try:
                await self._delete_tap(vm.tap_device)
            except Exception:
                logger.exception("Failed to remove TAP for VM %s", vm.id)

            try:
                await asyncio.to_thread(shutil.rmtree, JAILER_ROOT / str(vm.id), True)
                await asyncio.to_thread(shutil.rmtree, vm_dir, True)
            except Exception:
                logger.exception("Failed to clean up VM files for %s", vm.id)

            raise

        return vm

    async def _delete_tap(self, tap_device: str) -> None:
        """Delete a TAP interface if it exists."""
        process = await asyncio.create_subprocess_exec(
            "ip",
            "link",
            "delete",
            tap_device,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()

        if process.returncode != 0:
            error = stderr.decode().strip()
            if "Cannot find device" not in error:
                msg = f"Failed to delete {tap_device}: {error}"
                raise RuntimeError(msg)

    async def destroy(self, vm: VirtualMachine) -> None:
        """Stop a VM and remove its ephemeral resources."""
        await self.stop_firecracker(vm)
        await self._delete_tap(vm.tap_device)

        await asyncio.to_thread(
            shutil.rmtree,
            JAILER_ROOT / str(vm.id),
            True,
        )
        await asyncio.to_thread(
            shutil.rmtree,
            VM_DATA_DIR / str(vm.id),
            True,
        )

        logger.info("Destroyed VM %s", vm.id)
