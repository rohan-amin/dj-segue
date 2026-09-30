"""Dependency-free constants shared across layers (safe to import from anywhere)."""

# A beat grid's beat 0 may sit up to this far before the file start (see
# analyzer/beat.py), so beat positions can resolve slightly negative.
ANCHOR_TOLERANCE = 0.02  # seconds
