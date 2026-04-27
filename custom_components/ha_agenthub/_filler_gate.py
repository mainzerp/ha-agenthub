from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable, Literal, Optional

GateMechanism = Literal["announce", "media_player_state"]


@dataclass
class FillerGate:
    event: asyncio.Event
    deadline: float
    mechanism: GateMechanism
    cleanup: Optional[Callable[[], None]] = None
    observed_playing: bool = False