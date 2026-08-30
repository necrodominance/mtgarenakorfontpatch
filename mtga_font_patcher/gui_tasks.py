from __future__ import annotations

from dataclasses import dataclass
from queue import Empty, Queue
import threading
from typing import Any, Callable


@dataclass(frozen=True)
class JobEvent:
    kind: str
    payload: Any = None


class BackgroundJob:
    """Run one long operation away from the Tk main thread.

    Worker threads never touch Tk. They only write events to a thread-safe queue;
    the GUI polls that queue from the main loop.
    """

    def __init__(self) -> None:
        self._events: Queue[JobEvent] = Queue()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, work: Callable[[Callable[[str], None]], Any]) -> None:
        if self.running:
            raise RuntimeError('A background job is already running.')

        def progress(message: str) -> None:
            self._events.put(JobEvent('progress', str(message)))

        def runner() -> None:
            try:
                result = work(progress)
            except BaseException as exc:  # transport to UI thread; do not touch Tk here
                self._events.put(JobEvent('error', exc))
            else:
                self._events.put(JobEvent('success', result))

        self._thread = threading.Thread(target=runner, name='MTGAFontPatcherWorker', daemon=True)
        self._thread.start()

    def poll(self) -> list[JobEvent]:
        events: list[JobEvent] = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except Empty:
                return events
