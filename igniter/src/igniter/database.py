import logging
from datetime import UTC
from datetime import datetime

from anyio import Path as AsyncPath
from peewee import ForeignKeyField
from peewee import IPField
from peewee import TextField
from playhouse.pwasyncio import AsyncModel  # pyright: ignore[reportMissingTypeStubs]
from playhouse.pwasyncio import AsyncSqliteDatabase  # pyright: ignore[reportMissingTypeStubs]

logger: logging.Logger = logging.getLogger("tussilago")


DB_DIR: AsyncPath = AsyncPath("/var/lib/tussilago/")
DB_FILE: AsyncPath = DB_DIR / "ipam.db"


ipam_db = AsyncSqliteDatabase(
    str(DB_FILE),
    pragmas={
        "journal_mode": "wal",
        "foreign_keys": 1,
        "busy_timeout": 30_000,
    },
)


class TenantNetwork(AsyncModel):
    """Private IPv4 subnet allocated to a tenant on this Igniter host.

    Each tenant receives a unique /24 network. The first usable address
    is reserved for the tenant gateway.
    """

    tenant_id: TextField = TextField(
        primary_key=True,
        help_text="Unique identifier of the tenant owning this network.",
    )

    network_address: IPField = IPField(
        unique=True,
        help_text="IPv4 network address of the tenant's /24 subnet (e.g. 10.100.0.0).",
    )

    gateway: IPField = IPField(
        unique=True,
        help_text="IPv4 gateway address assigned to the tenant (e.g. 10.100.0.1).",
    )

    created_at: TextField = TextField(
        help_text="Creation timestamp (UTC ISO 8601)",
        default=lambda: datetime.now(UTC).isoformat(),
    )

    class Meta:
        database: AsyncSqliteDatabase = ipam_db
        table_name: str = "tenant_networks"


class IPAddress(AsyncModel):
    """IP address allocated to a microVM."""

    tenant: ForeignKeyField[TenantNetwork] = ForeignKeyField(
        TenantNetwork,
        backref="addresses",
        on_delete="CASCADE",
        lazy_load=False,
        help_text="Tenant network to which this IP address belongs.",
    )

    vm_id: TextField = TextField(
        help_text="Unique identifier of the microVM within the tenant.",
    )

    address: IPField = IPField(
        unique=True,
        help_text="IPv4 address assigned to this microVM (e.g. 10.100.0.2).",
    )

    class Meta:
        database: AsyncSqliteDatabase = ipam_db
        indexes = ((("tenant", "vm_id"), True),)
        table_name: str = "ip_addresses"


async def setup_database() -> None:
    """Run database initialization, creating tables if they do not exist."""
    await DB_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)

    async with ipam_db:
        logger.info("Creating TenantNetwork tables.")
        await ipam_db.create_tables([TenantNetwork])  # pyright: ignore[reportUnknownMemberType, reportGeneralTypeIssues]


async def close_database() -> None:
    """Close all database connections."""
    await ipam_db.close_pool()
