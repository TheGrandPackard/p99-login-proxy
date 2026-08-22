"""Tests for uploading EQ inventory/spellbook dumps to the SSO API.

Covers the encoding helper and the watchdog handler's two guards: the settle
delay (EQ's write produces a burst of events, and reading mid-write would ship a
truncated inventory) and the content-hash dedup (every upload costs the server a
delete-and-reinsert of the character's rows).
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from watchdog.events import FileCreatedEvent, FileModifiedEvent

from p99_sso_login_proxy import config, log_handler, shared_file_upload

INVENTORY_DUMP = (
    "Location\tName\tID\tCount\tSlots\n"
    "General1-Slot1\tTrakanon Idol\t1810\t1\t5\n"
    "General2-Slot1\tLizard Blood Potion\t28244\t12\t5\n"
)


@pytest.fixture
def uploads(monkeypatch):
    """Capture send_shared_file calls.

    The handler runs on a watchdog/timer thread and hands its coroutine to
    ``_run_async``, which needs the app's asyncio loop. Run it inline instead.
    """
    sent: list[tuple[str, str, str]] = []

    async def fake_send(filename, content, encoding):
        sent.append((filename, content, encoding))

    monkeypatch.setattr(log_handler.ws_client, "send_shared_file", fake_send)
    monkeypatch.setattr(log_handler.ws_client, "is_connected", lambda: True)
    monkeypatch.setattr(log_handler, "_run_async", asyncio.run)
    return sent


@pytest.fixture
def sso_character(monkeypatch):
    """Make "Bumperking" an SSO-tracked character."""
    monkeypatch.setattr(config, "USER_API_TOKEN", "token")
    monkeypatch.setattr(config, "CHARACTERS_CACHED", ["bumperking"])


def _write(tmp_path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


# --- encoding helper -------------------------------------------------------


def test_read_and_encode_roundtrips(tmp_path):
    path = _write(tmp_path, "Bumperking-Inventory.txt", INVENTORY_DUMP)

    digest, payload = shared_file_upload.read_and_encode(path)

    assert gzip.decompress(base64.b64decode(payload)).decode("utf-8") == INVENTORY_DUMP
    # Same content must produce the same digest and the same bytes, so an
    # unchanged rewrite is detectable.
    assert shared_file_upload.read_and_encode(path) == (digest, payload)


def test_read_and_encode_missing_file_returns_none(tmp_path):
    assert shared_file_upload.read_and_encode(str(tmp_path / "nope.txt")) is None


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("Bumperking-Inventory.txt", "Bumperking"),
        ("Bumperking-Spellbook.txt", "Bumperking"),
        ("bumperking-inventory.txt", "bumperking"),
        ("Bumperking-Bank.txt", ""),
        ("eqlog_Bumperking_project1999.txt", ""),
    ],
)
def test_character_name_from_path(filename, expected):
    assert shared_file_upload.character_name_from_path(filename) == expected
    assert shared_file_upload.is_uploadable_file_path(filename) == bool(expected)


# --- handler ---------------------------------------------------------------


def test_write_burst_collapses_to_one_upload(tmp_path, uploads, sso_character, monkeypatch):
    """EQ's single /outputfile run arrives as a create plus several modifies."""
    monkeypatch.setattr(log_handler, "SHARED_FILE_SETTLE_SECONDS", 0.05)
    path = _write(tmp_path, "Bumperking-Inventory.txt", INVENTORY_DUMP)
    handler = log_handler.SharedFileHandler()

    handler.on_created(FileCreatedEvent(path))
    handler.on_modified(FileModifiedEvent(path))
    timer = handler._timers[path]  # each event replaced the previous timer
    handler.on_modified(FileModifiedEvent(path))
    assert handler._timers[path] is not timer

    handler._timers[path].join(2.0)

    assert len(uploads) == 1
    filename, payload, encoding = uploads[0]
    assert filename == "Bumperking-Inventory.txt"
    assert encoding == shared_file_upload.WIRE_ENCODING
    assert gzip.decompress(base64.b64decode(payload)).decode("utf-8") == INVENTORY_DUMP


def test_unchanged_rewrite_is_not_reuploaded(tmp_path, uploads, sso_character):
    path = _write(tmp_path, "Bumperking-Inventory.txt", INVENTORY_DUMP)
    handler = log_handler.SharedFileHandler()

    handler._process(path)
    assert len(uploads) == 1

    # Rewriting identical content must not cost the server another
    # delete-and-reinsert of the character's rows.
    _write(tmp_path, "Bumperking-Inventory.txt", INVENTORY_DUMP)
    handler._process(path)
    assert len(uploads) == 1

    # Real changes still go out.
    _write(tmp_path, "Bumperking-Inventory.txt", INVENTORY_DUMP + "General3-Slot1\tPearl\t1\t20\t5\n")
    handler._process(path)
    assert len(uploads) == 2


def test_spellbook_is_uploaded(tmp_path, uploads, sso_character):
    path = _write(tmp_path, "Bumperking-Spellbook.txt", "1\tMinor Healing\n")

    log_handler.SharedFileHandler()._process(path)

    assert [f for f, _, _ in uploads] == ["Bumperking-Spellbook.txt"]


def test_untracked_character_is_not_uploaded(tmp_path, uploads, sso_character):
    path = _write(tmp_path, "Someoneelse-Inventory.txt", INVENTORY_DUMP)

    log_handler.SharedFileHandler()._process(path)

    assert uploads == []


def test_disconnected_upload_is_retried_on_next_write(tmp_path, uploads, sso_character, monkeypatch):
    path = _write(tmp_path, "Bumperking-Inventory.txt", INVENTORY_DUMP)
    handler = log_handler.SharedFileHandler()

    monkeypatch.setattr(log_handler.ws_client, "is_connected", lambda: False)
    handler._process(path)
    assert uploads == []

    # The dump wasn't sent, so it must not be remembered as uploaded — the
    # identical file has to go out once the connection is back.
    monkeypatch.setattr(log_handler.ws_client, "is_connected", lambda: True)
    handler._process(path)
    assert len(uploads) == 1


def test_unrelated_file_never_schedules(tmp_path, uploads, sso_character):
    path = _write(tmp_path, "Bumperking-Bank.txt", INVENTORY_DUMP)
    handler = log_handler.SharedFileHandler()

    handler.on_modified(FileModifiedEvent(path))

    assert handler._timers == {}
    assert uploads == []
