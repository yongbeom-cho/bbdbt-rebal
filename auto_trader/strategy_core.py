"""Minimal shared types for live auto_trader."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Lot:
    shares: float
    entry: float
