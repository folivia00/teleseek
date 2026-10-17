# Performance

This page lists only changes that were measured before and after on the
production data set. All times are wall-clock times of the operation named in
each section.

## Worst-case search time: 92 s → 3.3 s

### Symptom

Some searches took tens of seconds; the slowest measured one took 92 s. Users
saw "Searching…" for a minute and then "nothing found".

### Cause

When the full-text index found no posts, the search fell back to a substring
scan (`text ILIKE '%…%'`) over a posts table of about 5.2 million rows. A
leading wildcard cannot use an index, so the cost depended on how rare the word
was. A benchmark over real zero-result queries showed:

| Query type | Fallback time | Posts found by the fallback |
|---|---|---|
| 7 real queries with no results | 23–92 s each | 0 |
| A popular query whose channels matched but posts did not | 86 s | 0 |

The fallback ran 8 times out of 8 and found nothing every time. Probes also
showed that it *is* sometimes cheap and useful: two common substrings returned
200 rows in about 0.6 s each. So removing it would lose real results, while
keeping it unbounded made the worst case unpredictable.

Alternatives considered:

- **Remove the fallback** — loses cheap hits inside words, hashtags and glued forms.
- **Trigram index on post texts** — would add an index of several gigabytes and
  hours of build time on a table of millions of rows; rejected. Trigrams are
  used only on short channel fields.
- **Time budget** — chosen.

### Change

1. The substring fallback runs inside a savepoint with
   `SET LOCAL statement_timeout = 2000` and returns at most 200 rows. On timeout
   the savepoint is rolled back and the user gets "nothing found"; the rest of
   the transaction is unaffected.
2. The post result cap (`MAX_RESULTS`) was lowered from 1000 to 300, since the
   cost of ranking and deduplication is proportional to it.
3. User analytics moved into its own short transaction (next section).

### Measurement

A benchmark script (`scripts/bench_search.py`) calls the real search functions
of the bot — the same caps, deduplication, content filter, prefix fallback and
substring fallback — and records time, result count, the path taken and the ids
of the top results. It ran in a one-off container against the production
database: 18 measurements (zero-result queries, queries with results in several
languages, the content-filter toggle) before the change and twice after it.

| Group | Before | After |
|---|---|---|
| Zero-result queries (7) | 23–92 s | 2.5–3.3 s |
| Popular query with zero post hits | 86 s | 2.9–3.2 s |
| Worst of all 18 measurements | 92 s | **3.3 s** |
| Top-N result sets of queries with results | — | identical in 18 of 18 (membership and order) |

The time measured is the search itself (channels + posts), not the full
Telegram round trip.

## Lock cascade on user statistics: 5.7 s → 18 ms

### Symptom and cause

The analytics middleware updated the user's `user_stats` row inside the same
transaction as the search. The row lock was held for the whole search, so the
*next* update from the same user (a button press during a slow search) waited
for the previous one to finish.

### Change and measurement

The middleware now writes analytics in its own short transaction and commits
immediately. A dedicated test (`tests/integration/test_lock_cascade.py`)
reproduces both designs: a session takes the row lock, a simulated slow search
runs, and a second session measures how long its own update waits.

The numbers below come from the run on the production database with a 6-second
simulated search; the test in this repository uses a 3-second simulation and
asserts the same property (old design waits for the search, new design does not).

| Design | Wait of the second update |
|---|---|
| Analytics in the search transaction | 5 724 ms |
| Analytics in its own transaction | 18 ms |

## Media filter: 16.5 s → 0.38 s

Filtering posts by media type used `text ILIKE '%…%'` over hundreds of thousands
of media posts. It was replaced by a match on the existing GIN-indexed
`search_vector` plus an `EXISTS` check on the media type. `EXPLAIN ANALYZE` of a
video search on an earlier, smaller snapshot of the index: 16.5 s before,
379 ms after with a warm cache (Bitmap Index Scan on the GIN index instead of a
sequential scan). Stemming also returned more results than the substring match.
The exact result counter, which cost 55 s on the same query, was removed in
favour of N+1 pagination (see [search.md](search.md)).

## Monitoring panel: 10.2 s → under 0.4 s per dashboard request

The dashboard polls the metrics endpoint every 30 seconds, and a full metrics
collection (exact counts over the posts table) cost seconds of database time.
A background history loop also ran the full collection around the clock and
discarded most of it.

Changes: a TTL cache with stale-while-revalidate and single-flight refresh (the
data age is shown in the UI), a cache warm-up at start, and a minimal collector
for the history loop with the expensive counters taken only on every sixth tick.
Exact counts were kept: an estimate from `pg_class.reltuples` was measured to be
about 10 % off and rejected.

| Operation | Before | After |
|---|---|---|
| `/api/metrics` request | 10 248 ms | 228–393 ms |
| Background history tick (regular) | 7 003 ms | 54 ms |

## Table bloat after a mass vector rebuild

Changing the weighting scheme of `posts.search_vector` required rewriting the
vector of every existing post. The mass `UPDATE` of an indexed column produced
non-HOT updates and left the table about 43 % bloated; searches became disk-bound
and slowed down sharply. Plain `VACUUM` does not return the space, so the fix was
`VACUUM FULL` on `posts` (910 MB → 519 MB at the time); the measured searches
became 20–250 times faster. Since then, any mass rewrite of `search_vector` is followed
by `VACUUM FULL` in a low-traffic window (see [operations.md](operations.md)).
