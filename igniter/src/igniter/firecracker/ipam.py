from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import ipaddress

logger: logging.Logger = logging.getLogger("tussilago")


@dataclass(slots=True, frozen=True)
class TenantNetwork:
    """Network allocation assigned to a tenant on this Igniter host."""

    tenant_id: str
    subnet: ipaddress.IPv4Network
    gateway: ipaddress.IPv4Address
