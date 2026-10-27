"""Safe file writing (CS-14, FR-CFG-10, NFR-5).

A file is never half-written: the new content goes to a temporary file in the same folder, is
flushed to disk, and then replaces the old file in one step. The previous version is kept next to it
as ``<name>.bak``. Nothing is written when the content did not change, which spares the SD card.
"""

import hashlib
import os
import shutil
import tempfile
from pathlib import Path


def sha256_hex(data: bytes) -> str:
    """Return the SHA-256 of some bytes as hexadecimal text."""
    return hashlib.sha256(data).hexdigest()


def write_atomic(path: Path, data: bytes, *, keep_backup: bool = True) -> bool:
    """Write ``data`` to ``path`` atomically; returns False if the file already had that content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current: bytes | None = path.read_bytes()
    except FileNotFoundError:
        current = None
    if current == data:
        return False
    descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if keep_backup and current is not None:
            shutil.copyfile(path, path.with_name(path.name + ".bak"))
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return True
