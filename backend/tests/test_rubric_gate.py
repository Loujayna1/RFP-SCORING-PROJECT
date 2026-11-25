"""Issue #6: grading must not run against an unapproved (unlocked) rubric."""
import types
import pytest
from fastapi import HTTPException

from app.main import _enforce_rubric_locked
from app.core.config import settings


def _rfp(locked):
    return types.SimpleNamespace(rubric_locked_at=locked)


def test_locked_rubric_proceeds_no_override():
    assert _enforce_rubric_locked(_rfp("2026-01-01"), "r1", "Acme", False) is False


def test_unlocked_rubric_blocked_by_default(monkeypatch):
    monkeypatch.setattr(settings, "REQUIRE_LOCKED_RUBRIC", True)
    with pytest.raises(HTTPException) as e:
        _enforce_rubric_locked(_rfp(None), "r1", "Acme", acknowledge_unlocked=False)
    assert e.value.status_code == 409
    assert "not locked" in e.value.detail.lower()


def test_unlocked_rubric_allowed_with_explicit_override(monkeypatch):
    monkeypatch.setattr(settings, "REQUIRE_LOCKED_RUBRIC", True)
    # override returns True so the caller audits the deliberate choice
    assert _enforce_rubric_locked(_rfp(None), "r1", "Acme", acknowledge_unlocked=True) is True


def test_unlocked_rubric_allowed_when_enforcement_disabled(monkeypatch):
    monkeypatch.setattr(settings, "REQUIRE_LOCKED_RUBRIC", False)
    assert _enforce_rubric_locked(_rfp(None), "r1", "Acme", acknowledge_unlocked=False) is True
