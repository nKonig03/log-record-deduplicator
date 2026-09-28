import logging
import unittest

from log_record_deduplicator import DeduplicatingHandler, DeduplicatingMemory


class FakeClock:
    """A deterministic monotonic clock for tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class MemoryTests(unittest.TestCase):
    def test_first_occurrence_is_emitted(self) -> None:
        clock = FakeClock()
        mem = DeduplicatingMemory(quiet_period=5.0, clock=clock)
        emit, summary = mem.consider("app", logging.INFO, "hello")
        self.assertTrue(emit)
        self.assertIsNone(summary)

    def test_immediate_duplicate_is_suppressed(self) -> None:
        clock = FakeClock()
        mem = DeduplicatingMemory(quiet_period=5.0, clock=clock)
        mem.consider("app", logging.INFO, "hello")
        emit, summary = mem.consider("app", logging.INFO, "hello")
        self.assertFalse(emit)
        self.assertIsNone(summary)
        self.assertEqual(mem.pending_count, 1)

    def test_summary_after_quiet_period(self) -> None:
        clock = FakeClock()
        mem = DeduplicatingMemory(quiet_period=5.0, clock=clock)
        mem.consider("app", logging.INFO, "hello")
        mem.consider("app", logging.INFO, "hello")
        mem.consider("app", logging.INFO, "hello")
        # Not yet expired.
        clock.advance(4.0)
        self.assertIsNone(mem.expire())
        # Now expired.
        clock.advance(1.0)
        summary = mem.expire()
        self.assertIsNotNone(summary)
        name, levelno, count = summary  # type: ignore[misc]
        self.assertEqual(name, "app")
        self.assertEqual(levelno, logging.INFO)
        self.assertEqual(count, 2)
        # State is cleared after expiry.
        self.assertEqual(mem.pending_count, 0)

    def test_flush_forces_summary_regardless_of_quiet_period(self) -> None:
        clock = FakeClock()
        mem = DeduplicatingMemory(quiet_period=100.0, clock=clock)
        mem.consider("app", logging.WARNING, "boom")
        mem.consider("app", logging.WARNING, "boom")
        summary = mem.flush()
        self.assertIsNotNone(summary)
        _, _, count = summary  # type: ignore[misc]
        self.assertEqual(count, 1)

    def test_flush_with_nothing_pending_returns_none(self) -> None:
        clock = FakeClock()
        mem = DeduplicatingMemory(quiet_period=1.0, clock=clock)
        self.assertIsNone(mem.flush())

    def test_different_message_starts_new_batch(self) -> None:
        clock = FakeClock()
        mem = DeduplicatingMemory(quiet_period=5.0, clock=clock)
        mem.consider("app", logging.INFO, "hello")
        mem.consider("app", logging.INFO, "hello")
        # Switch to a different message after the quiet period has elapsed.
        clock.advance(6.0)
        emit, summary = mem.consider("app", logging.INFO, "goodbye")
        # The previous batch should be summarised.
        self.assertIsNotNone(summary)
        _, _, count = summary  # type: ignore[misc]
        self.assertEqual(count, 1)
        # The new message should be emitted.
        self.assertTrue(emit)

    def test_different_level_starts_new_batch(self) -> None:
        clock = FakeClock()
        mem = DeduplicatingMemory(quiet_period=5.0, clock=clock)
        mem.consider("app", logging.INFO, "hello")
        mem.consider("app", logging.INFO, "hello")
        clock.advance(6.0)
        emit, summary = mem.consider("app", logging.WARNING, "hello")
        self.assertIsNotNone(summary)
        self.assertTrue(emit)

    def test_different_logger_starts_new_batch(self) -> None:
        clock = FakeClock()
        mem = DeduplicatingMemory(quiet_period=5.0, clock=clock)
        mem.consider("app", logging.INFO, "hello")
        mem.consider("app", logging.INFO, "hello")
        clock.advance(6.0)
        emit, summary = mem.consider("other", logging.INFO, "hello")
        self.assertIsNotNone(summary)
        self.assertTrue(emit)

    def test_signature_change_before_quiet_period_drops_count(self) -> None:
        clock = FakeClock()
        mem = DeduplicatingMemory(quiet_period=5.0, clock=clock)
        mem.consider("app", logging.INFO, "hello")
        mem.consider("app", logging.INFO, "hello")
        # Switch before the quiet period elapses: the partial count is dropped.
        clock.advance(1.0)
        emit, summary = mem.consider("app", logging.INFO, "goodbye")
        self.assertIsNone(summary)
        self.assertTrue(emit)

    def test_negative_quiet_period_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DeduplicatingMemory(quiet_period=-1.0)

    def test_zero_quiet_period_allows_immediate_expiry(self) -> None:
        clock = FakeClock()
        mem = DeduplicatingMemory(quiet_period=0.0, clock=clock)
        mem.consider("app", logging.INFO, "hello")
        mem.consider("app", logging.INFO, "hello")
        # With a zero quiet period, the next call to expire should produce a
        # summary immediately.
        summary = mem.expire()
        self.assertIsNotNone(summary)


class _CollectingHandler(logging.Handler):
    """Captures emitted records for assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class HandlerTests(unittest.TestCase):
    def _make_record(
        self, name: str, level: int, msg: str, *args: object
    ) -> logging.LogRecord:
        return logging.LogRecord(
            name=name,
            level=level,
            pathname=__file__,
            lineno=0,
            msg=msg,
            args=args,
            exc_info=None,
        )

    def test_handler_forwards_first_and_suppresses_duplicates(self) -> None:
        clock = FakeClock()
        target = _CollectingHandler()
        handler = DeduplicatingHandler(
            target, quiet_period=5.0, clock=clock
        )
        handler.emit(self._make_record("app", logging.INFO, "hello"))
        handler.emit(self._make_record("app", logging.INFO, "hello"))
        handler.emit(self._make_record("app", logging.INFO, "hello"))
        self.assertEqual(len(target.records), 1)

    def test_handler_emits_summary_on_flush(self) -> None:
        clock = FakeClock()
        target = _CollectingHandler()
        handler = DeduplicatingHandler(
            target, quiet_period=100.0, clock=clock
        )
        handler.emit(self._make_record("app", logging.WARNING, "boom"))
        handler.emit(self._make_record("app", logging.WARNING, "boom"))
        handler.emit(self._make_record("app", logging.WARNING, "boom"))
        handler.flush()
        # First record + one summary.
        self.assertEqual(len(target.records), 2)
        summary = target.records[1]
        self.assertEqual(summary.name, "app")
        self.assertEqual(summary.levelno, logging.WARNING)
        self.assertIn("2", summary.getMessage())

    def test_handler_emits_summary_on_signature_change_after_quiet(self) -> None:
        clock = FakeClock()
        target = _CollectingHandler()
        handler = DeduplicatingHandler(
            target, quiet_period=5.0, clock=clock
        )
        handler.emit(self._make_record("app", logging.INFO, "hello"))
        handler.emit(self._make_record("app", logging.INFO, "hello"))
        clock.advance(6.0)
        handler.emit(self._make_record("app", logging.INFO, "goodbye"))
        # Original + summary + new message.
        self.assertEqual(len(target.records), 3)

    def test_handler_close_flushes(self) -> None:
        clock = FakeClock()
        target = _CollectingHandler()
        handler = DeduplicatingHandler(
            target, quiet_period=100.0, clock=clock
        )
        handler.emit(self._make_record("app", logging.INFO, "hello"))
        handler.emit(self._make_record("app", logging.INFO, "hello"))
        handler.close()
        self.assertEqual(len(target.records), 2)

    def test_handler_does_not_emit_summary_when_no_duplicates(self) -> None:
        clock = FakeClock()
        target = _CollectingHandler()
        handler = DeduplicatingHandler(
            target, quiet_period=1.0, clock=clock
        )
        handler.emit(self._make_record("app", logging.INFO, "hello"))
        clock.advance(2.0)
        handler.flush()
        # Only the original record; no summary because nothing was suppressed.
        self.assertEqual(len(target.records), 1)


if __name__ == "__main__":
    unittest.main()
