"""travis-player v2 — codec-driven motion transparency.

v1 re-derived motion by pixel-diffing every full frame. v2 reads the codec's own
per-macroblock motion vectors (a byproduct of decode) and only touches the tiles the
encoder says changed. see ../notes and the plan for the why.
"""

__version__ = "2.0.0-dev"
