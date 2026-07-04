"""Regression test for the local-character auto-bind mis-attribution bug.

Reproduces the confirmed single-client scenario: a stale, previously-unbound
``eqlog_*.txt`` file has its mtime bumped for a reason unrelated to any login
(backup software, cloud sync, antivirus, a manual open, ...) and momentarily
becomes the "latest modified" file in the watched ``Logs`` directory. Before the
fix, ``LogFileHandler.on_modified`` treated that purely as "a new character just
logged in" and silently bound it to whatever local account happened to be
pending, handing out that account's credentials to anyone typing the stale
character's name. The fix requires a file to be genuinely new (never seen by
this process before) to be eligible for auto-bind.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time

import pytest
from watchdog.events import FileModifiedEvent

from p99_sso_login_proxy import config, local_characters, log_handler


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _make_handler(log_dir, qapp):
    from PySide6.QtWidgets import QWidget

    get_latest = log_handler._make_get_latest_log_file(str(log_dir))
    parent = QWidget()
    return log_handler.LogFileHandler(str(log_dir), get_latest, parent)


def _seed_local_account(name: str, password: str = "pw") -> None:
    config.LOCAL_ACCOUNTS[name.lower()] = {"password": password, "aliases": []}


def test_stale_file_mtime_bump_does_not_trigger_auto_bind(tmp_path, qapp):
    # Simulate a player who already has a known local character "newgodflow"
    # bound to account "newgodflow" (so log tracking is active), plus an old,
    # never-locally-bound log file for a totally different character
    # "uberlyfttaxi" that predates this proxy run.
    _seed_local_account("newgodflow")
    local_characters.set_entry(
        {
            "name": "newgodflow",
            "account": "newgodflow",
            "class": None,
            "level": None,
            "bind": None,
            "park": None,
            "items": {},
        }
    )

    known_file = tmp_path / "eqlog_newgodflow_server.txt"
    known_file.write_text("")
    stale_file = tmp_path / "eqlog_uberlyfttaxi_server.txt"
    stale_file.write_text("")
    now = time.time()
    os.utime(known_file, (now, now))
    os.utime(stale_file, (now - 100, now - 100))

    handler = _make_handler(tmp_path, qapp)

    # A local login to "newgodflow" happens (this is what would normally arm
    # the auto-bind heuristic).
    local_characters.note_login("local", "newgodflow")

    candidates = []
    local_characters.ON_AUTO_BIND_CANDIDATE.append(lambda name, account: candidates.append((name, account)))

    # Something unrelated to any login (backup/AV/cloud-sync/manual open) bumps
    # the stale file's mtime so it now wins "latest modified in directory".
    bumped = time.time() + 5
    os.utime(stale_file, (bumped, bumped))

    handler.on_modified(FileModifiedEvent(str(stale_file)))

    assert candidates == []
    assert "uberlyfttaxi" not in config.LOCAL_CHARACTER_NAMES


def test_genuinely_new_file_still_triggers_auto_bind(tmp_path, qapp):
    _seed_local_account("newgodflow")
    local_characters.set_entry(
        {
            "name": "newgodflow",
            "account": "newgodflow",
            "class": None,
            "level": None,
            "bind": None,
            "park": None,
            "items": {},
        }
    )
    known_file = tmp_path / "eqlog_newgodflow_server.txt"
    known_file.write_text("")

    handler = _make_handler(tmp_path, qapp)

    local_characters.note_login("local", "newgodflow")
    candidates = []
    local_characters.ON_AUTO_BIND_CANDIDATE.append(lambda name, account: candidates.append((name, account)))

    # A character that genuinely didn't exist when the watcher started appears.
    new_file = tmp_path / "eqlog_brandnewalt_server.txt"
    new_file.write_text("")
    later = time.time() + 5
    os.utime(new_file, (later, later))

    handler.on_modified(FileModifiedEvent(str(new_file)))

    assert candidates == [("brandnewalt", "newgodflow")]
