from dataclasses import dataclass
from typing import TYPE_CHECKING

from igniter.firecracker.settings import JAILER_ROOT
from igniter.firecracker.settings import VM_DATA_DIR

if TYPE_CHECKING:
    import ipaddress

    from anyio import Path as AsyncPath


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
