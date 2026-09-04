import threading
from collections.abc import Callable

from sqlalchemy.orm import Session


class RecoveryScheduler:
    def __init__(self, session_factory: Callable[[], Session], run_cycle: Callable[[Session], int], interval_seconds: int = 15):
        self.session_factory = session_factory
        self.run_cycle = run_cycle
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="revive-recovery-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            if not self._lock.acquire(blocking=False):
                continue
            db = self.session_factory()
            try:
                self.run_cycle(db)
            finally:
                db.close()
                self._lock.release()
