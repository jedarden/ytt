# Bead ytt-7oe: json3 rolling-caption dedup - VERIFICATION COMPLETE

## Status: COMPLETE (Pre-existing implementation)

## Implementation Details

The json3 rolling-caption dedup was implemented in commit `ba9dcef` on 2026-06-21 by jedarden.

### Algorithm Implementation (ytt/parse_json3.py)

The rolling-caption dedup is implemented in the `_parse_asr()` function with two stages:

#### 1. Primary Dedup (Window Coverage)
- Sort events by `tStartMs` ascending
- Maintain `last_end_ms = 0`
- Emit only events where `tStartMs >= last_end_ms`
- On emit, update `last_end_ms = tStartMs + dDurationMs`

This ensures only the first event in each non-overlapping time window is emitted.

#### 2. Secondary Dedup (Prefix Check)
- For each emitted event `i`, compare its text with event `i+1`
- Discard event `i` if its stripped text is a **strict prefix** of event `i+1`'s stripped text
- Case-sensitive comparison
- Equal text is NOT a prefix (must be strictly shorter)

This handles edge cases where the primary dedup leaves redundant segments.

### Fixture Files

#### tests/fixtures/rolling_asr.json
- 11 events simulating YouTube's rolling ASR behavior
- Naive join produces 22 words with heavy duplication
- Expected dedup output: 3 segments, 9 words total
- Groups: "This is a test" | "of the dedup algorithm" | "works"

#### tests/fixtures/manual_track.json  
- 4 manual caption events (3 real + 1 formatting)
- Tests aAppend (word continuation) and pAppend (punctuation)
- No dedup needed for manual tracks

### Test Coverage

45 unit tests in `tests/unit/test_parse_json3.py`:
- Fixture reference-output assertions
- aAppend/pAppend spacing rules  
- Primary dedup: overlapping events, sorting, empty events, gaps
- Prefix-check: case-sensitivity, strict-vs-equal, chain behavior
- Edge cases: empty lists, missing fields, timing conversions
- Manual track: no dedup, overlapping times allowed

All tests pass: `45 passed in 0.14s`

## Verification Results

**First verification (2026-06-21):**
```bash
$ uv run pytest tests/unit/test_parse_json3.py -v
============================== 45 passed in 0.14s ==============================
```

**Re-verification (2026-06-25):**
```bash
$ source .venv/bin/activate && python -m pytest tests/unit/test_parse_json3.py -v
============================== 45 passed in 0.13s ==============================
```

Full unit test suite (560 tests) also passes:
```bash
$ python -m pytest tests/unit/ -v
======================== 560 passed, 1 warning in 1.71s ========================
```

Manual verification of rolling_asr.json fixture:
- **Naive join**: "This is a test is a test a test test of the dedup algorithm the dedup algorithm dedup algorithm algorithm works works" (22 words)
- **Dedup output**: ["This is a test", "of the dedup algorithm", "works"] (9 words)
- **Match**: ✓ True

## Conclusion

The rolling-caption dedup implementation is **complete and working correctly**. The bead ytt-7oe describes work that was already completed in commit `ba9dcef` (2026-06-21).

- ✓ Overlap/prefix dedup implemented
- ✓ Mandatory rolling+manual fixtures created
- ✓ All tests passing
- ✓ Output matches pre-verified reference

This bead can be closed as complete.
