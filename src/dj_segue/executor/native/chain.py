"""One deck's processing after its source audio, over consecutive ranges.

    stems (decks mixed by stems): each stem's EQ and volume, then summed
    → the deck's EQ → its filter → its volume (transitions × deck_volume
    lanes × crossfader)

`DeckChain.process(a, b, …)` takes the deck's pre-gain audio for mix samples
[a, b) and returns what it contributes to the mix. The EQ splitters and the
filter carry their state from call to call, so processing a deck in blocks
gives bitwise the same samples as one call over the whole mix: the offline
engine makes that one call, the live engine one per block.
"""

from __future__ import annotations

import numpy as np

from dj_segue.compiler import (
    crossfader_envelope,
    crossfader_gains,
    deck_volume_envelopes,
    eq_decks,
    eq_envelope,
    eq_stems,
    filter_envelope,
    stem_envelopes,
    transition_envelopes,
)
from dj_segue.executor.native.curves import render_envelope_range
from dj_segue.executor.native.eq import BandSplitter, apply_eq
from dj_segue.executor.native.filter import FilterState
from dj_segue.schema.plan import Plan

BANDS = ("low", "mid", "high")


class DeckChain:
    def __init__(
        self,
        plan: Plan,
        deck: int,
        mix_tempo: float,
        sample_rate: int,
        anchors: dict[str, float],
        stems: tuple[str, ...] = (),
    ) -> None:
        self.sr = sample_rate
        self.stems = stems
        envs = deck_volume_envelopes(plan, deck, mix_tempo, anchors)
        transitions = transition_envelopes(plan, mix_tempo, anchors)
        if deck in transitions:
            envs.append(transitions[deck])
        self.gain_envs = envs
        self.xfade = None
        if deck in (1, 2):
            self.xfade = crossfader_envelope(plan, mix_tempo, anchors)
        self.deck = deck

        def eq_envs(stem=None):
            return [eq_envelope(plan, deck, band, mix_tempo, anchors, stem) for band in BANDS]

        self.eq = eq_envs() if deck in eq_decks(plan) else None
        self.split = BandSplitter(sample_rate) if self.eq else None
        self.stem_envs = {s: stem_envelopes(plan, deck, s, mix_tempo, anchors) for s in stems}
        self.stem_eq = {s: eq_envs(s) for d, s in eq_stems(plan) if d == deck and s in stems}
        self.stem_split = {s: BandSplitter(sample_rate) for s in self.stem_eq}
        self.filter_env = filter_envelope(plan, deck, mix_tempo, anchors)
        self.filter = FilterState(sample_rate) if self.filter_env else None

    # -- per-sample controls over [a, b) --------------------------------------

    def gains(self, a: int, b: int) -> np.ndarray:
        g = np.ones(b - a, dtype=np.float32)
        for env in self.gain_envs:
            g *= render_envelope_range(env, self.sr, a, b)
        if self.xfade is not None:
            pos = render_envelope_range(self.xfade, self.sr, a, b)
            g1, g2 = crossfader_gains(pos)
            g *= (g1 if self.deck == 1 else g2).astype(np.float32)
        return g

    def stem_gains(self, stem: str, a: int, b: int) -> np.ndarray:
        g = np.ones(b - a, dtype=np.float32)
        for env in self.stem_envs[stem]:
            g *= render_envelope_range(env, self.sr, a, b)
        return g

    def _eq_gains(self, envs, a: int, b: int):
        out = []
        for env in envs:
            if env is None:
                out.append(np.ones(b - a, dtype=np.float32))
            else:
                db = render_envelope_range(env, self.sr, a, b)
                out.append((10.0 ** (db / 20.0)).astype(np.float32))
        return out

    # -- audio --------------------------------------------------------------------

    def process(self, a: int, b: int, audio, bands=None, stem_bands=None) -> np.ndarray:
        """The deck's contribution to mix samples [a, b).

        `audio`: the deck's pre-gain audio for [a, b) — or, on a deck mixed
        by stems, {stem: audio}. EQ'd audio is split here, unless the caller
        already split it (the offline engine, once per render): then `audio`
        is the band sum and `bands` / `stem_bands[stem]` its (low, high).
        """
        if self.stems:
            x = np.zeros((b - a, 2), dtype=np.float32)
            for stem in self.stems:
                s = audio[stem]
                if stem in self.stem_eq:
                    if stem_bands and stem in stem_bands:
                        sb = stem_bands[stem]
                    else:
                        s, *sb = self.stem_split[stem].process(s)
                    s = apply_eq(s, self._eq_gains(self.stem_eq[stem], a, b), sb)
                x += s * self.stem_gains(stem, a, b)[:, None]
            if self.eq:
                x, *bands = self.split.process(x)
        else:
            x = audio
            if self.eq and bands is None:
                x, *bands = self.split.process(x)
        if self.eq:
            x = apply_eq(x, self._eq_gains(self.eq, a, b), bands)
        if self.filter is not None:
            x = self.filter.process(x, render_envelope_range(self.filter_env, self.sr, a, b))
        return x * self.gains(a, b)[:, None]
