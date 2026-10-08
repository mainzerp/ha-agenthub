"""Shared system-prompt builder used by GeneralAgent and DynamicAgent."""

from __future__ import annotations

import re

# Reply token a sequential-send content agent emits when it cannot produce
# the message body. Lives in this leaf module so the orchestrator can import
# it without an import cycle; the orchestrator skips delivery when it sees it.
NO_CONTENT_SENTINEL = "[[NO_CONTENT]]"

# Tolerates case, missing or extra brackets and markdown backslash escapes
# ("\[\[NO\_CONTENT\]\]"). The underscore stays required, so natural text
# such as "no content" never matches.
_NO_CONTENT_SENTINEL_RE = re.compile(r"\\?\[*\s*\bno\\?_content\b\s*\\?\]*", re.IGNORECASE)


def contains_no_content_sentinel(text: str | None) -> bool:
    """Return True when ``text`` carries the no-content sentinel in any tolerated form."""
    if not text:
        return False
    return _NO_CONTENT_SENTINEL_RE.search(text) is not None


class PromptBuilder:
    """Builds an LLM system prompt by appending context to a base prompt."""

    @staticmethod
    def build(
        base_prompt: str,
        *,
        language: str | None = None,
        time_location: str | None = None,
        sequential_send: bool = False,
    ) -> str:
        prompt = base_prompt

        if time_location:
            prompt += f"\n\n{time_location}"

        if language and language.lower() not in ("en", "english", ""):
            prompt += (
                f"\n\nCRITICAL LANGUAGE INSTRUCTION: The user's language is {language}.\n"
                f"Respond in {language}.\n"
                "Copy entity, device, room, and scene names verbatim from the user's message.\n"
                "NEVER translate entity names to English, "
                "regardless of what language the few-shot examples use.\n"
                "If a few-shot example uses a different language than the user, "
                "copy the example's STRUCTURE but keep the USER's original entity names unchanged.\n\n"
            )

        if sequential_send:
            prompt += (
                "\n\nSEQUENTIAL DELIVERY MODE:\n"
                "This response will be delivered as text to a device (not spoken aloud). "
                "Your reply is used verbatim as the body of a message that a separate delivery step "
                "sends to a person or device. Delivery is handled elsewhere: do not refuse, and do not "
                "say you cannot send messages or control devices.\n"
                "- If the user dictated the message text, return exactly that text, without quotes, "
                "prefixes, or commentary.\n"
                "- Otherwise write only the message content itself, with no meta commentary such as "
                '"here is your message".\n'
                "- You MAY include URLs and links if relevant. "
                "Format for readability -- you can use line breaks.\n"
                f"- If you genuinely cannot produce the content, reply with only {NO_CONTENT_SENTINEL}"
            )

        return prompt
