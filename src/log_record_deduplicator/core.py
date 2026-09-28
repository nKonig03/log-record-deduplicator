"""A logging handler that suppresses repeated identical records.

When the same message is emitted many times in quick succession (a "log
storm"), only the first occurrence is forwarded to the wrapped target. After a
quiet period elapses with no further repeats, a single summary record is emitted
describing how many duplicates were suppressed.

Design decisions
----------------

* The handler wraps another ``logging.Handler`` rather than subclassing a
  specific one. This keeps it composable with ``StreamHandler``,
  ``FileHandler``, or any custom handler.

* Time is injected via a ``clock`` callable. Production code passes
  ``time.monotonic`` (monotonic, so wall-clock adjustments can't cause a quiet
  period to fire early or late). Tests pass a fake clock so assertions are
  deterministic.

* Two records are considered "identical" when their formatted message, level
  number, and logger name all match. This is deliberately stricter than
  comparing only the message string: a WARNING and an ERROR with the same text
  are different records and should not be coalesced.

* The summary record is emitted at the same level as the original and includes
  the logger name, so downstream filters and formatters see a consistent
  lineage.

* ``flush()`` forces emission of any pending summary. This matters at shutdown:
  without it, a process that exits during a storm would silently lose the count
  of suppressed records. ``close()`` calls ``flush()`` before delegating to the
  wrapped handler.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional


# Default quiet period in seconds. If no duplicate arrives within this window,
# the summary is emitted. Chosen to be short enough that operators see the
# summary promptly after a storm ends, but long enough that a burst of messages
# spaced a few seconds apart is still coalesced.
DEFAULT_QUIET_PERIOD = 5.0


class DeduplicatingMemory:
    """Pure state for the deduplication logic, with no I/O.

    Separating state from the handler makes the behaviour trivial to test
    without instantiating a real ``logging.Handler`` or touching the logging
    framework's global state.
    """

    def __init__(
        self,
        quiet_period: float = DEFAULT_QUIET_PERIOD,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if quiet_period < 0:
            raise ValueError("quiet_period must be non-negative")
        self._quiet_period = quiet_period
        self._clock = clock
        # Signature of the record currently being suppressed: (name, levelno,
        # formatted_message). ``None`` means nothing is being tracked.
        self._signature: Optional[tuple[str, int, str]] = None
        self._count: int = 0  # Number of duplicates suppressed so far.
        self._last_seen: float = 0.0  # Clock value of the most recent duplicate.

    @property
    def quiet_period(self) -> float:
        return self._quiet_period

    def consider(
        self, name: str, levelno: int, message: str
    ) -> tuple[bool, Optional[tuple[str, int, int]]]:
        """Decide what to do with a record.

        Returns a pair ``(emit_now, summary)``:

        * ``emit_now`` — whether the caller should forward this record
          immediately. True for the first occurrence of a signature (or when
          nothing is being tracked), False for subsequent duplicates.
        * ``summary`` — if not ``None``, a ``(name, levelno, count)`` tuple
          describing a batch of suppressed records that has just become eligible
          for emission (because the quiet period elapsed). The caller should
          emit exactly one summary record for it.

        At most one summary is returned per call. A call that returns a summary
        may also return ``emit_now=True`` for the incoming record when that
        record starts a new batch.
        """
        now = self._clock()
        sig = (name, levelno, message)

        summary: Optional[tuple[str, int, int]] = None

        if self._signature is not None and self._signature != sig:
            # The signature changed. If the previous batch is still open and
            # the quiet period has elapsed, emit its summary now. If the quiet
            # period has *not* elapsed we drop the pending count silently: the
            # storm was interrupted by a different message and the count is no
            # longer growing, so reporting a partial count would be noise.
            if self._count > 0 and (now - self._last_seen) >= self._quiet_period:
                summary = (self._signature[0], self._signature[1], self._count)
            self._signature = None
            self._count = 0

        if self._signature == sig:
            # A duplicate of the currently-tracked record.
            self._count += 1
            self._last_seen = now
            return False, summary

        # First occurrence of a new signature.
        self._signature = sig
        self._count = 0
        self._last_seen = now
        return True, summary

    def expire(self) -> Optional[tuple[str, int, int]]:
        """Emit a summary for the tracked batch if the quiet period has elapsed.

        Returns ``(name, levelno, count)`` or ``None``. This is the path by
        which a summary is produced *without* a new record arriving — for
        example, when the handler is flushed or the quiet period elapses and
        ``tick()`` is called.
        """
        if self._signature is None or self._count == 0:
            return None
        now = self._clock()
        if (now - self._last_seen) >= self._quiet_period:
            summary = (self._signature[0], self._signature[1], self._count)
            self._signature = None
            self._count = 0
            return summary
        return None

    def tick(self) -> Optional[tuple[str, int, int]]:
        """Poll for an expired batch.

        The handler does not run a background thread, so summaries are only
        emitted when a record arrives or ``flush``/``tick`` is called. ``tick``
        is exposed for callers who want to drive expiry from their own event
        loop without forcing a flush.
        """
        return self.expire()

    def flush(self) -> Optional[tuple[str, int, int]]:
        """Force-emission of any pending summary, regardless of the quiet period.

        Used at shutdown so a process that exits mid-storm still reports how
        many records were suppressed.
        """
        if self._signature is not None and self._count > 0:
            summary = (self._signature[0], self._signature[1], self._count)
            self._signature = None
            self._count = 0
            return summary
        return None

    @property
    def pending_count(self) -> int:
        """Number of duplicates currently suppressed but not yet summarised."""
        return self._count


class DeduplicatingHandler(logging.Handler):
    """A ``logging.Handler`` that coalesces repeated identical records.

    Parameters
    ----------
    target:
        The wrapped handler. Records that survive deduplication (and the
        summary records) are forwarded here.
    quiet_period:
        Seconds of silence after which a batch of suppressed records is
        summarised. Defaults to ``5.0``.
    clock:
        A zero-argument callable returning a float, used as the time source.
        Defaults to ``time.monotonic``. Inject a fake in tests.
    """

    def __init__(
        self,
        target: logging.Handler,
        quiet_period: float = DEFAULT_QUIET_PERIOD,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._target = target
        self._memory = DeduplicatingMemory(quiet_period=quiet_period, clock=clock)
        # Reuse the target's formatter so summary records render consistently
        # with the originals. If the target has no formatter yet we fall back
        # to the default set on this handler.
        self._formatter: Optional[logging.Formatter] = None

    @property
    def target(self) -> logging.Handler:
        return self._target

    @property
    def memory(self) -> DeduplicatingMemory:
        return self._memory

    def emit(self, record: logging.LogRecord) -> None:
        """Forward ``record`` unless it is a duplicate; emit summaries as needed."""
        try:
            message = self.format(record)
            emit_now, summary = self._memory.consider(
                record.name, record.levelno, message
            )
            if summary is not None:
                self._emit_summary(*summary)
            if emit_now:
                self._target.emit(record)
        except Exception:
            self.handleError(record)

    def _emit_summary(self, name: str, levelno: int, count: int) -> None:
        """Build and forward a synthetic summary record."""
        levelname = logging.getLevelName(levelno)
        msg = "message repeated %d times: %s"
        # We don't have the original formatted text here, so we report the
        # logger name and level instead. This keeps the summary self-contained
        # and avoids storing potentially large message bodies indefinitely.
        summary_record = logging.LogRecord(
            name=name,
            level=levelno,
            pathname=__file__,
            lineno=0,
            msg=msg,
            args=(count, levelname),
            exc_info=None,
        )
        # Use the target's formatter if one is set, so the summary is rendered
        # the same way as real records.
        target_formatter = getattr(self._target, "formatter", None)
        if target_formatter is not None:
            summary_record.getMessage()
            self._target.handle(summary_record)
        else:
            self._target.handle(summary_record)

    def flush(self) -> None:
        """Flush any pending summary, then flush the wrapped handler."""
        summary = self._memory.flush()
        if summary is not None:
            self._emit_summary(*summary)
        self._target.flush()

    def close(self) -> None:
        """Emit pending summary and close the wrapped handler."""
        try:
            self.flush()
        except Exception:
            pass
        self._target.close()
        super().close()
