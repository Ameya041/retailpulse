"""Tests for the bcrypt cost-factor guard.

The work factor is configurable so the test suite can run fast. That is a
security-relevant knob, so it needs a test proving it cannot be turned down
outside a test environment.
"""

from __future__ import annotations

import importlib
import os

import pytest


def _rounds_under(env: dict[str, str]) -> int:
    """Re-import the auth module with a patched environment."""
    original = {k: os.environ.get(k) for k in ("ENVIRONMENT", "BCRYPT_ROUNDS")}
    try:
        for key, value in env.items():
            os.environ[key] = value
        for key in ("ENVIRONMENT", "BCRYPT_ROUNDS"):
            if key not in env:
                os.environ.pop(key, None)

        import retailpulse_common.auth as auth_module

        importlib.reload(auth_module)
        return auth_module._resolve_rounds()
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import retailpulse_common.auth as auth_module

        importlib.reload(auth_module)


def test_test_environment_may_lower_the_cost_factor():
    assert _rounds_under({"ENVIRONMENT": "test", "BCRYPT_ROUNDS": "4"}) == 4


@pytest.mark.parametrize("environment", ["production", "staging", "local", "PRODUCTION"])
def test_non_test_environments_always_use_the_full_cost_factor(environment):
    """A low BCRYPT_ROUNDS must be ignored outside tests."""
    assert _rounds_under({"ENVIRONMENT": environment, "BCRYPT_ROUNDS": "4"}) == 12


def test_missing_environment_defaults_to_the_full_cost_factor():
    assert _rounds_under({"BCRYPT_ROUNDS": "4"}) == 12


def test_cost_factor_cannot_be_raised_above_the_default_by_accident():
    """Clamped at 12 -- a typo'd 120 would make every login take minutes."""
    assert _rounds_under({"ENVIRONMENT": "test", "BCRYPT_ROUNDS": "120"}) == 12


def test_garbage_cost_factor_falls_back_to_the_default():
    assert _rounds_under({"ENVIRONMENT": "test", "BCRYPT_ROUNDS": "not-a-number"}) == 12


def test_cost_factor_has_a_hard_floor():
    assert _rounds_under({"ENVIRONMENT": "test", "BCRYPT_ROUNDS": "1"}) == 4
