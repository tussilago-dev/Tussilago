import enum
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any

from anyio import Path as AsyncPath

if TYPE_CHECKING:
    import ipaddress


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
