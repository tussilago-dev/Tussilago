import asyncio
import asyncio.subprocess
import ipaddress
import logging
import os
import shutil
from ipaddress import IPv4Address
from typing import TYPE_CHECKING

import anyio
from anyio import Path as AsyncPath
from dbus_fast import BusType
from dbus_fast import Message
from dbus_fast import MessageType
from dbus_fast import Variant
from dbus_fast.aio import MessageBus

from igniter.firecracker.config import VMBuildConfig
from igniter.firecracker.config import VMRole
from igniter.firecracker.config import write_firecracker_config
from igniter.firecracker.images import download_linux_kernel
from igniter.firecracker.paths import get_arch_base_image_path
from igniter.firecracker.paths import get_chroot_dir
from igniter.firecracker.paths import get_console_log
from igniter.firecracker.paths import get_jail_socket
from igniter.firecracker.paths import get_kernel_dir
from igniter.firecracker.paths import get_kernel_path
from igniter.firecracker.paths import get_run_dir
from igniter.firecracker.paths import get_vm_data_dir
from igniter.firecracker.rootfs import build_base_rootfs
from igniter.firecracker.rootfs import create_vm_rootfs
from igniter.networking import delete_tap
from igniter.networking import get_default_host_interface
from igniter.networking import teardown_firewall
from igniter.nftables import setup_firewall

if TYPE_CHECKING:
    from asyncio.subprocess import Process

    from anyio._core._fileio import Path


logger: logging.Logger = logging.getLogger("tussilago")

type UnitInfo = tuple[
    str,  # Unit name
    str,  # Description
    str,  # Load state
    str,  # Active state
    str,  # Sub state
    str,  # Followed unit
    str,  # Unit object path
    int,  # Job ID
    str,  # Job type
    str,  # Job object path
]
type UnitList = list[UnitInfo]
type SystemdResponse = list[str | UnitList]


class VMManager:
    """Manage Firecracker microVMs through Jailer and systemd."""

    def __init__(self) -> None:
        """Initialize the VMManager with the D-Bus Message Bus."""
        self._bus: MessageBus | None = None
        self._rootfs_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Ensure the base rootfs exists."""
        async with self._rootfs_lock:
            if await get_arch_base_image_path().exists():
                return

            dockerfile_path: AsyncPath = AsyncPath(__file__).parent / "rootfs.Dockerfile"
            base_rootfs: AsyncPath = get_arch_base_image_path()
            await build_base_rootfs(
                dockerfile=dockerfile_path,
                output=base_rootfs,
                size_bytes=2 * 1024**3,
            )

    async def _get_bus(self) -> MessageBus:
        if self._bus is None:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

        return self._bus

    async def _systemd_call(
        self,
        member: str,
        signature: str,
        body: list[str | list[tuple[str, Variant]]],
    ) -> SystemdResponse:
        bus: MessageBus = await self._get_bus()
        reply: Message = await bus.call(
            Message(
                destination="org.freedesktop.systemd1",
                path="/org/freedesktop/systemd1",
                interface="org.freedesktop.systemd1.Manager",
                member=member,
                signature=signature,
                body=body,
            ),
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

    async def reset_failed_unit(self, unit_name: str) -> None:
        """Reset a systemd unit's failed state.

        Raises:
            RuntimeError: If resetting the unit fails.
        """
        # TODO(TheLovinator): We should send the failed unit to Tussilago's logging system instead of just resetting it.
        logger.info("Resetting failed state for systemd unit %s", unit_name)

        bus: MessageBus = await self._get_bus()
        reply: Message = await bus.call(
            Message(
                destination="org.freedesktop.systemd1",
                path="/org/freedesktop/systemd1",
                interface="org.freedesktop.systemd1.Manager",
                member="ResetFailedUnit",
                signature="s",
                body=[unit_name],
            ),
        )

        if reply.message_type == MessageType.ERROR:
            # No stale unit exists, so nothing needs resetting.
            if reply.error_name and reply.error_name.endswith(".NoSuchUnit"):
                return

            msg: str = f"Failed to reset {unit_name}: {reply.error_name}: {reply.body}"
            raise RuntimeError(msg)

    async def stage_file(self, source: AsyncPath, destination: AsyncPath) -> None:
        """Stage a file inside the Firecracker jail.

        Raises:
            RuntimeError: If the file copy fails.
        """
        command = (
            "cp",
            "--reflink=auto",
            "--sparse=always",
            "--",
            str(source),
            str(destination),
        )
        logger.debug("Running: %s", command)

        process: asyncio.subprocess.Process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        _, stderr = await process.communicate()

        if process.returncode != 0:
            await destination.unlink(missing_ok=True)
            msg = f"Failed to stage {self}: {stderr.decode().strip()}"
            raise RuntimeError(msg)

    async def prepare_jailer(self, vm_id: int) -> None:
        """Stage an immutable kernel and a private writable rootfs.

        Raises:
            RuntimeError: Failed to create guest rootfs.
            FileNotFoundError: If kernel was not found.
        """
        run_dir: AsyncPath = get_run_dir(vm_id)
        await run_dir.mkdir(parents=True, exist_ok=True)

        vm_dir: AsyncPath = get_vm_data_dir(vm_id)
        await vm_dir.mkdir(parents=True, exist_ok=True)

        kernel_version: str = await self.get_latest_kernel_version()
        source_kernel: AsyncPath = get_kernel_path(kernel_version=kernel_version)

        if not await source_kernel.is_file():
            raise FileNotFoundError(source_kernel)

        jail_dir: AsyncPath = get_chroot_dir(vm_id)
        await jail_dir.mkdir(parents=True, exist_ok=True)

        base_rootfs: AsyncPath = get_arch_base_image_path()
        for source, destination in (
            (source_kernel, jail_dir / "vmlinux"),
            (base_rootfs, jail_dir / "rootfs.ext4"),
        ):
            if await destination.exists():
                logger.warning("Destination %s already exists, skipping copy", destination)
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
        os.chown(run_dir, uid=vm_id, gid=vm_id)
        await AsyncPath(run_dir).chmod(0o700)

        os.chown(jail_dir / "vmlinux", uid=vm_id, gid=vm_id)
        await AsyncPath(jail_dir / "vmlinux").chmod(0o400)

        rootfs: AsyncPath = jail_dir / "rootfs.ext4"
        os.chown(rootfs, uid=vm_id, gid=vm_id)
        await AsyncPath(rootfs).chmod(0o600)

    async def start_firecracker(self, vm_id: int) -> None:
        """Start Jailer as a systemd-supervised process.

        Raises:
            RuntimeError: If jailer failed to create all the directories.
            FileNotFoundError: Kernel or rootfs is missing from Jailer root dir.
        """
        jail_socket_path: AsyncPath = get_jail_socket(vm_id)
        if await jail_socket_path.exists():
            msg = f"Firecracker socket already exists for VM {vm_id}. Is it already running?"
            raise RuntimeError(msg)

        if not await jail_socket_path.parent.is_dir():
            msg = "Jailer resources have not been prepared"
            raise RuntimeError(msg)

        jail_dir: AsyncPath = get_chroot_dir(vm_id)
        logger.info("Starting Firecracker for VM %s with Jailer root %s", vm_id, jail_dir)

        jail_kernel: AsyncPath = jail_dir / "vmlinux"
        jail_rootfs: AsyncPath = jail_dir / "rootfs.ext4"
        if not await jail_kernel.is_file() or not await jail_rootfs.is_file():
            msg = "Kernel or rootfs missing from Jailer root"
            raise FileNotFoundError(msg)

        # argv[0] must be the executable path. Firecracker options follow "--"
        # and their file paths are interpreted from inside the chroot.
        argv: list[str] = [
            "/usr/local/bin/jailer",
            "--id",
            str(vm_id),
            "--exec-file",
            "/usr/local/bin/firecracker",
            "--uid",
            str(vm_id),
            "--gid",
            str(vm_id),
            "--chroot-base-dir",
            "/srv/tussilago",
            "--",
            "--api-sock",
            "/run/firecracker.sock",  # Note: This is not /run/firecracker.sock but <JAILER_ROOT>/firecracker/<vm.id>/root/run/firecracker.sock  # ruff: ignore[line-too-long]
            "--config-file",
            "/config.json",  # And this one is at /srv/tussilago/firecracker/<vm.id>/root/config.json
        ]

        # TODO(TheLovinator): We should add resource limits.
        unit_name: str = f"tussilago-vm-{vm_id}.service"
        console_log_path: AsyncPath = get_console_log(vm_id)

        await self.reset_failed_unit(unit_name)
        await self.start_transient_unit(
            unit_name,
            [
                ("Description", Variant("s", f"Tussilago VM {vm_id}")),
                (
                    "ExecStart",
                    Variant(
                        "a(sasb)",
                        [("/usr/local/bin/jailer", argv, False)],
                    ),
                ),
                ("Restart", Variant("s", "no")),
                ("KillMode", Variant("s", "control-group")),
                (
                    "StandardOutputFileToAppend",
                    Variant("s", str(console_log_path)),
                ),
                ("StandardError", Variant("s", "journal")),
                ("LimitNOFILE", Variant("t", 65536)),
                ("TasksMax", Variant("t", 64)),
            ],
        )

        logger.info("Started Firecracker for VM %s with systemd unit %s", vm_id, unit_name)

    async def list_running(self) -> list[str]:
        """Return active Tussilago VM systemd units."""
        response: SystemdResponse = await self._systemd_call("ListUnits", "", [])

        if len(response) != 1 or not isinstance(response[0], (list, tuple)):
            logger.error("Unexpected ListUnits reply structure: %r", response)
            return []

        units: UnitList = response[0]
        logger.info("systemd returned %d loaded units", len(units))

        running: list[str] = []

        unit: UnitInfo
        for index, unit in enumerate(units):
            if not isinstance(unit, (list, tuple)) or len(unit) < 10:  # ruff: ignore[magic-value-comparison]
                logger.warning("Malformed unit at index %d: %r", index, unit)
                continue

            unit_name, load_state, active_state, sub_state = (unit[0], unit[2], unit[3], unit[4])

            if unit_name.startswith("tussilago-vm-"):
                logger.info("VM unit: name=%s load=%s active=%s sub=%s", unit_name, load_state, active_state, sub_state)

                if unit_name.endswith(".service") and active_state == "active" and sub_state == "running":
                    running.append(unit_name)

        logger.info("Found %d running Tussilago VMs: %s", len(running), running)
        return running

    async def stop_firecracker(self, vm_id: int) -> None:
        """Stop the systemd unit supervising this VM.

        Raises:
            RuntimeError: If no VM with that ID was found.
        """
        unit_name: str = f"tussilago-vm-{vm_id}.service"
        try:
            await self._systemd_call(
                member="StopUnit",
                signature="ss",
                body=[unit_name, "replace"],
            )
        except RuntimeError as exc:
            if "NoSuchUnit" not in str(exc):
                raise

        logger.info("Stopped systemd unit %s for VM %s", unit_name, vm_id)

    async def close(self) -> None:
        """Disconnect from the system bus."""
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None

    async def get_latest_kernel_version(self) -> str:
        """Retrieve the latest kernel version string from disk or download it if absent.

        Returns:
            str: Kernel version string (e.g. '6.18.51').
        """
        kernel_dir: AsyncPath = get_kernel_dir()
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
            "dev",
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

    async def verify_tap_device(self, build_config: VMBuildConfig, host_ip: ipaddress.IPv4Interface) -> None:
        if not await (AsyncPath("/sys/class/net")).joinpath(build_config.tap_device).exists():
            await self.setup_network_interface(
                tap_device=build_config.tap_device,
                tap_ip=str(host_ip),
            )

    async def create(
        self,
        *,
        vm_id: int,
        host_ip: ipaddress.IPv4Interface,
        guest_ip: ipaddress.IPv4Address,
        disk_size_gib: int = 4,
        memory_mib: int = 512,
        vcpus: int = 1,
    ) -> int:
        """Create and start a microVM, rolling back on failure."""
        logger.info("Creating microVM %s with %d MiB RAM and %d vCPUs", vm_id, memory_mib, vcpus)

        vm_dir: AsyncPath = get_vm_data_dir(vm_id=vm_id)
        await vm_dir.mkdir(parents=True, exist_ok=True)

        rootfs_path: Path = await create_vm_rootfs(
            base_image=get_arch_base_image_path(),
            destination=vm_dir / "rootfs.ext4",
            size_bytes=disk_size_gib * 1024**3,
        )

        build_config = VMBuildConfig(
            dns_ip=IPv4Address("1.1.1.1"),
            enable_vsock=True,
            gateway_ip=IPv4Address("172.16.0.1"),
            guest_ip=guest_ip,
            kernel_path=AsyncPath("/vmlinux"),
            memory_mib=memory_mib,
            name=f"test-vm-for-{vm_id}",
            netmask="",
            organization_id=1337,
            persistent_data_path=None,
            role=VMRole.DJANGO,
            rootfs_path=AsyncPath("/rootfs.ext4"),
            smt=False,
            vcpus=vcpus,
            vm_id=vm_id,
            vsock_cid=vm_id,
        )

        await write_firecracker_config(config=build_config, jailer_root=get_chroot_dir(vm_id))
        await self.verify_tap_device(build_config=build_config, host_ip=host_ip)

        try:
            host_iface: str = await get_default_host_interface()
            logger.info("Default host interface: %s", host_iface)

            await setup_firewall(
                tap_name=build_config.tap_device,
                guest_ip=str(guest_ip),
                external_interface=host_iface,
            )

        except BaseException:
            logger.warning("VM %s creation failed halfway; rolling back...", vm_id)
            await rootfs_path.unlink(missing_ok=True)
            await self.destroy(vm_id)
            raise

        await self.prepare_jailer(vm_id)

        # TODO(TheLovinator): Remove a stale socket only after confirming this VM isn't running.
        jail_socket_path: AsyncPath = get_jail_socket(vm_id)
        await jail_socket_path.unlink(missing_ok=True)

        await self.start_firecracker(vm_id)

        # Wait for Firecracker socket to be created.
        with anyio.fail_after(5.0):
            while not await jail_socket_path.exists():  # ruff: ignore[async-busy-wait]
                await anyio.sleep(0.05)

        # TODO(TheLovinator): Check if it worked.
        logger.info("MicroVM %s started", vm_id)
        logger.info("Successfully created and started microVM %s", vm_id)

        return vm_id

    async def destroy(self, vm_id: int) -> None:
        """Stop a VM and remove its ephemeral resources."""
        try:
            await self.stop_firecracker(vm_id)
        except Exception:
            logger.exception("Error stopping systemd unit for VM %s", vm_id)

        try:
            await delete_tap(vm_id)
        except Exception:
            logger.exception("Error deleting TAP device for VM %s", vm_id)

        try:
            await teardown_firewall(vm_id)
        except Exception:
            logger.exception("Error removing nftables rules for VM %s", vm_id)

        await asyncio.to_thread(shutil.rmtree, get_chroot_dir(vm_id), True)
        logger.info("Removed chroot directory for VM %s", vm_id)

        vm_data_dir: AsyncPath = get_vm_data_dir(vm_id)
        await asyncio.to_thread(shutil.rmtree, vm_data_dir, True)

        logger.info("Removed VM data directory for VM %s", vm_id)
        logger.info("Successfully cleaned up all resources for VM %s", vm_id)
