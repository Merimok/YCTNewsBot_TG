import sys
from pathlib import Path
import pytest

# Ensure repository root is on the import path so ``feeds`` resolves to the
# local module rather than any installed package with the same name.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from feeds import parse_interval

@pytest.mark.parametrize(
    "text,expected",
    [
        ("1h", 3600),
        ("2h 30m", 9000),
        ("40m", 2400),
    ],
)
def test_parse_interval(text, expected):
    assert parse_interval(text) == expected
