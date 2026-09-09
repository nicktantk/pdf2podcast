# Parallel Pdf2Podcast processing

This change is staged locally only. Do not deploy, replace files on `home`, or restart
the backend while the current conversion is running. Existing automatic retries on
`home` continue to use the deployed sequential implementation.

## Parallel work

- Generate up to four paragraph recordings concurrently by default. Each paragraph
  retains its male source reading followed by its female summary.
- Generate independent recovered-paragraph summaries in up to two concurrent API
  requests. Each request handles at most six paragraphs with disjoint source IDs.
- Keep chapter/paragraph mapping sequential: each annotation batch carries reference
  section context from the previous batch. Making those requests independent without
  separating context resolution would risk changing the interpretation of the paper.
- Keep one conversion worker. Concurrency is within a conversion, not an unbounded
  multiplication of jobs, paragraphs, and provider requests.

## Ordering, progress, and recovery

Recordings have stable chapter/paragraph keys and separate artifact paths. Only the
coordinator writes conversion progress. It counts actual completions, even when a later
paragraph finishes first. ETA uses elapsed wall time and completed text volume, not
the sum of overlapping worker durations; early estimates remain approximate.

After narration completes, the coordinator traverses the original chapter/paragraph
order to assign global timestamps and concatenate audio. Male/female timestamps,
transcript order, and source text do not depend on the order requests finish.

Only the configured number of tasks are submitted at once. A failed paragraph retries
locally with exponential backoff and jitter, reusing valid voice checkpoints. Provider
calls have a timeout. Authentication, configuration and storage errors are surfaced
rather than retried locally without purpose. On a terminal error, no more work is
submitted; running workers finish and checkpoint before a job-level retry can start.
Completed annotation and voice checkpoints remain compatible with the sequential path.

## Configuration

| Environment variable | Default | Allowed range |
| --- | --- | --- |
| `PDF2PODCAST_TTS_CONCURRENCY` | `4` | 1-16 |
| `PDF2PODCAST_SUMMARY_CONCURRENCY` | `2` | 1-8 |
| `PDF2PODCAST_TTS_ATTEMPTS` | `3` | 1-6 |
| `PDF2PODCAST_TTS_TIMEOUT_SECONDS` | `300` | 30-1800 |

Values are clamped to the indicated ranges. Use concurrency `1` for a sequential
fallback. These are application limits, not a statement of a provider's quota. Reduce
concurrency if throttling increases; parallel requests do not guarantee proportional
speedups or change the amount of narration required.

## Activation

After the current conversion finishes, build confidence with focused concurrency and
ordering checks before deploying. In particular, exercise out-of-order completions,
failed workers, cached voice segments, and final timestamp continuity. No such checks
were run as part of staging this change, and no running server was modified.
