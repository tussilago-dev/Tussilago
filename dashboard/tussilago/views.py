from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.decorators import login_required
from django.http.response import HttpResponse
from django.shortcuts import render

if TYPE_CHECKING:
    from django.http import HttpRequest
    from django.http import HttpResponse


def index_view(request: HttpRequest) -> HttpResponse:
    """Return the index page for the site."""
    return render(
        request,
        template_name="index.html",
        context={},
    )


@login_required
def profile_view(request: HttpRequest) -> HttpResponse:
    """Return the profile page for the logged-in user."""
    return render(
        request,
        template_name="profile.html",
        context={
            "user": request.user,
        },
    )
