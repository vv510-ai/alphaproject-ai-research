"""OS-released ownership: a second server cannot recover or execute this DB's jobs."""

import os


class RuntimeLock:
    def __init__(self, path):
        self.path, self.handle = path, None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError("这份数据库已有服务运行，第二份实例未启动；请使用现有入口。") from None
        self.handle = handle

    def release(self):
        if self.handle is not None:
            # Closing the descriptor also releases the lock after a process crash.
            self.handle.close()
            self.handle = None
