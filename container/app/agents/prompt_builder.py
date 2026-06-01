"""Shared system-prompt builder used by GeneralAgent and DynamicAgent."""

from __future__ import annotations


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
                f"\n\nIMPORTANT: Respond in {language}. "
                f"The user's language is {language}. "
                "Keep entity names, device names, and room names exactly as the user wrote them -- "
                "do NOT translate those."
            )

        if sequential_send:
            prompt += (
                "\n\nThis response will be delivered as text to a device (not spoken aloud). "
                "You MAY include URLs and links if relevant. "
                "Format for readability -- you can use line breaks."
            )

        return prompt
