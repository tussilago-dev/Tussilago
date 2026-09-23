from __future__ import annotations

from typing import TYPE_CHECKING

from django import forms
from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse

from tussilago.models import Organization
from tussilago.models import OrganizationInvitation
from tussilago.models import OrganizationMember

if TYPE_CHECKING:
    from django.http import HttpRequest

    from tussilago.models import User


class SignUpForm(forms.Form):
    name = forms.CharField(max_length=60, help_text="User's full name. Username will be used if not provided.")

    def signup(self, request: HttpRequest, user: User) -> None:
        """Called after user is created, but before the user is saved."""
        user.name = self.cleaned_data["name"]

        user.save()


class CreateOrganizationForm(forms.Form):
    name = forms.CharField(max_length=100, help_text="Organization name.")
    description = forms.CharField(
        widget=forms.Textarea,
        help_text="Organization description.",
        required=False,
    )

    def save(self, request: HttpRequest, user: User) -> None:
        """Create a new organization and add the user as a member."""
        organization: Organization = Organization.objects.create(
            name=self.cleaned_data["name"],
            description=self.cleaned_data["description"],
            slug=self.cleaned_data["name"],
        )

        OrganizationMember.objects.create(
            user=user,
            organization=organization,
        )


class InviteToOrganizationForm(forms.Form):
    email = forms.EmailField(help_text="Email address of the user to invite.")

    def save(self, request: HttpRequest, organization: Organization) -> None:
        """Create a new organization invitation."""
        invitation: OrganizationInvitation = OrganizationInvitation.objects.create(
            organization=organization,
            email=self.cleaned_data["email"],
            invited_by=request.user,
            is_accepted=False,
        )

        invitation_path: str = reverse("handle_org_invitation", kwargs={"token": invitation.token})
        invitation_url: str = request.build_absolute_uri(invitation_path)

        send_mail(
            subject=f"Tussilago: Invitation to join {organization.name}",
            html_message=(
                f"<p>Hello,</p>"
                f"<p>You have been invited to join {organization.name}.</p>"
                f"<p>Please click the link below to accept or decline the invitation.</p>"
                f"<p><a href='{invitation_url}'>Accept or decline the invitation</a></p>"
                f"<p>If you did not expect this invitation, you can safely ignore this email.</p>"
                f"<p>Best regards,<br>The Tussilago Team</p>"
            ),
            message=(
                f"Hello,\n\n"
                f"You have been invited to join {organization.name}.\n\n"
                f"Please click the link below to accept or decline the invitation.\n\n"
                f"Invitation link:\n{invitation_url}\n\n"
                f"If you did not expect this invitation, you can safely ignore this email.\n\n"
                f"Best regards,\n"
                f"The Tussilago Team"
                f"\n"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[invitation.email],
        )
