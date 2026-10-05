import pytest

from ghost_api.config import Settings


def test_defaults() -> None:
    s = Settings(url="https://example.com", goal="x")
    assert s.lang == "python"
    assert s.max_steps == 40 and s.max_llm_calls == 50
    assert s.rate_limit == 1.0


@pytest.mark.parametrize(
    "kwargs", [{"lang": "cobol"}, {"rate_limit": 0}, {"max_steps": 0}, {"max_llm_calls": 0}]
)
def test_invalid_settings(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        Settings(url="https://example.com", goal="x", **kwargs)  # type: ignore[arg-type]
