# Rubric Run — MPV-Fork-leaning evaluation

This scoring pass is based on architectural analysis and implementation experience patterns, not benchmarked prototype metrics yet.

## Did we run the rubric?
Yes — this file is the first explicit scoring run.

## Who filled this out?
I (the assistant) filled it out directly. No subagents were used.

## 1-5 Scale
- 1 = poor fit for MVP goals
- 3 = acceptable / moderate risk
- 5 = excellent fit

## Criteria and weighted scores

| Route | Time to Demo (20) | Reliability (15) | Render Control (20) | Motion Path (10) | Perf (15) | Packaging (10) | Maintainability (10) | Total / 100 |
|------|--------------------|------------------|---------------------|------------------|-----------|----------------|----------------------|-------------|
| A: mpv fork | 4 (16) | 5 (15) | 3 (12) | 4 (8) | 4 (12) | 4 (8) | 3 (6) | **77** |
| B: app shell + backend | 3 (12) | 4 (12) | 5 (20) | 4 (8) | 4 (12) | 3 (6) | 4 (8) | **78** |
| C: GStreamer | 2 (8) | 4 (12) | 4 (16) | 4 (8) | 4 (12) | 2 (4) | 3 (6) | **66** |
| D: hybrid phased | 4 (16) | 4 (12) | 4 (16) | 5 (10) | 4 (12) | 3 (6) | 3 (6) | **78** |

## Why mpv still looks strong
Even with Route B/D tying or narrowly edging it numerically, Route A is a very strong choice if your priority is:
- fastest path to stable playback,
- lower codec/seek/sync risk,
- getting to a demo quickly and artistically iterating on the effect.

## Important caveat
These scores should be treated as **pre-spike confidence scores**. Once we implement a 3-check technical spike, we should re-score using actual performance and integration data.

## Suggested immediate next step (if you love MPV fork)
Commit to Route A spike now:
1. fork mpv,
2. implement frame-diff activity mask,
3. drive alpha mask in custom render path,
4. capture frame-time + CPU/GPU metrics on Win11,
5. re-score rubric with evidence.
