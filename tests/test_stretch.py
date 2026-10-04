"""Streaming time-stretch (Rubber Band library): deterministic, independent
of how it's read, beat-accurate."""

from __future__ import annotations

import numpy as np

from dj_segue.executor.native.stretch import Stretcher, time_stretch

SR = 44100


def _reader(x: np.ndarray):
    def read(start: int, n: int) -> np.ndarray:
        out = np.zeros((n, 2), dtype=np.float32)
        lo, hi = max(start, 0), min(start + n, len(x))
        if hi > lo:
            out[lo - start : hi - start] = x[lo:hi]
        return out

    return read


def _noise(seconds: float, seed: int = 0) -> np.ndarray:
    return (0.2 * np.random.default_rng(seed).standard_normal((int(seconds * SR), 2))).astype(
        np.float32
    )


def test_same_input_same_output() -> None:
    x = _noise(3)
    a = Stretcher(_reader(x), 0, 1.05, SR).pull(SR * 2)
    b = Stretcher(_reader(x), 0, 1.05, SR).pull(SR * 2)
    assert np.array_equal(a, b)


def test_output_doesnt_depend_on_how_it_is_read() -> None:
    x = _noise(3, 1)
    whole = Stretcher(_reader(x), 0, 0.93, SR).pull(SR * 2)
    st, parts, got = Stretcher(_reader(x), 0, 0.93, SR), [], 0
    rng = np.random.default_rng(2)
    while got < SR * 2:
        k = int(min(rng.integers(1, 2000), SR * 2 - got))
        parts.append(st.pull(k))
        got += k
    assert np.array_equal(whole, np.concatenate(parts))


def test_clicks_land_on_the_stretched_beat() -> None:
    bpm, rate = 120.0, 1.05
    x = np.zeros((SR * 10, 2), dtype=np.float32)
    beat = 60 / bpm
    for k in range(int(10 / beat)):
        i = int(round(k * beat * SR))
        x[i : i + 200] = (np.hanning(400)[200:] * np.sin(np.arange(200) * 0.9))[:, None] * 0.8
    y = time_stretch(x, SR, rate)
    e = np.abs(y[:, 0])
    onsets, i = [], 0
    while i < len(e):
        if e[i] > 0.2 * e.max():
            onsets.append(i)
            i += SR // 5
        else:
            i += 1
    ideal = np.array([k * beat * SR / rate for k in range(len(onsets))])
    assert np.abs(np.array(onsets) - ideal).max() / SR < 0.0005  # within 0.5 ms


def test_length_and_identity() -> None:
    x = _noise(1)
    assert len(time_stretch(x, SR, 2.0)) == SR // 2
    assert len(time_stretch(x, SR, 0.5)) == SR * 2
    assert time_stretch(x, SR, 1.0) is x
