import asyncio
import ipaddress
import logging
import os
import re
import shutil
from typing import TYPE_CHECKING
from urllib.parse import quote

import anyio
import niquests
from anyio import Path as AsyncPath
from dbus_fast import BusType
from dbus_fast import Message
from dbus_fast import MessageType
from dbus_fast import Variant
from dbus_fast.aio import MessageBus

from igniter.firecracker.images import download_linux_kernel
from igniter.firecracker.models import VirtualMachine
from igniter.firecracker.settings import DATA_DIR
from igniter.firecracker.settings import FIRECRACKER_PATH
from igniter.firecracker.settings import JAILER_PATH
from igniter.firecracker.settings import JAILER_ROOT
from igniter.firecracker.settings import KERNEL_DIR
from igniter.firecracker.settings import LOG_DIR
from igniter.firecracker.settings import VM_DATA_DIR
from igniter.networking import generate_mac_from_ip
from igniter.networking import get_default_host_interface

if TYPE_CHECKING:
    from asyncio.subprocess import Process


logger: logging.Logger = logging.getLogger("tussilago")
TAP_NAME_PATTERN: re.Pattern[str] = re.compile(r"[A-Za-z0-9_.-]{1,15}")


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
        return 20000 + vm_id

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
