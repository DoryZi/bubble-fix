"""The sample app's own tests (run inside a copy: uv run --with pytest pytest -q)."""
from app import company_name


def test_known_company_has_a_name() -> None:
    assert company_name(1) == "Kestrel Data"
