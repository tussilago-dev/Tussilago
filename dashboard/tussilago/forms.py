from __future__ import annotations

from typing import TYPE_CHECKING

from django import forms

if TYPE_CHECKING:
    from django.http import HttpRequest

    from tussilago.models import User


class SignUpForm(forms.Form):
    name = forms.CharField(max_length=60, help_text="User's full name. Username will be used if not provided.")

    def signup(self, request: HttpRequest, user: User) -> None:
        """Called after user is created, but before the user is saved."""
        user.name = self.cleaned_data["name"]

        user.save()
