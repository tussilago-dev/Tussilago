from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http.response import HttpResponse
from django.shortcuts import redirect
from django.shortcuts import render

from tussilago.forms import CreateOrganizationForm
from tussilago.models import Organization
from tussilago.models import OrganizationMember

if TYPE_CHECKING:
    from django.http import HttpRequest
    from django.http import HttpResponse


def index_view(request: HttpRequest) -> HttpResponse:
    """Display the index page for the site."""
    return render(
        request,
        template_name="index.html",
        context={},
    )


@login_required
def profile_view(request: HttpRequest) -> HttpResponse:
    """Display the profile page for the logged-in user."""
    return render(
        request,
        template_name="profile.html",
        context={
            "user": request.user,
        },
    )


@login_required
def organizations(request: HttpRequest) -> HttpResponse:
    """Display the organizations page for the logged-in user."""
    memberships = (
        OrganizationMember.objects
        .filter(user=request.user)
        .select_related("organization")
        .order_by("organization__name")
    )

    if request.method == "POST":
        name: str = request.POST.get("name", "").strip()

        if name:
            organization: Organization = Organization.objects.create(
                name=name,
                slug=name.lower().replace(" ", "-"),
            )

            OrganizationMember.objects.create(
                organization=organization,
                user=request.user,
                role=OrganizationMember.Role.OWNER,
            )

            return redirect("organizations")

    return render(
        request,
        "organizations.html",
        {"memberships": memberships},
    )


@login_required
def organization_detail(request: HttpRequest, organization_id: str) -> HttpResponse:
    """Display the detail page for a specific organization."""
    organization = Organization.objects.get(id=organization_id)
    return render(
        request,
        "organization_detail.html",
        {"organization": organization},
    )


@login_required
def join_organization(request: HttpRequest, organization_id: str) -> HttpResponse:
    """Allow the logged-in user to join a specific organization."""
    organization = Organization.objects.get(id=organization_id)

    if request.method == "POST":
        OrganizationMember.objects.get_or_create(
            organization=organization,
            user=request.user,
            defaults={"role": OrganizationMember.Role.MEMBER},
        )
        return redirect("organization_detail", organization_id=organization.id)

    return render(
        request,
        "join_organization.html",
        {"organization": organization},
    )


@login_required
def create_organization(request: HttpRequest) -> HttpResponse:
    """Allow the logged-in user to create a new organization."""
    if request.method == "POST":
        form = CreateOrganizationForm(request.POST)
        if form.is_valid():
            form.save(request, request.user)  # pyright: ignore[reportArgumentType]
            messages.success(request, "Organization created successfully.")

            return redirect("organizations")
    else:
        form = CreateOrganizationForm()

    return render(
        request,
        "create_organization.html",
        {
            "form": form,
        },
    )
