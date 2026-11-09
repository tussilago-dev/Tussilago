# pyright: basic
from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass

from igniter.database import IPAddress
from igniter.database import TenantNetwork as TenantNetworkModel
from igniter.database import ipam_db

logger: logging.Logger = logging.getLogger("tussilago")


@dataclass(slots=True, frozen=True)
class TenantNetwork:
    """Network allocation assigned to a tenant on this Igniter host."""

    tenant_id: str
    subnet: ipaddress.IPv4Network
    gateway: ipaddress.IPv4Address


class IPAM:
    """Manage persistent tenant subnet and IP address allocations."""

    def __init__(
        self,
        pool: str,
        tenant_prefix: int = 24,
    ) -> None:
        """Initialize IPAM with an address pool and tenant subnet size.

        Raises:
            ValueError: When tenant_prefix is 0 or above 30.
        """
        self.pool = ipaddress.IPv4Network(pool)
        self.tenant_prefix: int = tenant_prefix

        if not self.pool.prefixlen <= tenant_prefix <= 30:  # ruff: ignore[magic-value-comparison]
            msg = "Tenant prefix must be between the pool prefix and /30."
            raise ValueError(msg)

    async def allocate_network(self, tenant_id: str) -> TenantNetwork:
        """Allocate a private subnet and gateway for a tenant.

        Existing allocations are returned unchanged.

        Raises:
            RuntimeError: No tenant subnets remain available.
        """
        async with ipam_db, ipam_db.atomic(lock_type="IMMEDIATE"):
            existing: TenantNetworkModel | None = await TenantNetworkModel.aget_or_none(tenant_id=tenant_id)

            if existing:
                existing_subnet: str = str(existing.subnet)
                existing_gateway: str = str(existing.gateway)

                return TenantNetwork(
                    tenant_id=tenant_id,
                    subnet=ipaddress.IPv4Network(existing_subnet),
                    gateway=ipaddress.IPv4Address(existing_gateway),
                )

            allocations: list[str] = await ipam_db.list(TenantNetworkModel.select(TenantNetworkModel.subnet))
            allocated = {network.subnet for network in allocations}

            subnet: ipaddress.IPv4Network | None = next(
                (
                    candidate
                    for candidate in self.pool.subnets(new_prefix=self.tenant_prefix)
                    if str(candidate) not in allocated
                ),
                None,
            )

            if subnet is None:
                msg = "No tenant subnets remain available."
                raise RuntimeError(msg)

            gateway = ipaddress.IPv4Address(int(subnet.network_address) + 1)

            await TenantNetworkModel.acreate(tenant_id=tenant_id, subnet=str(subnet), gateway=str(gateway))

            logger.info("Allocated network %s to tenant %s", subnet, tenant_id)

            return TenantNetwork(
                tenant_id=tenant_id,
                subnet=subnet,
                gateway=gateway,
            )

    async def allocate_address(
        self,
        tenant_id: str,
        vm_id: str,
    ) -> ipaddress.IPv4Address:
        """Allocate an unused IP address from a tenant's subnet.

        Existing allocations for the same VM are returned unchanged.

        Raises:
            RuntimeError: No addresses remain in the tenant network.
        """
        network: TenantNetwork = await self.allocate_network(tenant_id)

        async with ipam_db, ipam_db.atomic(lock_type="IMMEDIATE"):
            existing: IPAddress | None = await IPAddress.aget_or_none(
                (IPAddress.tenant == tenant_id) & (IPAddress.vm_id == vm_id),
            )

            if existing:
                return ipaddress.IPv4Address(existing.address)

            allocations = await ipam_db.list(IPAddress.select(IPAddress.address).where(IPAddress.tenant == tenant_id))
            allocated = {address.address for address in allocations}

            address = next(
                (
                    candidate
                    for candidate in network.subnet.hosts()
                    if candidate != network.gateway and str(candidate) not in allocated
                ),
                None,
            )

            if address is None:
                msg = f"No addresses remain in tenant network {network.subnet}."
                raise RuntimeError(msg)

            await IPAddress.acreate(
                tenant=tenant_id,
                vm_id=vm_id,
                address=str(address),
            )

            logger.info(
                "Allocated %s to VM %s (tenant %s)",
                address,
                vm_id,
                tenant_id,
            )

            return address

    async def release_address(
        self,
        tenant_id: str,
        vm_id: str,
    ) -> None:
        """Release the address assigned to a VM.

        Does nothing if the VM has no existing allocation.
        """
        async with ipam_db:
            await (
                IPAddress
                .delete()
                .where(
                    (IPAddress.tenant == tenant_id) & (IPAddress.vm_id == vm_id),
                )
                .aexecute()
            )

        logger.info(
            "Released address for VM %s (tenant %s)",
            vm_id,
            tenant_id,
        )

    async def reconcile(self) -> None:
        """Validate persisted allocations against the configured IPAM pool.

        Detect invalid subnets, overlapping networks, invalid gateways,
        and addresses outside their assigned tenant networks.

        Does not modify host networking.

        Raises:
            RuntimeError: Address references a missing tenant network.
            RuntimeError: Invalid address allocated to tenant.
        """
        async with ipam_db:
            networks: list[TenantNetworkModel] = await ipam_db.list(TenantNetworkModel.select())
            addresses: list[IPAddress] = await ipam_db.list(IPAddress.select())

        allocated_networks: list[ipaddress.IPv4Network] = []
        tenant_networks: dict[str, TenantNetwork] = {}

        for record in networks:
            subnet = ipaddress.IPv4Network(record.subnet)
            gateway = ipaddress.IPv4Address(record.gateway)

            if not subnet.subnet_of(self.pool):
                msg: str = f"Tenant {record.tenant_id} has subnet outside IPAM pool: {subnet}"
                raise RuntimeError(msg)

            if subnet.prefixlen != self.tenant_prefix:
                msg: str = f"Tenant {record.tenant_id} has unexpected subnet size: {subnet}"
                raise RuntimeError(msg)

            expected_gateway = ipaddress.IPv4Address(int(subnet.network_address) + 1)

            if gateway != expected_gateway:
                msg: str = f"Tenant {record.tenant_id} has invalid gateway: {gateway}"
                raise RuntimeError(msg)

            if any(subnet.overlaps(other) for other in allocated_networks):
                msg: str = f"Overlapping tenant network detected: {subnet}"
                raise RuntimeError(msg)

            allocated_networks.append(subnet)
            tenant_networks[record.tenant_id] = TenantNetwork(
                tenant_id=record.tenant_id,
                subnet=subnet,
                gateway=gateway,
            )

        for record in addresses:
            network: TenantNetwork | None = tenant_networks.get(record.tenant_id)

            if network is None:
                msg = f"Address {record.address} references a missing tenant network."
                raise RuntimeError(msg)

            address = ipaddress.IPv4Address(record.address)

            if address not in network.subnet or address in {
                network.gateway,
                network.subnet.network_address,
                network.subnet.broadcast_address,
            }:
                msg = f"Invalid address {address} allocated to tenant {record.tenant_id}."
                raise RuntimeError(msg)

        logger.info("IPAM reconciliation complete: %d networks, %d addresses", len(networks), len(addresses))


ipam = IPAM()
