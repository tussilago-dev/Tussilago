import enum
import json
import logging
import os
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any

from igniter.firecracker.paths import get_jail_config
from igniter.settings import DEBUG

if TYPE_CHECKING:
    import ipaddress

    from anyio import Path as AsyncPath

logger: logging.Logger = logging.getLogger("tussilago")


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
    """Unique numeric identifier for the organization that owns the VM."""

    vm_id: int
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

    smt: bool = False
    """Whether to enable simultaneous multithreading."""

    netmask: str = "255.255.255.0"
    """IPv4 subnet mask for the VM network."""

    dns_ip: ipaddress.IPv4Address | None = None
    """DNS server address. Uses the network gateway when unset."""

    persistent_data_path: AsyncPath | None = None
    """Optional path to a persistent data disk, such as PostgreSQL data."""

    enable_vsock: bool = True
    """Whether to attach a Firecracker AF_VSOCK device."""

    vsock_cid: int = 3
    """Firecracker Guest CID used for vsock communication."""

    tap_device: str = field(init=False)
    """Host TAP interface name."""

    mac_address: str = field(init=False)
    """MAC address is created from the VM's IPv4 address."""

    def __post_init__(self) -> None:
        """Create TAP interface name and MAC address."""
        self.tap_device = f"tap_{self.vm_id:04d}_01"  # TODO(TheLovinator): Get the actual TAP device name
        self.mac_address = f"06:00:{self.guest_ip.packed.hex(':')}"

    def build_boot_args(self) -> str:
        """Generate Linux kernel command line for zero-delay boot autoconfiguration.

        Returns:
            str: Kernel command line containing serial console parameters and
                ip= autoconfiguration string.
        """
        dns: str = str(self.dns_ip) if self.dns_ip else str(self.gateway_ip)

        # By default, Firecracker will boot a guest microVM passing the following command line parameters to the kernel:
        # Note that Firecracker also has pci=off as default arg if not running with --enable-pci
        default_firecracker_args: list[str] = [
            "reboot=k",  # shut down the guest on reboot, instead of rebooting
            "panic=1",  # on panic, reboot after 1 second
            "nomodule",  # disable loadable kernel module support
            "8250.nr_uarts=0",  # disable 8250 serial interface
            "i8042.noaux",  # do not probe the i8042 controller for an attached mouse (save boot time)
            "i8042.nomux",  # do not probe i8042 for a multiplexing controller (save boot time)
            "i8042.dumbkbd",  # do not attempt to control kbd state via the i8042 (save boot time)
            "swiotlb=noforce",  # disable software bounce buffers (SWIOTLB)
        ]

        args: list[str] = default_firecracker_args.copy()

        if DEBUG:
            args.remove("8250.nr_uarts=0")  # Enable the serial interface in development.
            args.append("console=ttyS0")  # Enable the Firecracker serial console for kernel output.
        else:
            args.extend(["quiet", "loglevel=1"])

        # Configure network
        hostname: str = f"{self.name}.tussilago.internal"
        args.append(f"ip={self.guest_ip}::{self.gateway_ip}:{self.netmask}:{hostname}:eth0:off:{dns}")

        return " ".join(args)

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
            },
        ]

        if self.persistent_data_path is not None:
            drives.append({
                "drive_id": "data",
                "path_on_host": str(self.persistent_data_path),
                "is_root_device": False,
                "is_read_only": False,
            })

        config["drives"] = drives

        net_iface: dict[str, Any] = {
            "iface_id": "eth0",
            "host_dev_name": self.tap_device,
            "guest_mac": self.mac_address,
        }
        config["network-interfaces"] = [net_iface]

        if self.enable_vsock:
            config["vsock"] = {
                "guest_cid": self.vsock_cid,
                "uds_path": "/vsock.sock",
            }

        return config


async def write_firecracker_config(config: VMBuildConfig, *, jailer_root: AsyncPath) -> AsyncPath:
    """Write the Firecracker configuration to a file.

    Args:
        config (VMBuildConfig):  The configuration to write to disk.
        jailer_root (AsyncPath): The root directory for the jailer.

    Returns:
        AsyncPath: The path to the written configuration file.
    """
    await jailer_root.mkdir(parents=True, exist_ok=True)

    destination: AsyncPath = get_jail_config(config.vm_id)
    temporary: AsyncPath = destination.with_name(f"{destination.name}.tmp")

    contents: str = json.dumps(config.to_firecracker_dict(), indent=2) + "\n"
    await temporary.write_text(contents, encoding="utf-8")

    os.chown(str(temporary), config.vm_id, config.vm_id)
    await temporary.chmod(0o400)
    await temporary.replace(destination)

    logger.info("Wrote Firecracker config to %s", destination)

    return destination
