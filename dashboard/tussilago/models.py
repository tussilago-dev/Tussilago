from __future__ import annotations

import logging
import secrets
import typing
import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db import transaction
from django.utils.text import slugify

logger: logging.Logger = logging.getLogger("tussilago")


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=60, help_text="User's full name. Username will be used if not provided.")


class Organization(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    description = models.TextField(help_text="Organization's description.", blank=True)

    slug = models.SlugField(unique=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.name

    def save(
        self,
        *,
        force_insert: bool = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: typing.Iterable[str] | None = None,
    ) -> None:
        """Automatically generate the slug from the organization name if not provided."""
        if not self.slug:
            self.slug = slugify(self.name)

        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )


class OrganizationMember(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        MEMBER = "member", "Member"

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="members",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="organization_memberships",
    )
    role = models.CharField(
        max_length=20,
        choices=Role,
        default=Role.MEMBER,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Ensure that each user can only be a member of an organization once.
        constraints: typing.ClassVar[list[models.UniqueConstraint]] = [
            models.UniqueConstraint(
                fields=["organization", "user"],
                name="unique_organization_member",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user} ({self.role}) in {self.organization}"


def generate_token() -> str:
    """Generates a secure token for the organization invitation."""
    return secrets.token_urlsafe(32)


class OrganizationInvitation(models.Model):
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="invitations",
    )
    email = models.EmailField()
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sent_invitations",
    )
    token = models.CharField(max_length=255, default=generate_token, editable=False, unique=True)
    is_accepted = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints: typing.ClassVar[list[models.UniqueConstraint]] = [
            models.UniqueConstraint(
                fields=["organization", "email"],
                name="unique_organization_invitation",
            ),
        ]

    def __str__(self) -> str:
        return f"Invitation for {self.email} to join {self.organization} (Accepted: {self.is_accepted})"

    def accept(self, user: User) -> OrganizationMember:
        with transaction.atomic():
            invitation: OrganizationInvitation = OrganizationInvitation.objects.select_for_update().get(pk=self.pk)

            if invitation.is_accepted:
                msg = "Invitation has already been accepted."
                raise ValueError(msg)

            member, created = OrganizationMember.objects.get_or_create(
                organization=invitation.organization,
                user=user,
                defaults={"role": OrganizationMember.Role.MEMBER},
            )

            invitation.is_accepted = True
            invitation.save(update_fields=["is_accepted"])

        logger.info("Organization invitation accepted: %s, member created: %s", invitation, created)

        return member

    def decline(self) -> None:
        with transaction.atomic():
            invitation: OrganizationInvitation = OrganizationInvitation.objects.select_for_update().get(pk=self.pk)

            if invitation.is_accepted:
                msg = "Invitation has already been accepted and cannot be declined."
                raise ValueError(msg)

            invitation.delete()

        logger.info("Organization invitation declined: %s", self)
