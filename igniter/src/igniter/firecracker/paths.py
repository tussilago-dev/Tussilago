from anyio import Path as AsyncPath


def chroot_dir(vm_id: str) -> AsyncPath:
    """Return the path to the chroot directory for a given VM ID."""
    # /srv/tussilago/firecracker/<vm_id>/root
    return AsyncPath("/srv/tussilago/firecracker") / vm_id / "root"


def get_jail_socket(vm_id: str) -> AsyncPath:
    """Return the path to the Firecracker socket for a given VM ID.

    This is where the Firecracker process will listen for API requests.
    """
    # /srv/tussilago/firecracker/<vm_id>/root/run/firecracker.sock
    return chroot_dir(vm_id) / "run" / "firecracker.sock"


def get_jail_config(vm_id: str) -> AsyncPath:
    """Return the path to the Firecracker configuration file for a given VM ID.

    This is where the Firecracker process will read its configuration from.
    """
    # /srv/tussilago/firecracker/<vm_id>/root/config.json
    return chroot_dir(vm_id) / "config.json"


def get_console_log(vm_id: str) -> AsyncPath:
    """Return the path to the console log file for a given VM ID.

    This is where the Firecracker process will write its console output.
    """
    # /srv/tussilago/firecracker/<vm_id>/root/console.log
    return chroot_dir(vm_id) / "console.log"


def get_kernel_path(kernel_version: str) -> AsyncPath:
    """Return the path to the Firecracker kernel for a given kernel version."""
    # /var/lib/tussilago/kernels/<kernel_version>/vmlinux
    return AsyncPath("/var/lib/tussilago/kernels") / kernel_version / "vmlinux"
