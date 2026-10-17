"""
Print/compare bench_search results.

    python3 scripts/bench_report.py before.json            # table for a single run
    python3 scripts/bench_report.py before.json after.json # BEFORE/AFTER + top-N regression check

Contains no secrets or production data: reads only benchmark JSON files.
"""

import json
import sys

HDR = f"{'query':<16}{'adult':<7}{'ms_ch':>8}{'ms_posts':>10}{'ms_tot':>9}{'nCh':>4}{'nP':>4}{'blks':>9}  path"


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def show(d, title):
    print(f"\n########## {title} (MAX_RESULTS={d['max_results']}, "
          f"fallback={d.get('post_fallback_enabled')}, "
          f"budget_ms={d.get('post_fallback_timeout_ms')}) ##########")
    for g in ("empty", "result", "adult_toggle"):
        print(f"\n=== {g} ===")
        print(HDR)
        for r in d["groups"][g]:
            print(f"{r['query'][:15]:<16}{str(r['adult_only']):<7}"
                  f"{r['ms_channels']:>8.0f}{r['ms_posts']:>10.0f}{r['ms_total']:>9.0f}"
                  f"{r['n_channels']:>4}{r['n_posts']:>4}{r['blks_read_delta']:>9}  {r['post_path']}")


def key(r):
    return (r["query"], r["adult_only"])


def compare(a, b):
    print("\n########## BEFORE → AFTER ##########")
    print(f"{'query':<16}{'adult':<7}{'ms_tot pre':>11}{'ms_tot post':>14}{'speedup':>11}"
          f"{'nP pre':>7}{'nP post':>10}  top-N")
    worst = 0.0
    regress = []
    for g in ("empty", "result", "adult_toggle"):
        rows_a = {key(r): r for r in a["groups"][g]}
        for rb in b["groups"][g]:
            ra = rows_a.get(key(rb))
            if not ra:
                continue
            spd = (ra["ms_total"] / rb["ms_total"]) if rb["ms_total"] else float("inf")
            same_ch = ra["top_channel_ids"] == rb["top_channel_ids"]
            same_p = ra["top_post_ids"] == rb["top_post_ids"]
            mark = "OK" if (same_ch and same_p) else (
                "CHANGED:" + ("" if same_ch else " channels") + ("" if same_p else " posts"))
            if not (same_ch and same_p):
                regress.append((g, rb["query"], rb["adult_only"], same_ch, same_p,
                                len(ra["top_post_ids"]), len(rb["top_post_ids"])))
            worst = max(worst, rb["ms_total"])
            print(f"{rb['query'][:15]:<16}{str(rb['adult_only']):<7}"
                  f"{ra['ms_total']:>11.0f}{rb['ms_total']:>14.0f}{spd:>10.1f}x"
                  f"{ra['n_posts']:>7}{rb['n_posts']:>10}  {mark}")
    print(f"\nWORST path AFTER: {worst:.0f} ms  "
          f"({'✅ <5s' if worst < 5000 else '❌ >5s — requirement not met'})")
    if regress:
        print("\n⚠️ TOP-N MISMATCHES (breakdown):")
        for g, q, ad, sc, sp, na, nb in regress:
            print(f"  [{g}] {q!r} adult={ad}: channels_match={sc} posts_match={sp} "
                  f"(posts BEFORE={na}, AFTER={nb})")
    else:
        print("\n✅ No regression: top-N (set AND order) matched in all measurements.")


if __name__ == "__main__":
    if len(sys.argv) == 2:
        show(load(sys.argv[1]), sys.argv[1])
    elif len(sys.argv) == 3:
        a, b = load(sys.argv[1]), load(sys.argv[2])
        show(a, "BEFORE")
        show(b, "AFTER")
        compare(a, b)
    else:
        print(__doc__)
        sys.exit(2)
