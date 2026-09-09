from contextlib import nullcontext
from types import SimpleNamespace

import pytest

import run_report


@pytest.mark.parametrize("configured,expected", [(None, 604800), ("86400", 86400)])
def test_beta_default_and_explicit_override(monkeypatch, configured, expected):
    monkeypatch.delenv("PLUM_DA_ATTRIBUTION_SECONDS", raising=False)
    if configured:
        monkeypatch.setenv("PLUM_DA_ATTRIBUTION_SECONDS", configured)
    observed = []
    monkeypatch.setattr(run_report.db, "connect",
                        lambda: nullcontext(SimpleNamespace(commit=lambda: None)))
    monkeypatch.setattr(run_report, "apply_migrations", lambda conn: [])
    monkeypatch.setattr(run_report, "project",
                        lambda conn, seconds, **kwargs: observed.append(seconds))
    assert run_report.main(["--only", "project"]) == 0
    assert observed == [expected]


def test_invalid_config_fails_before_connecting(monkeypatch):
    monkeypatch.setenv("PLUM_DA_ATTRIBUTION_SECONDS", "0")
    monkeypatch.setattr(run_report.db, "connect", lambda: pytest.fail("must not connect"))
    with pytest.raises(SystemExit) as error:
        run_report.main(["--only", "project"])
    assert error.value.code == 2
