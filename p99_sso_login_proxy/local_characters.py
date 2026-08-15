"""Helpers for mutating / persisting the local character CSV.

The in-memory source of truth is :data:`config.LOCAL_CHARACTERS`. All mutators here
take the lock, update the dict, schedule a debounced flush to disk, and notify
listeners so the UI can re-render.

No Qt dependency at import time; UI subscribes via :data:`ON_UPDATED`.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from p99_sso_login_proxy import config, utils

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_save_timer: threading.Timer | None = None
_SAVE_DEBOUNCE_SEC = 0.25

# Login methods (from server.PROXY_STATS.user_login) that establish a local-account
# binding suitable for pairing with the next new EQ log file.
_LOCAL_METHODS = frozenset({"local", "local_char"})

# How long a pending local-account login stays eligible to be paired with a new EQ
# log file. Needs to comfortably cover the real gap between a login handshake and
# the resulting new log file - P99's login queue alone routinely runs minutes long,
# on top of character-select and world-connect/zone-load. A shorter window discards
# the pairing for legitimate logins before the log file ever shows up, which forces
# players to bind the character manually via the (unvalidated) Add Local Character
# dialog instead - the actual source of most bad account references in the wild.
# This is no longer the primary safeguard against mis-attribution (see
# try_auto_create/log_handler.LogFileHandler: the marker is single-use and only
# ever offered for a genuinely new log file, and nothing is persisted without human
# confirmation via ON_AUTO_BIND_CANDIDATE) - it's just a sanity bound on how stale a
# dangling marker can get.
_PENDING_LOCAL_LOGIN_TTL_SECONDS = 15 * 60.0

# The most recently observed local-account login, and when it was recorded (via
# time.monotonic()). Set by note_login when a local rewrite fires, cleared by any
# non-local login. Consumed exactly once by try_auto_create, which clears it
# immediately regardless of outcome.
_pending_local_account: str | None = None
_pending_local_account_ts: float = 0.0

# Callbacks invoked (from the save-timer thread) whenever LOCAL_CHARACTERS mutates.
# Subscribers are responsible for marshalling back to their UI thread.
ON_UPDATED: list[Callable[[], None]] = []

# Callbacks invoked when try_auto_create finds a plausible new character/account
# pairing that needs a human to confirm before it's persisted and made usable for
# credential rewrites. Subscribers are responsible for marshalling back to their UI
# thread and, on approval, calling confirm_auto_bind().
ON_AUTO_BIND_CANDIDATE: list[Callable[[str, str], None]] = []


def _blank_items() -> dict[str, bool | int | None]:
    return {k: None for k in (*utils.LOCAL_CHARACTER_BOOL_ITEMS, *utils.LOCAL_CHARACTER_COUNT_ITEMS)}


def _ensure_entry(name: str) -> dict:
    """Return the mutable entry for ``name`` (lowercased), creating an empty one if needed."""
    key = name.lower()
    entry = config.LOCAL_CHARACTERS.get(key)
    if entry is None:
        entry = {
            "name": name,
            "account": "",
            "class": None,
            "level": None,
            "bind": None,
            "park": None,
            "items": _blank_items(),
        }
        config.LOCAL_CHARACTERS[key] = entry
        config.LOCAL_CHARACTER_NAMES.add(key)
    else:
        entry.setdefault("items", _blank_items())
        for wk in (*utils.LOCAL_CHARACTER_BOOL_ITEMS, *utils.LOCAL_CHARACTER_COUNT_ITEMS):
            entry["items"].setdefault(wk, None)
    return entry


def _notify() -> None:
    for cb in list(ON_UPDATED):
        try:
            cb()
        except Exception:
            logger.exception("Error in local_characters ON_UPDATED callback")


def _flush() -> None:
    with _lock:
        global _save_timer
        _save_timer = None
        snapshot = {k: dict(v) for k, v in config.LOCAL_CHARACTERS.items()}
    utils.save_local_characters(snapshot, config.LOCAL_CHARACTERS_FILE)
    _notify()


def _schedule_save() -> None:
    global _save_timer
    if _save_timer is not None:
        return
    _save_timer = threading.Timer(_SAVE_DEBOUNCE_SEC, _flush)
    _save_timer.daemon = True
    _save_timer.start()


def mark_dirty() -> None:
    """Schedule a debounced save + listener notification."""
    with _lock:
        _schedule_save()


def save_now() -> bool:
    """Flush synchronously (cancels any pending debounce). Returns success flag."""
    global _save_timer
    with _lock:
        if _save_timer is not None:
            _save_timer.cancel()
            _save_timer = None
        snapshot = {k: dict(v) for k, v in config.LOCAL_CHARACTERS.items()}
    ok = utils.save_local_characters(snapshot, config.LOCAL_CHARACTERS_FILE)
    _notify()
    return ok


def apply_update(
    name: str,
    *,
    park: str | None = None,
    bind: str | None = None,
    level: int | None = None,
    klass: str | None = None,
    items: dict | None = None,
) -> bool:
    """Merge observed fields into :data:`config.LOCAL_CHARACTERS[name.lower()]`.

    Only updates fields that are not ``None`` in the call. Returns ``True`` if the
    entry changed and a save was scheduled.
    """
    key = name.lower()
    if key not in config.LOCAL_CHARACTER_NAMES:
        return False

    changed = False
    with _lock:
        entry = _ensure_entry(name)
        if park is not None and entry.get("park") != park:
            entry["park"] = park
            changed = True
        if bind is not None and entry.get("bind") != bind:
            entry["bind"] = bind
            changed = True
        if level is not None and entry.get("level") != level:
            entry["level"] = level
            changed = True
        if klass is not None and entry.get("class") != klass:
            entry["class"] = klass
            changed = True
        if items:
            bucket = entry.setdefault("items", _blank_items())
            for wire_key, value in items.items():
                if bucket.get(wire_key) != value:
                    bucket[wire_key] = value
                    changed = True
        if changed:
            _schedule_save()
    return changed


def set_entry(entry: dict) -> None:
    """Replace the entry for ``entry['name']`` (used by Add/Edit dialog handlers)."""
    name = (entry.get("name") or "").strip()
    if not name:
        raise ValueError("entry must have a non-empty 'name'")
    key = name.lower()
    with _lock:
        normalized = {
            "name": name,
            "account": (entry.get("account") or "").strip().lower(),
            "class": entry.get("class") or None,
            "level": entry.get("level"),
            "bind": entry.get("bind") or None,
            "park": entry.get("park") or None,
            "items": {**_blank_items(), **(entry.get("items") or {})},
        }
        config.LOCAL_CHARACTERS[key] = normalized
        config.LOCAL_CHARACTER_NAMES.add(key)


def delete_entry(name: str) -> bool:
    """Remove the entry for ``name``; returns ``True`` if a row was removed."""
    key = name.lower()
    with _lock:
        if key not in config.LOCAL_CHARACTERS:
            return False
        del config.LOCAL_CHARACTERS[key]
        config.LOCAL_CHARACTER_NAMES.discard(key)
    return True


def note_login(method: str, account: str | None) -> None:
    """Record (or clear) the pending local account based on the login method.

    Called by every rewrite path in :mod:`server`. If *method* is one of
    :data:`_LOCAL_METHODS`, the resolved local account is stashed for the next
    new log file to claim. Any other method clears the slot so a stale local
    login can't be mis-paired with a subsequent non-local session.
    """
    global _pending_local_account, _pending_local_account_ts
    with _lock:
        if method in _LOCAL_METHODS and account and account.strip():
            _pending_local_account = account.strip().lower()
            _pending_local_account_ts = time.monotonic()
        else:
            _pending_local_account = None


def _consume_pending_local_account() -> str | None:
    """Pop the pending local account, if any and not expired. Single-use."""
    global _pending_local_account
    pending = _pending_local_account
    pending_ts = _pending_local_account_ts
    _pending_local_account = None
    if not pending:
        return None
    age = time.monotonic() - pending_ts
    if age > _PENDING_LOCAL_LOGIN_TTL_SECONDS:
        logger.warning(
            "Discarding pending local login for %r: %.0fs old (limit %.0fs)",
            pending,
            age,
            _PENDING_LOCAL_LOGIN_TTL_SECONDS,
        )
        return None
    return pending


def try_auto_create(character_name: str) -> bool:
    """Surface a candidate local-character binding for *character_name* when a
    local login is pending, for a human to confirm via :data:`ON_AUTO_BIND_CANDIDATE`.

    The pending login marker is consumed (cleared) exactly once by this call,
    regardless of outcome, and is ignored if it has expired. Returns ``True`` if
    a candidate was surfaced for confirmation. Safe no-op (with a logged warning)
    on collision with an existing row bound to a different account.
    """
    if not config.AUTO_ADD_LOCAL_CHARACTERS:
        return False
    if not character_name:
        return False

    with _lock:
        pending = _consume_pending_local_account()
        if not pending:
            return False
        if pending not in config.LOCAL_ACCOUNTS:
            logger.warning(
                "Skipping auto-add for %s: pending local account %r is no longer in local_accounts.csv",
                character_name,
                pending,
            )
            return False

        key = character_name.lower()
        if key in config.LOCAL_CHARACTER_NAMES:
            existing_account = (config.LOCAL_CHARACTERS.get(key) or {}).get("account") or ""
            if existing_account.lower() != pending:
                logger.warning(
                    "Local character %s is bound to %s; saw recent local login as %s (not overwriting)",
                    character_name,
                    existing_account or "<unset>",
                    pending,
                )
            return False

    logger.info("Auto-bind candidate: %s -> account %s (awaiting confirmation)", character_name, pending)
    for cb in list(ON_AUTO_BIND_CANDIDATE):
        try:
            cb(character_name, pending)
        except Exception:
            logger.exception("Error in local_characters ON_AUTO_BIND_CANDIDATE callback")
    return True


def confirm_auto_bind(character_name: str, account: str) -> bool:
    """Persist an auto-bind candidate after a human has confirmed it.

    Called by the UI layer in response to :data:`ON_AUTO_BIND_CANDIDATE`. Re-validates
    the binding is still sane (the account wasn't removed and the character name
    wasn't separately bound in the meantime) before writing it.
    """
    key = character_name.lower()
    with _lock:
        if account not in config.LOCAL_ACCOUNTS:
            logger.warning("Not binding %s -> %s: account no longer exists", character_name, account)
            return False
        if key in config.LOCAL_CHARACTER_NAMES:
            logger.warning("Not binding %s -> %s: character was bound in the meantime", character_name, account)
            return False

        set_entry(
            {
                "name": character_name,
                "account": account,
                "class": None,
                "level": None,
                "bind": None,
                "park": None,
                "items": {},
            }
        )
        mark_dirty()

    logger.info("Auto-added local character %s -> account %s (confirmed)", character_name, account)
    return True
