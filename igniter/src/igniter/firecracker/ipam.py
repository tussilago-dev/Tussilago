import asyncio
import ipaddress
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class AllocatedNetwork:
    host_ip: ipaddress.IPv4Interface
    guest_ip: ipaddress.IPv4Address
    netmask: str
    network: ipaddress.IPv4Network


class IPAM:
    """Allocates /30 subnets per VM from a parent subnet."""

    def __init__(self, base_pool: str = "172.16.0.0/16") -> None:  # ruff: ignore[undocumented-public-init]
        self.pool = ipaddress.IPv4Network(base_pool)
        self.subnets: list[ipaddress.IPv4Network] = list(self.pool.subnets(new_prefix=30))
        self._allocated: dict[int, AllocatedNetwork] = {}
        self._lock = asyncio.Lock()

    async def allocate(self, vm_id: int) -> AllocatedNetwork:
        async with self._lock:
            if vm_id in self._allocated:
                return self._allocated[vm_id]

            # Use vm_id as direct index or find first free subnet
            if vm_id < len(self.subnets):
                subnet: ipaddress.IPv4Network = self.subnets[vm_id]
            else:
                allocated_subnets: set[ipaddress.IPv4Network] = {alloc.network for alloc in self._allocated.values()}
                available: ipaddress.IPv4Network | None = next(
                    (s for s in self.subnets if s not in allocated_subnets),
                    None,
                )
                if not available:
                    msg = "No available /30 subnets remaining in IPAM pool."
                    raise RuntimeError(msg)
                subnet = available

            hosts: list[ipaddress.IPv4Address] = list(subnet.hosts())
            # hosts[0] = Host Gateway TAP IP, hosts[1] = Guest IP
            alloc = AllocatedNetwork(
                host_ip=ipaddress.IPv4Interface(f"{hosts[0]}/{subnet.prefixlen}"),
                guest_ip=hosts[1],
                netmask=str(subnet.netmask),
                network=subnet,
            )
            self._allocated[vm_id] = alloc
            return alloc

    async def release(self, vm_id: int) -> None:
        async with self._lock:
            self._allocated.pop(vm_id, None)


ipam = IPAM()
