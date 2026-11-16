"""Tests for `Key`."""

import pytest

from pycordis.keys import Key


def test_key_equals_and_hashes_like_its_name() -> None:
    key = Key[int]("db")

    assert key == "db"
    assert key == Key[str]("db")
    assert key != Key[int]("cache")
    assert key != 3
    assert hash(key) == hash("db")
    assert {"db": 1}[key] == 1
    assert key.name == "db"


def test_key_rejects_a_name_that_is_not_a_string() -> None:
    with pytest.raises(TypeError, match="must be a str"):
        Key(3)  # type: ignore[arg-type]
