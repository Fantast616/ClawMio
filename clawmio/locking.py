"""Kernel-held locks: crashes release ownership without deleting lock files."""
from contextlib import contextmanager
import os
from pathlib import Path


class Locked(RuntimeError):
    pass


@contextmanager
def exclusive(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    file = open(path, 'a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            file.seek(0, 2)
            if not file.tell():
                file.write(b'0')
                file.flush()
            file.seek(0)
            try:
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise Locked('已有进程正在使用此配置或数据库，请先停止服务。') from None
        else:
            import fcntl
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise Locked('已有进程正在使用此配置或数据库，请先停止服务。') from None
        yield
    finally:
        file.close()


def is_locked(path):
    try:
        with exclusive(path):
            return False
    except Locked:
        return True
