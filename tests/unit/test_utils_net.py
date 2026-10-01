"""Unit tests for app.utils.net."""
from __future__ import annotations

import pytest

from app.utils.net import ip_version


@pytest.mark.unit
@pytest.mark.parametrize(("address", "expected"), [
    ("192.0.2.10", 4),
    ("2001:db8::10", 6),
    ("not-an-ip", None),
    ("", None),
])
def test_ip_version(address: str, expected: int | None) -> None:
    assert ip_version(address) == expected
