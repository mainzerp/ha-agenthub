"""Keep user/LLM text from being rendered as a Home Assistant template."""

from __future__ import annotations

import re

_TEMPLATE_OPENERS_RE = re.compile(r"\{(?=[{%#])")


def neutralize_ha_template(text: str) -> str:
    """Break Jinja delimiters so HA renders user/LLM content literally.

    The legacy ``notify.*`` services treat ``message`` and ``title`` as
    templates, so delivered content containing ``{{ ... }}`` / ``{% ... %}``
    / ``{# ... #}`` would be evaluated by Home Assistant (reading arbitrary
    entity states). Inserting a space after the opening brace keeps the
    text readable.
    """
    return _TEMPLATE_OPENERS_RE.sub("{ ", text or "")
