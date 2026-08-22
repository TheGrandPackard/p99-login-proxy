"""Encode EQ ``*-Inventory.txt`` / ``*-Spellbook.txt`` dumps for upload to the SSO API.

The server parses these with its own handlers and derives the key/pot flags it
sends back on the character tree, so the proxy ships the file verbatim rather
than a parsed summary. Payloads are gzipped and base64'd — a full inventory is
~40 KB of tab-delimited text that compresses to a few KB.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import logging
import os
import re

logger = logging.getLogger(__name__)

WIRE_ENCODING = "gzip+base64"

# Mirrors the server's ^\w+-Inventory\.txt$ / ^\w+-Spellbook\.txt$ regexes. A
# file the server would reject is not worth putting on the wire.
_UPLOADABLE_RE = re.compile(r"^\w+-(Inventory|Spellbook)\.txt$", re.IGNORECASE)


def is_uploadable_file_path(path: str) -> bool:
    """Is this an EQ dump the server will accept?"""
    return bool(_UPLOADABLE_RE.match(os.path.basename(path)))


def character_name_from_path(path: str) -> str:
    """Extract the character name from a dump filename, or ``""`` if it isn't one."""
    m = _UPLOADABLE_RE.match(os.path.basename(path))
    return m.group(0).rsplit("-", 1)[0] if m else ""


def read_and_encode(path: str) -> tuple[str, str] | None:
    """Read *path* once and return ``(sha256_hex, gzip+base64 payload)``.

    The hash is over the raw bytes so callers can skip re-uploading a file whose
    contents haven't changed — watchdog fires several events per write, and each
    upload costs the server a delete-and-reinsert of the character's rows.
    Returns ``None`` if the file can't be read.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        logger.exception("Failed to read shared info file: %s", path)
        return None

    digest = hashlib.sha256(raw).hexdigest()
    # mtime=0 keeps the payload byte-identical for identical content, so a
    # re-encode of an unchanged file doesn't look like a change downstream.
    payload = base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")
    return digest, payload
