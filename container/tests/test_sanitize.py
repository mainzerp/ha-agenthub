"""FLOW-MED-4: canonical sanitizer corpus.

The container's :func:`app.agents.sanitize.strip_markdown` is the
canonical implementation. The HA custom_component's
``_strip_markdown`` in ``custom_components/ha_agenthub/conversation.py``
MUST produce identical output for every input in
``tests/data/sanitize_corpus.txt``. This test locks the container
side; HA parity is verified manually from the PR description.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.sanitize import strip_markdown

CORPUS_PATH = Path(__file__).parent / "data" / "sanitize_corpus.txt"


def _load_cases() -> list[tuple[str, str]]:
    """Parse the corpus into (input, expected) pairs.

    Format:
      # comment lines start with '#'
      INPUT_LINE(s)
      <blank line>
      EXPECTED_LINE(s)
      ---
    """
    raw = CORPUS_PATH.read_text(encoding="utf-8")
    # Strip leading comment header
    cleaned_lines = []
    for line in raw.splitlines():
        if line.startswith("#"):
            continue
        cleaned_lines.append(line)
    body = "\n".join(cleaned_lines).strip("\n")

    cases: list[tuple[str, str]] = []
    for block in body.split("\n---\n"):
        block = block.strip("\n")
        if not block:
            continue
        parts = block.split("\n\n", 1)
        if len(parts) != 2:
            continue
        input_text, expected_text = parts[0], parts[1]
        cases.append((input_text, expected_text))
    return cases


CASES = _load_cases()


@pytest.mark.parametrize("input_text,expected_text", CASES)
def test_strip_markdown_corpus(input_text: str, expected_text: str) -> None:
    assert strip_markdown(input_text) == expected_text


def test_corpus_is_nonempty() -> None:
    assert len(CASES) >= 10, "corpus should cover the main markdown constructs"


class TestStripMarkdown:
    """Tests for the strip_markdown TTS sanitization utility."""

    def test_empty_string(self):
        assert strip_markdown("") == ""

    def test_none_returns_none(self):
        assert strip_markdown(None) is None

    def test_plain_text_unchanged(self):
        text = "The weather today is sunny with a high of 72 degrees."
        assert strip_markdown(text) == text

    def test_strips_headers(self):
        assert strip_markdown("## Weather Today") == "Weather Today"
        assert strip_markdown("# Title\n## Subtitle") == "Title\nSubtitle"

    def test_strips_bold(self):
        assert strip_markdown("This is **important** info") == "This is important info"

    def test_strips_italic(self):
        assert strip_markdown("This is *emphasized* text") == "This is emphasized text"

    def test_strips_bold_italic(self):
        assert strip_markdown("This is ***very important***") == "This is very important"

    def test_strips_links(self):
        result = strip_markdown("Check [BBC News](https://bbc.com) for details")
        assert result == "Check BBC News for details"

    def test_strips_images(self):
        result = strip_markdown("![weather icon](https://example.com/icon.png)")
        assert result == "weather icon"

    def test_strips_inline_code(self):
        assert strip_markdown("Run `pip install`") == "Run pip install"

    def test_strips_code_blocks(self):
        text = "Example:\n```python\nprint('hello')\n```\nDone."
        result = strip_markdown(text)
        assert "```" not in result
        assert "print('hello')" in result

    def test_strips_bullet_lists(self):
        text = "Items:\n- First\n- Second\n- Third"
        result = strip_markdown(text)
        assert "- " not in result
        assert "First" in result

    def test_strips_numbered_lists(self):
        text = "Steps:\n1. First\n2. Second"
        result = strip_markdown(text)
        assert "1. " not in result
        assert "First" in result

    def test_strips_horizontal_rules(self):
        text = "Section one\n---\nSection two"
        result = strip_markdown(text)
        assert "---" not in result

    def test_strips_html_tags(self):
        assert strip_markdown("Hello<br>World") == "HelloWorld"

    def test_strips_bare_urls(self):
        text = "Visit https://example.com/long/path for more"
        result = strip_markdown(text)
        assert "https://" not in result

    def test_strips_blockquotes(self):
        assert strip_markdown("> This is a quote") == "This is a quote"

    def test_strips_strikethrough(self):
        assert strip_markdown("~~old~~ new") == "old new"

    def test_collapses_whitespace(self):
        text = "Hello\n\n\n\nWorld"
        result = strip_markdown(text)
        assert "\n\n\n" not in result

    def test_complex_web_search_response(self):
        text = (
            "## Weather in Berlin\n\n"
            "According to **Weather.com**, the current temperature is *15C*.\n\n"
            "- Humidity: 60%\n"
            "- Wind: 10 km/h\n\n"
            "Source: [Weather.com](https://weather.com/berlin)\n"
        )
        result = strip_markdown(text)
        assert "##" not in result
        assert "**" not in result
        assert "*" not in result
        assert "[" not in result
        assert "https://" not in result
        assert "- " not in result
        assert "Weather in Berlin" in result
        assert "15C" in result
