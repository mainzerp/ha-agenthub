"""Bootstrap modules for setup-dependent runtime service initialization.

Each module exports an async setup function that initializes one
domain of the runtime and stores its results on ``app.state``.
"""

from __future__ import annotations
