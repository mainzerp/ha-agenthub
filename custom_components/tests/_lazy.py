"""Lazy module proxy for tests.

The Home Assistant stand-ins are installed by a session fixture in conftest,
after test modules were collected, so test modules cannot import bridge
modules at import time. ``lazy`` returns a proxy that imports on first
attribute access; ``patch.object`` works on it (get and set are forwarded).
"""

import importlib


class LazyModule:
    def __init__(self, name: str) -> None:
        object.__setattr__(self, "_lazy_name", name)

    def _module(self):
        return importlib.import_module(object.__getattribute__(self, "_lazy_name"))

    def __getattr__(self, attr):
        return getattr(self._module(), attr)

    def __setattr__(self, attr, value):
        setattr(self._module(), attr, value)

    def __delattr__(self, attr):
        delattr(self._module(), attr)


def lazy(name: str) -> LazyModule:
    return LazyModule(name)
