# Test fixtures (plan: tests/fixtures/)

json3 rolling+manual caption samples, the URL-form table, and stubbed
yt-dlp DownloadError builders live here. Populated in Phase 2 onward.

- `rolling_asr_real.json` — live-captured real YouTube ASR track (modern
  line-roll shape: time-overlapping windows carrying disjoint text, empty
  erase events, no-segs lead event). Truncated to the opening verse; see the
  fixture's `_provenance`. Regression anchor for text-subsumption dedup
  (window-coverage gating silently dropped 3 of its 8 lines).
