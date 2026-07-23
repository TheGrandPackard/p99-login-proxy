"""Tests for :mod:`p99_sso_login_proxy.local_characters`.

Covers the state machine that pairs the most-recent local login with the next
new EQ log file, plus ``apply_update`` / ``set_entry`` / ``delete_entry``.
"""

from __future__ import annotations

from p99_sso_login_proxy import config, local_characters


def _seed_local_account(name: str = "acct1", password: str = "pw") -> None:
    """Register a local account so ``try_auto_create`` treats it as valid."""
    config.LOCAL_ACCOUNTS[name.lower()] = {"password": password, "aliases": []}


def _capture_candidates() -> list[tuple[str, str]]:
    """Subscribe a recorder to ON_AUTO_BIND_CANDIDATE and return the list it fills."""
    seen: list[tuple[str, str]] = []
    local_characters.ON_AUTO_BIND_CANDIDATE.append(lambda name, account: seen.append((name, account)))
    return seen


# ---- note_login -----------------------------------------------------------


def test_note_login_local_sets_pending():
    local_characters.note_login("local", "ACCT1")
    assert local_characters._pending_local_account == "acct1"


def test_note_login_local_char_sets_pending():
    local_characters.note_login("local_char", "acct1")
    assert local_characters._pending_local_account == "acct1"


def test_note_login_sso_clears_pending():
    local_characters.note_login("local", "acct1")
    assert local_characters._pending_local_account == "acct1"

    local_characters.note_login("sso", "acct1")
    assert local_characters._pending_local_account is None


def test_note_login_passthrough_clears_pending():
    local_characters.note_login("local", "acct1")
    local_characters.note_login("passthrough", "whatever")
    assert local_characters._pending_local_account is None


def test_note_login_local_with_no_account_clears_pending():
    local_characters.note_login("local", "acct1")
    local_characters.note_login("local", None)
    assert local_characters._pending_local_account is None


# ---- try_auto_create ------------------------------------------------------


def test_try_auto_create_no_pending_returns_false():
    assert local_characters.try_auto_create("Toald") is False
    assert "toald" not in config.LOCAL_CHARACTERS


def test_try_auto_create_surfaces_candidate_without_persisting():
    # try_auto_create no longer persists on its own - it must not add the row or
    # make the name usable for a credential rewrite until a human confirms it via
    # ON_AUTO_BIND_CANDIDATE / confirm_auto_bind.
    _seed_local_account("acct1")
    local_characters.note_login("local", "acct1")
    candidates = _capture_candidates()

    assert local_characters.try_auto_create("Toald") is True

    assert candidates == [("Toald", "acct1")]
    assert "toald" not in config.LOCAL_CHARACTER_NAMES
    assert "toald" not in config.LOCAL_CHARACTERS


def test_try_auto_create_local_char_method_seeds_pending():
    _seed_local_account("acct1")
    local_characters.note_login("local_char", "acct1")
    candidates = _capture_candidates()

    assert local_characters.try_auto_create("Toald") is True
    assert candidates == [("Toald", "acct1")]


def test_confirm_auto_bind_happy_path_adds_row():
    _seed_local_account("acct1")
    local_characters.note_login("local", "acct1")
    assert local_characters.try_auto_create("Toald") is True

    assert local_characters.confirm_auto_bind("Toald", "acct1") is True

    assert "toald" in config.LOCAL_CHARACTER_NAMES
    entry = config.LOCAL_CHARACTERS["toald"]
    assert entry["name"] == "Toald"
    assert entry["account"] == "acct1"
    assert entry["class"] is None
    assert entry["level"] is None


def test_confirm_auto_bind_rejects_removed_account():
    _seed_local_account("acct1")
    local_characters.note_login("local", "acct1")
    assert local_characters.try_auto_create("Toald") is True

    config.LOCAL_ACCOUNTS.pop("acct1", None)

    assert local_characters.confirm_auto_bind("Toald", "acct1") is False
    assert "toald" not in config.LOCAL_CHARACTER_NAMES


def test_confirm_auto_bind_rejects_character_bound_in_the_meantime():
    _seed_local_account("acct1")
    _seed_local_account("acct2")
    local_characters.note_login("local", "acct1")
    assert local_characters.try_auto_create("Toald") is True

    # Something else (e.g. a manual add) bound the name before the human confirmed.
    local_characters.set_entry(
        {"name": "Toald", "account": "acct2", "class": None, "level": None, "bind": None, "park": None, "items": {}}
    )

    assert local_characters.confirm_auto_bind("Toald", "acct1") is False
    assert config.LOCAL_CHARACTERS["toald"]["account"] == "acct2"


def test_pending_account_is_single_use():
    # A pending login must only ever be offered to one candidate, even if it's
    # declined/ignored - otherwise it can silently attach to every later
    # first-sighting until some unrelated login resets it.
    _seed_local_account("acct1")
    local_characters.note_login("local", "acct1")
    candidates = _capture_candidates()

    assert local_characters.try_auto_create("Toald") is True
    assert local_characters.try_auto_create("SomeoneElse") is False

    assert candidates == [("Toald", "acct1")]


def test_pending_account_expires_after_ttl(monkeypatch):
    _seed_local_account("acct1")
    local_characters.note_login("local", "acct1")
    candidates = _capture_candidates()

    # Simulate the pending marker aging past the TTL before a new file shows up.
    local_characters._pending_local_account_ts -= local_characters._PENDING_LOCAL_LOGIN_TTL_SECONDS + 1

    assert local_characters.try_auto_create("Toald") is False
    assert candidates == []


def test_pending_account_within_ttl_still_works():
    _seed_local_account("acct1")
    local_characters.note_login("local", "acct1")
    candidates = _capture_candidates()

    local_characters._pending_local_account_ts -= local_characters._PENDING_LOCAL_LOGIN_TTL_SECONDS - 1

    assert local_characters.try_auto_create("Toald") is True
    assert candidates == [("Toald", "acct1")]


def test_try_auto_create_disabled_by_config_flag():
    _seed_local_account("acct1")
    local_characters.note_login("local", "acct1")
    config.AUTO_ADD_LOCAL_CHARACTERS = False

    assert local_characters.try_auto_create("Toald") is False
    assert "toald" not in config.LOCAL_CHARACTER_NAMES


def test_try_auto_create_pending_account_missing_skips_and_warns(caplog):
    # Pending account exists on the slot but was since deleted from local_accounts.csv.
    local_characters.note_login("local", "ghost")
    config.LOCAL_ACCOUNTS.pop("ghost", None)

    with caplog.at_level("WARNING", logger="p99_sso_login_proxy.local_characters"):
        assert local_characters.try_auto_create("Toald") is False

    assert "toald" not in config.LOCAL_CHARACTER_NAMES
    assert any("no longer in local_accounts" in rec.message for rec in caplog.records)


def test_try_auto_create_collision_preserves_existing_binding(caplog):
    # Existing row bound to acctA.
    _seed_local_account("accta")
    _seed_local_account("acctb")
    local_characters.set_entry(
        {"name": "Toald", "account": "accta", "class": None, "level": None, "bind": None, "park": None, "items": {}}
    )
    # Log in as a different local account, then hit the auto-add path.
    local_characters.note_login("local", "acctb")

    with caplog.at_level("WARNING", logger="p99_sso_login_proxy.local_characters"):
        assert local_characters.try_auto_create("Toald") is False

    assert config.LOCAL_CHARACTERS["toald"]["account"] == "accta"
    assert any("not overwriting" in rec.message for rec in caplog.records)


def test_try_auto_create_collision_same_account_is_noop():
    _seed_local_account("acct1")
    local_characters.set_entry(
        {"name": "Toald", "account": "acct1", "class": None, "level": None, "bind": None, "park": None, "items": {}}
    )
    local_characters.note_login("local", "acct1")

    # No new row created, no warning — existing row already correct.
    assert local_characters.try_auto_create("Toald") is False
    assert config.LOCAL_CHARACTERS["toald"]["account"] == "acct1"


def test_try_auto_create_two_consecutive_characters():
    _seed_local_account("acct1")
    _seed_local_account("acct2")

    local_characters.note_login("local", "acct1")
    assert local_characters.try_auto_create("Alice") is True
    assert local_characters.confirm_auto_bind("Alice", "acct1") is True

    local_characters.note_login("local", "acct2")
    assert local_characters.try_auto_create("Bob") is True
    assert local_characters.confirm_auto_bind("Bob", "acct2") is True

    assert config.LOCAL_CHARACTERS["alice"]["account"] == "acct1"
    assert config.LOCAL_CHARACTERS["bob"]["account"] == "acct2"


def test_try_auto_create_empty_name_is_noop():
    _seed_local_account("acct1")
    local_characters.note_login("local", "acct1")

    assert local_characters.try_auto_create("") is False
    assert "" not in config.LOCAL_CHARACTER_NAMES


# ---- apply_update ---------------------------------------------------------


def test_apply_update_only_runs_for_known_characters():
    assert local_characters.apply_update("Unknown", klass="Cleric") is False
    assert "unknown" not in config.LOCAL_CHARACTERS


def test_apply_update_sets_class_level_bind_park():
    local_characters.set_entry(
        {"name": "Toald", "account": "acct1", "class": None, "level": None, "bind": None, "park": None, "items": {}}
    )

    changed = local_characters.apply_update(
        "Toald",
        klass="Cleric",
        level=60,
        bind="East Commonlands",
        park="Plane of Knowledge",
    )
    assert changed is True

    e = config.LOCAL_CHARACTERS["toald"]
    assert e["class"] == "Cleric"
    assert e["level"] == 60
    assert e["bind"] == "East Commonlands"
    assert e["park"] == "Plane of Knowledge"


def test_apply_update_no_change_returns_false():
    local_characters.set_entry(
        {"name": "Toald", "account": "acct1", "class": "Cleric", "level": 60, "bind": None, "park": None, "items": {}}
    )
    assert local_characters.apply_update("Toald", klass="Cleric", level=60) is False


def test_apply_update_merges_items():
    local_characters.set_entry(
        {
            "name": "Toald",
            "account": "acct1",
            "class": "Cleric",
            "level": 60,
            "bind": None,
            "park": None,
            "items": {"pearl": 5, "reaper": False},
        }
    )

    changed = local_characters.apply_update("Toald", items={"pearl": 12, "reaper": True, "lizard": 3})
    assert changed is True
    items = config.LOCAL_CHARACTERS["toald"]["items"]
    assert items["pearl"] == 12
    assert items["reaper"] is True
    assert items["lizard"] == 3


# ---- delete_entry ---------------------------------------------------------


def test_delete_entry_removes_from_name_set_and_dict():
    local_characters.set_entry(
        {"name": "Toald", "account": "acct1", "class": None, "level": None, "bind": None, "park": None, "items": {}}
    )
    assert "toald" in config.LOCAL_CHARACTER_NAMES

    assert local_characters.delete_entry("Toald") is True
    assert "toald" not in config.LOCAL_CHARACTERS
    assert "toald" not in config.LOCAL_CHARACTER_NAMES
    assert local_characters.delete_entry("Toald") is False
