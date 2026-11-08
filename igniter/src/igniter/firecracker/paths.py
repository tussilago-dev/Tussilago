from anyio import Path as AsyncPath


def get_chroot_dir(vm_id: int) -> AsyncPath:
    """Return the path to the chroot directory for a given VM ID."""
    # /srv/tussilago/firecracker/<vm_id>/root
    return AsyncPath("/srv/tussilago/firecracker") / str(vm_id) / "root"


def get_run_dir(vm_id: int) -> AsyncPath:
    """Return the path to the run directory for a given VM ID."""
    # /srv/tussilago/firecracker/<vm_id>/root/run
    return get_chroot_dir(vm_id) / "run"


def get_vm_data_dir(vm_id: int) -> AsyncPath:
    """Return the path to the VM data directory for a given VM ID."""
    # /var/lib/tussilago/vms/<vm_id>
    return AsyncPath("/var/lib/tussilago/vms") / str(vm_id)


def get_kernel_dir() -> AsyncPath:
    """Return the path to the Firecracker kernel directory."""
    # /var/lib/tussilago/kernels
    return AsyncPath("/var/lib/tussilago/kernels")


def get_jail_socket(vm_id: int) -> AsyncPath:
    """Return the path to the Firecracker socket for a given VM ID.

    This is where the Firecracker process will listen for API requests.
    """
    # /srv/tussilago/firecracker/<vm_id>/root/run/firecracker.sock
    return get_chroot_dir(vm_id) / "run" / "firecracker.sock"


def get_jail_config(vm_id: int) -> AsyncPath:
    """Return the path to the Firecracker configuration file for a given VM ID.

    This is where the Firecracker process will read its configuration from.
    """
    # /srv/tussilago/firecracker/<vm_id>/root/config.json
    return get_chroot_dir(vm_id) / "config.json"


def get_console_log(vm_id: int) -> AsyncPath:
    """Return the path to the console log file for a given VM ID.

    This is where the Firecracker process will write its console output.
    """
    # /srv/tussilago/firecracker/<vm_id>/root/console.log
    return get_chroot_dir(vm_id) / "console.log"


def get_kernel_path(kernel_version: str) -> AsyncPath:
    """Return the path to the Firecracker kernel for a given kernel version."""
    # /var/lib/tussilago/kernels/<kernel_version>/vmlinux
    return get_kernel_dir() / kernel_version / "vmlinux"


def get_arch_base_image_path() -> AsyncPath:
    """Return the path to the base Arch Linux root filesystem image."""
    # TODO(TheLovinator): Use versioning for the base image in the future.
    return AsyncPath("/var/lib/tussilago/rootfs/arch-base.ext4")
