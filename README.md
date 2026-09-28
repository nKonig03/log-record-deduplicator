# Log Record Deduplicator

A `logging.Handler` that suppresses repeated identical log records and emits a single count summary after a quiet period, to blunt log storms.

```python
import logging
from log_record_deduplicator import DeduplicatingHandler

stream = logging.StreamHandler()
dedup = DeduplicatingHandler(stream, quiet_period=5.0)

logger = logging.getLogger("app")
logger.addHandler(dedup)
logger.setLevel(logging.INFO)

for _ in range(1000):
    logger.error("connection refused")
# One ERROR record is emitted immediately. After 5 seconds of silence, a
# single summary record ("message repeated 999 times: ERROR") is emitted.
```

## Why this exists

A tight loop that logs the same error on every iteration can fill a disk or drown a dashboard in seconds, yet the first occurrence is usually the only one an operator needs to act on. This handler forwards that first record and replaces the rest with one summary line, so the signal stays visible and the volume drops by orders of magnitude.

The trade-off: the summary is delayed by the quiet period, and if the process exits mid-storm the count is only reported because `close()` flushes. If you never close the handler, a storm that never quietens down produces no summary at all — the first record is emitted, the rest are dropped, and the count is lost. Call `flush()` explicitly if you need it sooner.

## How identity is defined

Two records are considered identical when their formatted message, level number, and logger name all match. A `WARNING` and an `ERROR` with the same text are treated as different records and tracked separately. This is stricter than comparing only the message string, and it is the one interpretation the library commits to.

## Exported names

- `DeduplicatingHandler(target, quiet_period=5.0, clock=time.monotonic)` — the `logging.Handler` subclass. `target` is the wrapped handler.
- `DeduplicatingMemory(quiet_period=5.0, clock=time.monotonic)` — the pure state object, exposed for testing and for callers who want to drive deduplication logic without a handler.

## The awkward edge

The handler does not run a background thread. Summaries are produced only when a new record arrives, or when `flush()` / `tick()` is called. If your application goes quiet during a storm and never logs again, the summary will not appear until you flush. Wiring `flush()` into your shutdown path (or calling `tick()` from an event loop) is the caller's responsibility.
