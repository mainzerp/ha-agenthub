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
                "\n\nThis response will be delivered as text to a device (not spoken aloud). "
                "You MAY include URLs and links if relevant. "
                "Format for readability -- you can use line breaks."
            )

        return prompt
