from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from typing import cast

from allauth.account.models import EmailAddress
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import DetailView
from django.views.generic import FormView
from django.views.generic import ListView
from django.views.generic import TemplateView
from django.views.generic.edit import FormMixin

from tussilago.forms import CreateOrganizationForm
from tussilago.forms import InviteToOrganizationForm
from tussilago.models import Organization
from tussilago.models import OrganizationInvitation
from tussilago.models import OrganizationMember
from tussilago.models import User

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from django.http import HttpRequest
    from django.http.response import HttpResponse


class OrganizationAccessMixin(LoginRequiredMixin, View):
    """Restricts access to organizations the current user belongs to."""

    pk_url_kwarg: str = "organization_id"

    def get_queryset(self) -> QuerySet[Organization]:
        # Users can only query organizations they are a member of
        return Organization.objects.filter(members__user=self.request.user)


# MARK: Index
class IndexView(TemplateView):
    template_name = "index.html"


# MARK: Profile
class ProfileView(LoginRequiredMixin, TemplateView):
    template_name = "profile.html"


# MARK: Organizations
class OrganizationsView(LoginRequiredMixin, ListView):
    template_name = "organizations.html"
    context_object_name = "memberships"

    def get_queryset(self) -> QuerySet[OrganizationMember]:
        return (
            OrganizationMember.objects
            .filter(user=self.request.user)
            .select_related("organization")
            .order_by("organization__name")
        )


# MARK: Org details
class OrganizationDetailView(OrganizationAccessMixin, DetailView):
    model = Organization
    template_name = "organization_detail.html"
    context_object_name = "organization"


# MARK: Org members
class OrganizationMembersView(OrganizationAccessMixin, FormMixin, DetailView):
    model = Organization
    template_name = "organization_members.html"
    context_object_name = "organization"
    form_class = InviteToOrganizationForm
    object: Organization

    def get_success_url(self) -> str:
        return reverse_lazy("organization_members", kwargs={"organization_id": self.object.id})

    def get_object(self, queryset: QuerySet[Organization] | None = None) -> Organization:
        return cast("Organization", super().get_object(queryset))

    def form_valid(self, form: InviteToOrganizationForm) -> HttpResponse:
        messages.success(self.request, "Invitation sent successfully.")
        return super().form_valid(form)

    def form_invalid(self, form: InviteToOrganizationForm) -> HttpResponse:
        messages.error(self.request, "Failed to send invitation.")
        return super().form_invalid(form)

    def get_context_data(self, **kwargs: str) -> dict[str, Any]:
        context: dict[str, Any] = super().get_context_data(**kwargs)
        context["members"] = OrganizationMember.objects.filter(organization=self.object)
        context["pending_invitations"] = OrganizationInvitation.objects.filter(organization=self.object)
        context.setdefault("form", self.get_form())
        return context

    def post(self, request: HttpRequest, *args: str, **kwargs: str) -> HttpResponse:
        self.object = self.get_object()
        form: InviteToOrganizationForm = self.get_form()  # pyright: ignore[reportAssignmentType]
        if form.is_valid():
            form.save(request, self.object)
            return self.form_valid(form)
        return self.form_invalid(form)


# MARK: Org creation
class CreateOrganizationView(LoginRequiredMixin, FormView):
    form_class = CreateOrganizationForm
    template_name = "create_organization.html"
    success_url = reverse_lazy("organizations")

    def form_valid(self, form: CreateOrganizationForm) -> HttpResponse:
        if not isinstance(self.request.user, User):
            msg = "You must be signed in to accept this invitation."
            raise PermissionDenied(msg)

        with transaction.atomic():
            form.save(self.request, self.request.user)

        messages.success(self.request, "Organization created successfully.")
        return redirect(self.get_success_url())


# MARK: Org invitations
class OrganizationInvitationView(LoginRequiredMixin, DetailView):
    model = OrganizationInvitation
    slug_field = "token"
    slug_url_kwarg = "token"
    template_name = "handle_org_invitation.html"
    context_object_name = "invitation"

    def get_queryset(self) -> QuerySet[OrganizationInvitation]:
        return OrganizationInvitation.objects.filter(is_accepted=False)

    def get_object(self, queryset: QuerySet[OrganizationInvitation] | None = None) -> OrganizationInvitation:
        invitation: OrganizationInvitation = super().get_object(queryset)  # pyright: ignore[reportAssignmentType]

        # Validate that the user is signed in.
        if not isinstance(self.request.user, User):
            msg = "You must be signed in to accept this invitation."
            raise PermissionDenied(msg)

        # Validate that the user's email is verified.
        user: User = self.request.user
        email: str = self.request.user.email
        self.validate_user_email_verification(user=user, email=email)

        # Validate that the invitation email matches the user's email.
        self.validate_invitation_email(invitation_email=invitation.email, user_email=self.request.user.email)

        return invitation

    def validate_invitation_email(self, invitation_email: str, user_email: str) -> None:
        """Validates that the invitation email matches the user's email in a case-insensitive manner.

        Args:
            invitation_email (str): The email address on the invitation.
            user_email (str): The email address of the user.

        Raises:
            PermissionDenied: If the invitation email does not match the user's email.
        """
        if invitation_email.casefold() != user_email.casefold():
            msg = "This invitation was sent to a different email address."
            raise PermissionDenied(msg)

    def validate_user_email_verification(self, user: User, email: str) -> None:
        """Validates that the user has a verified email address.

        Args:
            user (User): The user instance to validate.
            email (str): The email address of the user.

        Raises:
            PermissionDenied: If the user is not signed in.
            PermissionDenied: If the user does not have a verified email address.
        """
        email_address: EmailAddress | None = EmailAddress.objects.filter(user=user, email=email, verified=True).first()
        if not email_address:
            msg = "You must have a verified email address to accept this invitation."
            raise PermissionDenied(msg)


class AcceptOrganizationInvitationView(OrganizationInvitationView):
    def post(self, request: HttpRequest, *args: str, **kwargs: str) -> HttpResponse:
        invitation: OrganizationInvitation = self.get_object()

        user: User = cast("User", request.user)
        invitation.accept(user)

        messages.success(request, f"Joined {invitation.organization.name}.")
        return redirect("organization_detail", organization_id=invitation.organization.id)


class DeclineOrganizationInvitationView(OrganizationInvitationView):
    def post(self, request: HttpRequest, *args: str, **kwargs: str) -> HttpResponse:
        invitation: OrganizationInvitation = self.get_object()
        invitation.decline()

        messages.info(request, f"Declined invitation to {invitation.organization.name}.")

        return redirect("organizations")
