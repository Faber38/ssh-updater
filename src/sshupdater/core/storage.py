"""Private application storage. Never follow application-file symlinks."""
from pathlib import Path
import os
import stat
import tempfile


def _validate(info, path, directory=False):
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not correct_type or (os.name == "posix" and info.st_uid != os.getuid()):
        raise OSError(f"Unsicherer Dateityp oder fremder Eigentümer: {path}")
    if not directory and info.st_nlink != 1:
        raise OSError(f"Mehrfach verknüpfte Datei wird nicht geöffnet: {path}")


def secure_directory(path: Path):
    path.mkdir(mode=0o700, exist_ok=True)
    _validate(path.lstat(), path, directory=True)
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _validate(os.fstat(fd), path, directory=True)
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)


def secure_file(path: Path, *, create=False):
    try:
        _validate(path.lstat(), path)
    except FileNotFoundError:
        if not create:
            return
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if create:
        flags |= os.O_CREAT
    fd = os.open(path, flags, 0o600)
    try:
        _validate(os.fstat(fd), path)
        if os.name == "posix":
            os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def read_private(path: Path) -> bytes:
    secure_file(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        _validate(os.fstat(stream.fileno()), path)
        return stream.read()


def write_private(path: Path, data: bytes, *, exclusive=False):
    secure_directory(path.parent)
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return
    secure_file(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def initialize(data_dir: Path):
    secure_directory(data_dir)
    for name in ("app.db", "app.db-journal", "app.db-wal", "app.db-shm",
                 "vault.salt", "vault.verify", "config.enc", "app.log",
                 "known_hosts", "known_hosts.lock", "theme.txt"):
        secure_file(data_dir / name)
