"""MixExecutor: the abstract surface that all executors implement.

A plan is engine-agnostic. The executor is what turns a Plan into either a
sample-accurate WAV file (offline) or a live audio stream.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from dj_segue.schema.plan import Plan


@dataclass(frozen=True)
class RenderResult:
    """Summary of a render. Audio data is float32 stereo, shape (n_samples, 2)."""

    samples: np.ndarray
    sample_rate: int

    @property
    def duration_sec(self) -> float:
        return self.samples.shape[0] / self.sample_rate


class MixExecutor(ABC):
    """A plan-rendering engine. Implementations: native (sounddevice + WAV), mixxx (later)."""

    @abstractmethod
    def render(self, plan: Plan, audio_root: Path) -> RenderResult:
        """Render the plan to an in-memory float32 stereo buffer."""

    @abstractmethod
    def render_to_wav(self, plan: Plan, audio_root: Path, out_path: Path) -> RenderResult:
        """Render the plan and write to a WAV file. Returns the same buffer."""

    @abstractmethod
    def play_live(self, plan: Plan, audio_root: Path) -> RenderResult:
        """Render the plan and stream it to the default audio output device."""
