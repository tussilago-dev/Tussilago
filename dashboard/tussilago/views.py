from __future__ import annotations

from typing import TYPE_CHECKING
from typing import cast

from allauth.account.models import EmailAddress
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.shortcuts import render
from django.views.decorators.http import require_POST

from tussilago.forms import CreateOrganizationForm
from tussilago.forms import InviteToOrganizationForm
from tussilago.models import Organization
from tussilago.models import OrganizationInvitation
from tussilago.models import OrganizationMember
from tussilago.models import User

if TYPE_CHECKING:
    from django.db.models.manager import BaseManager
    from django.http import HttpRequest
    from django.http.response import HttpResponse


# MARK: Helpers
def _get_user_organization(user: User, organization_id: int) -> Organization:
    """Fetch an organization the user belongs to, or raise a 404."""
    return get_object_or_404(Organization, pk=organization_id, members__user=user)


def _get_valid_invitation(user: User, token: str) -> OrganizationInvitation:
    """Validate and return an unaccepted invitation addressed to the current user.

    Raises:
        Http404: If the invitation does not exist or has already been accepted.
        PermissionDenied: If the user lacks a verified email or the invitation email differs.
    """  # ruff: ignore[docstring-extraneous-exception]
    invitation: OrganizationInvitation = get_object_or_404(OrganizationInvitation, token=token, is_accepted=False)

    has_verified_email: bool = EmailAddress.objects.filter(user=user, email=user.email, verified=True).exists()
    if not has_verified_email:
        msg = "You must have a verified email address to accept this invitation."
        raise PermissionDenied(msg)

    if invitation.email.casefold() != user.email.casefold():
        msg = "This invitation was sent to a different email address."
        raise PermissionDenied(msg)

    return invitation


# MARK: Index
def index(request: HttpRequest) -> HttpResponse:
    """Render the index page."""
    return render(request, "index.html")


# MARK: Profile
@login_required
def profile(request: HttpRequest) -> HttpResponse:
    """Render the profile page."""
    return render(request, "profile.html")


# MARK: Organizations
@login_required
def organizations(request: HttpRequest) -> HttpResponse:
    """Render the organizations page with the user's memberships."""
    memberships: BaseManager = (
        OrganizationMember.objects
        .filter(user=request.user)
        .select_related("organization")
        .order_by("organization__name")
    )
    return render(request, "organizations.html", {"memberships": memberships})


# MARK: Org details & update
@login_required
def organization_detail(request: HttpRequest, organization_id: int) -> HttpResponse:
    """Render the detail page for a specific organization."""
    user: User = cast("User", request.user)
    organization: Organization = _get_user_organization(user, organization_id)
    return render(request, "organization_detail.html", {"organization": organization})


@login_required
def organization_update(request: HttpRequest, organization_id: int) -> HttpResponse:
    """Handle updating an organization's details."""
    user: User = cast("User", request.user)
    organization: Organization = _get_user_organization(user, organization_id)

    if request.method == "POST":
        form = CreateOrganizationForm(request.POST)
        if form.is_valid():
            organization.name = form.cleaned_data["name"]
            organization.save()
            messages.success(request, "Organization updated successfully.")
            return redirect("organization_detail", organization_id=organization.id)
    else:
        form = CreateOrganizationForm(initial={"name": organization.name})

    return render(
        request,
        "organization_update.html",
        {"organization": organization, "form": form},
    )


# MARK: Org members
@login_required
def organization_members(request: HttpRequest, organization_id: int) -> HttpResponse:
    """Render the members page for a specific organization and handle invitations."""
    user: User = cast("User", request.user)
    organization: Organization = _get_user_organization(user, organization_id)

    if request.method == "POST":
        form = InviteToOrganizationForm(request.POST)
        if form.is_valid():
            form.save(request, organization)
            messages.success(request, "Invitation sent successfully.")
            return redirect("organization_members", organization_id=organization.id)
        messages.error(request, "Failed to send invitation.")
    else:
        form = InviteToOrganizationForm()

    context: dict[str, Organization | InviteToOrganizationForm | BaseManager] = {
        "organization": organization,
        "form": form,
        "members": OrganizationMember.objects.filter(organization=organization),
        "pending_invitations": OrganizationInvitation.objects.filter(organization=organization),
    }
    return render(request, "organization_members.html", context)


# MARK: Org creation
@login_required
def create_organization(request: HttpRequest) -> HttpResponse:
    """Handle the creation of a new organization."""
    user: User = cast("User", request.user)

    if request.method == "POST":
        form = CreateOrganizationForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                form.save(request, user)
            messages.success(request, "Organization created successfully.")
            return redirect("organizations")
    else:
        form = CreateOrganizationForm()

    return render(request, "create_organization.html", {"form": form})


# MARK: Org invitations
@login_required
def organization_invitation(request: HttpRequest, token: str) -> HttpResponse:
    """Render the page to handle an organization invitation."""
    user: User = cast("User", request.user)
    invitation: OrganizationInvitation = _get_valid_invitation(user, token)

    return render(request, "handle_org_invitation.html", {"invitation": invitation})


@login_required
@require_POST
def accept_organization_invitation(request: HttpRequest, token: str) -> HttpResponse:
    """Handle accepting an organization invitation."""
    user: User = cast("User", request.user)
    invitation: OrganizationInvitation = _get_valid_invitation(user, token)
    invitation.accept(user)

    messages.success(request, f"Joined {invitation.organization.name}.")
    return redirect("organization_detail", organization_id=invitation.organization.id)


@login_required
@require_POST
def decline_organization_invitation(request: HttpRequest, token: str) -> HttpResponse:
    """Handle declining an organization invitation."""
    user: User = cast("User", request.user)
    invitation: OrganizationInvitation = _get_valid_invitation(user, token)
    invitation.decline()

    messages.info(request, f"Declined invitation to {invitation.organization.name}.")
    return redirect("organizations")
