"""Tests for ``LoginProxy._needs_sso`` / ``_has_valid_local_character_binding``.

Regression coverage for: a local character whose bound account no longer exists
(the account was renamed/deleted, or the row was written by the since-fixed
auto-add heuristic) must not permanently block that name from falling back to
SSO resolution. Before this fix, any name present in
``config.LOCAL_CHARACTER_NAMES`` was unconditionally excluded from SSO,
so a broken local binding guaranteed a failed login (raw passthrough of a
name that isn't a real EQEmu account) even when the real SSO backend could
have resolved it correctly.
"""

from __future__ import annotations

from unittest import mock

import pytest

from p99_sso_login_proxy import config, local_characters


@pytest.fixture
def login_proxy():
    with mock.patch("p99_sso_login_proxy.ui.PROXY_STATS", new=mock.MagicMock()):
        from p99_sso_login_proxy import server as server_mod

        yield server_mod.LoginProxy()


def _seed_local_account(name: str, password: str = "pw") -> None:
    config.LOCAL_ACCOUNTS[name.lower()] = {"password": password, "aliases": []}


def _seed_local_character(name: str, account: str) -> None:
    local_characters.set_entry(
        {"name": name, "account": account, "class": None, "level": None, "bind": None, "park": None, "items": {}}
    )


def _seed_sso_eligible(username: str) -> None:
    config.ALL_CACHED_NAMES.append(username.lower())
    config.USER_API_TOKEN = "token"


@pytest.fixture(autouse=True)
def _reset_sso_globals():
    saved_names = list(config.ALL_CACHED_NAMES)
    saved_token = config.USER_API_TOKEN
    yield
    config.ALL_CACHED_NAMES[:] = saved_names
    config.USER_API_TOKEN = saved_token


def test_needs_sso_true_for_normal_sso_eligible_name(login_proxy):
    _seed_sso_eligible("someplayer")
    assert login_proxy._needs_sso("someplayer") is True


def test_needs_sso_false_for_valid_local_character_binding(login_proxy):
    _seed_local_account("kcleric2")
    _seed_local_character("curlyfri", "kcleric2")
    _seed_sso_eligible("curlyfri")

    assert login_proxy._needs_sso("curlyfri") is False


def test_needs_sso_falls_back_to_sso_when_local_binding_is_orphaned(login_proxy):
    # "curlyfri" has a local-character row, but the account it points to
    # ("kcleric2") doesn't exist - e.g. it was deleted/renamed, or the row
    # was written by the old, since-fixed auto-add heuristic. This must not
    # block SSO if the name is otherwise SSO-eligible.
    _seed_local_character("curlyfri", "kcleric2")
    assert "kcleric2" not in config.LOCAL_ACCOUNTS
    _seed_sso_eligible("curlyfri")

    assert login_proxy._needs_sso("curlyfri") is True


def test_needs_sso_false_when_orphaned_binding_and_not_sso_eligible(login_proxy):
    # No SSO token / not in the cache: falls through to the existing local
    # "unknown account -> passthrough" safety net, same as before this fix.
    _seed_local_character("curlyfri", "kcleric2")
    assert login_proxy._needs_sso("curlyfri") is False


def test_has_valid_local_character_binding_requires_known_account(login_proxy):
    assert login_proxy._has_valid_local_character_binding("nobody") is False

    _seed_local_character("curlyfri", "kcleric2")
    assert login_proxy._has_valid_local_character_binding("curlyfri") is False

    _seed_local_account("kcleric2")
    assert login_proxy._has_valid_local_character_binding("curlyfri") is True
