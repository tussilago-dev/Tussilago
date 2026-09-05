from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st

from config import settings_helpers

if TYPE_CHECKING:
    from pathlib import Path

    from _pytest.monkeypatch import MonkeyPatch


@given(
    segment=st.text(
        alphabet=st.characters(
            whitelist_categories=("Ll", "Lu", "Nd"),
            whitelist_characters="._-",
        ),
        min_size=1,
        max_size=12,
    ),
)
def test_get_data_dir_uses_environment_variable(segment: str, tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    custom_dir: Path = tmp_path / segment
    monkeypatch.setenv(
        name="TUSSILAGO_DATA_DIR",
        value=str(custom_dir),
    )

    result: Path = settings_helpers.get_data_dir()

    assert result == custom_dir
    assert result.is_dir()


def test_get_data_dir_uses_platformdirs_default(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    default_dir: Path = tmp_path / "default-data-dir"
    monkeypatch.delenv(name="TUSSILAGO_DATA_DIR", raising=False)
    monkeypatch.setattr(
        target=settings_helpers,
        name="user_data_path",
        value=lambda **kwargs: default_dir,  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]  # ruff: ignore[unused-lambda-argument]
    )

    result: Path = settings_helpers.get_data_dir()

    assert result == default_dir
    assert result.is_dir()
