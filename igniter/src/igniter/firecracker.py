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
    hex_ip = ":".join(f"{b:02x}" for b in guest_ip.packed)
    return f"06:00:{hex_ip}"


async def download_linux_kernel() -> None:
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
    kernel_dir: AsyncPath = DATA_DIR / "kernels"
    await kernel_dir.mkdir(parents=True, exist_ok=True)

    kernel_path: AsyncPath = kernel_dir / f"vmlinux-{kernel_version}"
    await kernel_path.write_bytes(http_response.content)
    logger.info("Saved vmlinux file to %s", kernel_path)

    # Save the latest kernel version to a file for future reference
    latest_version_file: AsyncPath = kernel_dir / "latest_version.txt"
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
    filename: str = AsyncPath(latest_ubuntu_key).name
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
    rootfs_dir: AsyncPath = DATA_DIR / "rootfs"
    await rootfs_dir.mkdir(parents=True, exist_ok=True)

    rootfs_path: AsyncPath = rootfs_dir / f"ubuntu-{ubuntu_version}.squashfs"
    await rootfs_path.write_bytes(ubuntu_response.content)
    logger.info("Saved Ubuntu rootfs file to %s", rootfs_path)

    extracted_rootfs: AsyncPath = rootfs_dir / f"ubuntu-{ubuntu_version}"

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
    ssh_dir: AsyncPath = extracted_rootfs / "root" / ".ssh"
    await ssh_dir.mkdir(parents=True, exist_ok=True)
    await ssh_dir.chmod(0o700)

    authorized_keys_path: AsyncPath = ssh_dir / "authorized_keys"
    public_key: bytes = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    )
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

    # We will also need to have the key so we can login from the host machine. Save it to current working directory
    await AsyncPath("id_ed25519").write_bytes(private_key)
    logger.info("Saved SSH private key to %s", AsyncPath("id_ed25519"))

    # create ext4 filesystem image
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
    rootfs: AsyncPath
    tap_device: str
    socket_path: AsyncPath
    memory_mib: int
    vcpus: int
    host_ip: ipaddress.IPv4Interface
    guest_ip: ipaddress.IPv4Address


class VMManager:
    async def _configure_firecracker(self, vm: VirtualMachine) -> None:
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
        console_log_path = vm.socket_path.parent / "console.log"
        console_file = Path(console_log_path).open("wb", buffering=0)

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
        res: niquests.Response = await client.put(
            "/actions",
            json={
                "action_type": "InstanceStart",
            },
        )
        res.raise_for_status()

    async def set_network_interfaces(self, client: niquests.AsyncSession, vm: VirtualMachine) -> None:
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

    async def set_drives_rootfs(self, client: niquests.AsyncSession, vm: VirtualMachine):
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
        kernel_dir: AsyncPath = DATA_DIR / "kernels"
        latest_version_file: AsyncPath = kernel_dir / "latest_version.txt"
        if not await latest_version_file.exists():
            logger.warning("Latest kernel version file not found. Downloading the latest kernel.")
            await download_linux_kernel()

        kernel_version: str = await latest_version_file.read_text(encoding="utf-8")

        logger.info("Latest kernel version is %s", kernel_version)
        return kernel_version.strip()

    async def setup_network_interface(self, tap_device: str, tap_ip: str = "172.16.0.1/30") -> None:
        """Create and configure TAP device."""
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
        timeout: float = 30.0,  # Ubuntu with systemd can take 15-25s on first boot
    ) -> None:
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
        try:
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
