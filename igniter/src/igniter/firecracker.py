import asyncio
import hashlib
import logging
import re
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote

import anyio
import niquests
from anyio import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from natsort import natsorted

if TYPE_CHECKING:
    from asyncio.subprocess import Process

logger: logging.Logger = logging.getLogger("tussilago")

DATA_DIR: Path = Path("/var/lib/tussilago")
s3_url = "https://s3.amazonaws.com/spec.ccfc.min"


def generate_mac_from_id(vm_id: str) -> str:
    """Generate a MAC address from a VM ID.

    Args:
        vm_id (str): The VM ID.

    Returns:
        str: A MAC address in the format "XX:XX:XX:XX:XX:XX".
    """
    # Hash the VM ID
    digest: bytes = hashlib.sha256(vm_id.encode()).digest()

    # Set locally administered (bit 1 = 1) and unicast (bit 0 = 0)
    first_byte: int = (digest[0] & 0xFE) | 0x02

    mac_bytes: list[int] = [first_byte, *list(digest[1:6])]
    return ":".join(f"{b:02x}" for b in mac_bytes)


async def download_linux_kernel():
    """Fetch the latest Firecracker Linux kernel from S3."""
    s = niquests.AsyncSession()

    # TODO(TheLovinator): Build an URL instead of using string concatenation
    url: str = f"{s3_url}?list-type=2&prefix=firecracker-ci/&delimiter=/"
    s3_response: niquests.Response = await s.get(url)
    s3_response.raise_for_status()

    if not s3_response.text:
        logger.warning("No response received from S3 when fetching Firecracker CI builds.")
        return

    weekly_builds: list[str] = re.findall(
        r"(?<=<Prefix>)firecracker-ci/[0-9]{8}-[^/]+/(?=</Prefix>)",
        s3_response.text,
    )

    # Sort the builds in reverse order to get the latest one
    weekly_builds.sort(reverse=True)

    if not weekly_builds:
        logger.warning("No Firecracker CI builds found in S3.")
        return
    logger.info("Latest Firecracker CI build: %s", weekly_builds[0])

    # TODO(TheLovinator): Build an URL instead of using string concatenation
    latest_kernel_url: str = f"{s3_url}?list-type=2&prefix={weekly_builds[0]}x86_64/vmlinux-"

    kernel_response: niquests.Response = await s.get(latest_kernel_url)
    kernel_response.raise_for_status()
    if not kernel_response.text:
        logger.warning("No response received from S3 when fetching vmlinux files.")
        return

    kernel_keys: list[str] = re.findall(
        r"(?<=<Key>)firecracker-ci/[0-9]{8}-[^/]+/x86_64/vmlinux-[0-9]+\.[0-9]+\.[0-9]{1,3}(?=</Key>)",
        kernel_response.text,
    )

    if not kernel_keys:
        logger.warning("No vmlinux files found in the latest Firecracker CI build.")
        return

    logger.info("Found %d vmlinux files in the latest Firecracker CI build.", len(kernel_keys))

    # Sort the kernel files by version number to get the latest one
    kernel_keys = natsorted(kernel_keys)
    for kernel_key in kernel_keys:
        logger.info("Found vmlinux file: %s", kernel_key)

    latest_kernel_key: str = kernel_keys[-1]

    latest_kernel_download_url: str = f"{s3_url}/{latest_kernel_key}"

    http_response: niquests.Response = await s.get(latest_kernel_download_url)
    http_response.raise_for_status()
    if not http_response.content:
        logger.warning("No content received when downloading the latest vmlinux file.")
        return

    logger.info("Downloaded vmlinux file from %s", latest_kernel_download_url)
    logger.info("vmlinux file size: %d bytes", len(http_response.content))

    kernel_version = "unknown"
    kernel_version_match: re.Match[str] | None = re.search(r"vmlinux-(\d+\.\d+\.\d+)", latest_kernel_key)
    if kernel_version_match:
        kernel_version: str = kernel_version_match.group(1)
        logger.info("vmlinux version: %s", kernel_version)
    else:
        logger.warning("Could not extract version from vmlinux file name: %s", latest_kernel_key)

    # Save the vmlinux file to /var/lib/tussilago/kernels/vmlinux-{version}
    # TODO(TheLovinator): Should it be configurable?
    kernel_dir: Path = DATA_DIR / "kernels"
    await kernel_dir.mkdir(parents=True, exist_ok=True)
    kernel_path: Path = kernel_dir / f"vmlinux-{kernel_version}"
    await kernel_path.write_bytes(http_response.content)
    logger.info("Saved vmlinux file to %s", kernel_path)

    # Save the latest kernel version to a file for future reference
    latest_version_file: Path = kernel_dir / "latest_version.txt"
    await latest_version_file.write_text(kernel_version, encoding="utf-8")

    latest_ubuntu_key: str = f"{s3_url}?list-type=2&prefix={weekly_builds[0]}x86_64/ubuntu-"
    latest_ubuntu_response: niquests.Response = await s.get(latest_ubuntu_key)
    latest_ubuntu_response.raise_for_status()

    if not latest_ubuntu_response.text:
        logger.warning("No response received from S3 when fetching Ubuntu rootfs files.")
        return

    latest_ubuntu_keys: list[str] = re.findall(
        rf"(?<=<Key>){weekly_builds[0]}x86_64/ubuntu-[0-9]+\.[0-9]+\.squashfs(?=</Key>)",
        latest_ubuntu_response.text,
    )
    if not latest_ubuntu_keys:
        logger.warning("No Ubuntu rootfs files found in the latest Firecracker CI build.")
        return
    logger.info("Found %d Ubuntu rootfs files in the latest Firecracker CI build.", len(latest_ubuntu_keys))

    # Sort the Ubuntu rootfs files by version number to get the latest one
    latest_ubuntu_keys = natsorted(latest_ubuntu_keys)
    for ubuntu_key in latest_ubuntu_keys:
        logger.info("Found Ubuntu rootfs file: %s", ubuntu_key)

    latest_ubuntu_key: str = latest_ubuntu_keys[-1]

    # ubuntu_version=$(basename $latest_ubuntu_key .squashfs | grep -oE '[0-9]+\.[0-9]+')
    filename: str = Path(latest_ubuntu_key).name
    ubuntu_version_match: re.Match[str] | None = re.search(r"\d+\.\d+", filename)
    ubuntu_version: str | None = ubuntu_version_match.group(0) if ubuntu_version_match else None
    if not ubuntu_version:
        logger.warning("Could not extract version from Ubuntu rootfs file name: %s", filename)
        return

    logger.info("Latest Ubuntu rootfs version: %s", ubuntu_version)

    # Download the latest Ubuntu rootfs file
    latest_ubuntu_download_url: str = f"{s3_url}/{latest_ubuntu_key}"
    ubuntu_response: niquests.Response = await s.get(latest_ubuntu_download_url)
    ubuntu_response.raise_for_status()
    if not ubuntu_response.content:
        logger.warning("No content received when downloading the latest Ubuntu rootfs file.")
        return

    # Save the Ubuntu rootfs file to /var/lib/tussilago/rootfs/ubuntu-{version}.squashfs
    rootfs_dir: Path = DATA_DIR / "rootfs"
    await rootfs_dir.mkdir(parents=True, exist_ok=True)

    rootfs_path: Path = rootfs_dir / f"ubuntu-{ubuntu_version}.squashfs"
    await rootfs_path.write_bytes(ubuntu_response.content)
    logger.info("Saved Ubuntu rootfs file to %s", rootfs_path)

    extracted_rootfs: Path = rootfs_dir / f"ubuntu-{ubuntu_version}"

    # Extract rootfs for customization.
    rootfs_dir = await rootfs_dir.resolve()
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
        return

    logger.info("Extracted Ubuntu rootfs to %s", extracted_rootfs)

    # Add SSH public key to the rootfs for remote access
    key: ed25519.Ed25519PrivateKey = ed25519.Ed25519PrivateKey.generate()

    logger.info("Generated new Ed25519 SSH key pair for rootfs access.")

    # Save the public key to the rootfs
    ssh_dir: Path = extracted_rootfs / "root" / ".ssh"
    await ssh_dir.mkdir(parents=True, exist_ok=True)

    authorized_keys_path: Path = ssh_dir / "authorized_keys"
    public_key: bytes = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    )
    await authorized_keys_path.write_bytes(public_key)
    logger.info("Added SSH public key to %s", authorized_keys_path)

    private_key: bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    private_key_path: Path = rootfs_dir / "id_ed25519"
    public_key_path: Path = rootfs_dir / "id_ed25519.pub"

    await private_key_path.write_bytes(private_key)
    await public_key_path.write_bytes(public_key + b"\n")

    logger.info("Saved SSH private key to %s", private_key_path)
    logger.info("Saved SSH public key to %s", public_key_path)

    # We will also need to have the key so we can login from the host machine. Save it to current working directory
    await Path("id_ed25519").write_bytes(private_key)
    logger.info("Saved SSH private key to %s", Path("id_ed25519"))

    # create ext4 filesystem image
    # sudo chown -R root:root squashfs-root
    # truncate -s 1G ubuntu-$ubuntu_version.ext4
    # sudo mkfs.ext4 -d squashfs-root -F ubuntu-$ubuntu_version.ext4
    create_ext4_process: asyncio.subprocess.Process = await asyncio.create_subprocess_exec(
        "truncate",
        "-s",
        "5G",
        str(rootfs_dir / f"ubuntu-{ubuntu_version}.ext4"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await create_ext4_process.wait()

    # sudo mkfs.ext4 -d squashfs-root -F ubuntu-$ubuntu_version.ext4
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

    logger.info("The following files were downloaded and set up:")

    kernel_files: list[Path] = natsorted([f async for f in (DATA_DIR / "kernels").glob("vmlinux-*")])
    if kernel_files:
        logger.info("Kernel: %s", kernel_files[-1])
    else:
        logger.error("ERROR: No kernel files found in %s", DATA_DIR / "kernels")

    rootfs_files: list[Path] = natsorted([f async for f in rootfs_dir.glob("*.ext4")])
    if rootfs_files:
        logger.info("Rootfs: %s", rootfs_files[-1])
    else:
        logger.error("ERROR: No rootfs files found in %s", rootfs_dir)

    ssh_keys: list[Path] = [f async for f in rootfs_dir.glob("id_ed25519*")]
    if ssh_keys:
        logger.info("SSH Key: %s", ssh_keys[-1])
    else:
        logger.error("ERROR: No SSH key files found in %s", rootfs_dir)

    # Check if the kernel and rootfs files are valid
    if kernel_files and rootfs_files:
        kernel_file: Path = kernel_files[-1]
        rootfs_file: Path = rootfs_files[-1]

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
        rootfs_file: Path = rootfs_files[-1]
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
        ssh_key_file: Path = ssh_keys[-1]
        if not await ssh_key_file.exists():
            logger.error("ERROR: SSH key file %s does not exist", ssh_key_file)
        else:
            logger.info("SSH key file %s exists", ssh_key_file)

    # Check if the SSH public key is in the rootfs authorized_keys
    if ssh_keys and rootfs_files:
        ssh_key_file: Path = ssh_keys[-1]
        rootfs_file: Path = rootfs_files[-1]
        authorized_keys_path: Path = extracted_rootfs / "root" / ".ssh" / "authorized_keys"
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

    # Clean up extracted rootfs directory
    if await extracted_rootfs.exists():
        if rootfs_dir not in extracted_rootfs.parents:
            msg_0 = f"Refusing to delete directory outside rootfs directory: {extracted_rootfs}"
            raise RuntimeError(msg_0)

        await anyio.to_thread.run_sync(shutil.rmtree, extracted_rootfs)
        logger.info("Cleaned up extracted rootfs directory %s", extracted_rootfs)


@dataclass(slots=True)
class VirtualMachine:
    id: str
    rootfs: Path
    tap_device: str
    socket_path: Path
    memory_mib: int
    vcpus: int


class VMManager:
    async def _configure_firecracker(self, vm: VirtualMachine) -> None:

        process: Process = await asyncio.create_subprocess_exec(
            "/usr/bin/firecracker",
            "--api-sock",
            str(vm.socket_path),
        )
        logger.info("Started Firecracker process with PID %d", process.pid)

        encoded_socket: str = quote(str(vm.socket_path), safe="")
        client = niquests.AsyncSession(base_url=f"http+unix://{encoded_socket}")

        logger.info("Configuring Firecracker VM %s", vm.id)
        logger.info("Setting machine configuration: vCPUs=%d, Memory=%d MiB", vm.vcpus, vm.memory_mib)
        response: niquests.Response = await client.put(
            "http://localhost/machine-config",
            json={
                "vcpu_count": vm.vcpus,
                "mem_size_mib": vm.memory_mib,
            },
        )
        logger.info("Response from Firecracker API: %s", response.text)

        kernel_version: str = await self._get_latest_kernel_version()
        logger.info("Setting boot source: kernel_image_path=%s", f"{DATA_DIR}/kernels/vmlinux-{kernel_version}")
        response: niquests.Response = await client.put(
            "http://localhost/boot-source",
            json={
                "kernel_image_path": f"{DATA_DIR}/kernels/vmlinux-{kernel_version}",
                "boot_args": "console=ttyS0 reboot=k panic=1",
            },
        )
        logger.info("Response from Firecracker API: %s", response.text)

        logger.info("Setting root filesystem: path_on_host=%s", vm.rootfs)
        response: niquests.Response = await client.put(
            "http://localhost/drives/rootfs",
            json={
                "drive_id": "rootfs",
                "path_on_host": str(vm.rootfs),
                "is_root_device": True,
                "is_read_only": False,
            },
        )
        logger.info("Response from Firecracker API: %s", response.text)

        logger.info(
            "Setting network interface: iface_id=%s, host_dev_name=%s, guest_mac=%s",
            "eth0",
            vm.tap_device,
            generate_mac_from_id(vm.id),
        )
        response: niquests.Response = await client.put(
            "http://localhost/network-interfaces/eth0",
            json={
                "iface_id": "eth0",
                "host_dev_name": vm.tap_device,
                "guest_mac": generate_mac_from_id(vm.id),
            },
        )
        logger.info("Response from Firecracker API: %s", response.text)

        await client.close()

    async def _get_latest_kernel_version(self) -> str:
        kernel_dir: Path = DATA_DIR / "kernels"
        latest_version_file: Path = kernel_dir / "latest_version.txt"
        if not await latest_version_file.exists():
            logger.warning("Latest kernel version file not found. Downloading the latest kernel.")
            await download_linux_kernel()

        kernel_version: str = await latest_version_file.read_text(encoding="utf-8")
        return kernel_version.strip()

    async def _create_tap(self, tap_device: str) -> None:
        process: Process = await asyncio.create_subprocess_exec(
            "ip",
            "tuntap",
            "add",
            tap_device,
            "mode",
            "tap",
        )
        await process.wait()

        process: Process = await asyncio.create_subprocess_exec(
            "ip",
            "link",
            "set",
            tap_device,
            "up",
        )
        await process.wait()

    async def create(
        self,
        *,
        vm_id: str,
        rootfs: Path,
        memory_mib: int = 512,
        vcpus: int = 1,
    ) -> VirtualMachine:
        vm_dir: Path = Path("/var/lib/tussilago/vms") / vm_id
        await vm_dir.mkdir(parents=True, exist_ok=False)

        socket_path: Path = vm_dir / "firecracker.sock"
        tap_device: str = f"tap-{vm_id[:8]}"
        await self._create_tap(tap_device)

        kernel_dir: Path = DATA_DIR / "kernels"
        latest_version_file: Path = kernel_dir / "latest_version.txt"
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
        )

        await self._configure_firecracker(vm)

        return vm
