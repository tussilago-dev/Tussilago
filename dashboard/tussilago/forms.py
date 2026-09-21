from __future__ import annotations

from typing import TYPE_CHECKING

from django import forms

from tussilago.models import Organization
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
        )

        OrganizationMember.objects.create(
            user=user,
            organization=organization,
        )
