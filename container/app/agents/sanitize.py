"""Lightweight Markdown stripping for TTS-friendly output."""

from __future__ import annotations

import re


def strip_parenthetical_asides(text: str) -> str:
    """Remove parenthetical asides and meta-commentary from TTS output.

    Strips blocks like ``(I corrected the name...)`` and collapses
    leftover whitespace so the result is clean spoken text.
    """
    if not text:
        return text
    text = re.sub(r"\s*\([^)]*\)", "", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def strip_markdown(text: str) -> str:
    """Remove common Markdown formatting artifacts for TTS clarity.

    Handles: headers, bold/italic, links, images, code blocks/inline code,
    horizontal rules, bullet/numbered lists, HTML tags, and excessive whitespace.
    """
    if not text:
        return text

    # Fenced code blocks: ```lang\ncode\n``` -> code content only
    text = re.sub(r"```[a-zA-Z]*\n?", "", text)

    # Inline code: `code` -> code
    text = re.sub(r"`([^`]+)`", r"\1", text)

    # Images: ![alt](url) -> alt
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)

    # Links: [text](url) -> text
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)

    # Reference links: [text][ref] -> text
    text = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", text)

    # Headers: ## Header -> Header
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)

    # Bold/italic: ***text***, **text**, *text*, ___text___, __text__, _text_
    text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}([^_]+)_{1,3}", r"\1", text)

    # Strikethrough: ~~text~~ -> text
    text = re.sub(r"~~([^~]+)~~", r"\1", text)

    # Horizontal rules: --- or *** or ___ (standalone lines)
    text = re.sub(r"^[\s]*([-*_]){3,}\s*$", "", text, flags=re.MULTILINE)

    # Bullet list markers: - item or * item -> item
    text = re.sub(r"^[\s]*[-*+]\s+", "", text, flags=re.MULTILINE)

    # Numbered list markers: 1. item -> item
    text = re.sub(r"^[\s]*\d+\.\s+", "", text, flags=re.MULTILINE)

    # Blockquotes: > text -> text
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)

    # HTML tags: <br>, <b>, </b>, etc.
    text = re.sub(r"<[^>]+>", "", text)

    # Remaining standalone URLs (bare urls not already handled by link removal)
    # Keep the domain for context: https://www.example.com/long/path -> example.com
    # Actually, just remove bare URLs entirely -- they are useless in TTS
    text = re.sub(r"https?://\S+", "", text)

    # Collapse multiple blank lines into one
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Collapse multiple spaces into one
    text = re.sub(r" {2,}", " ", text)

    # Strip leading/trailing whitespace per line, then overall
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(lines).strip()

    return text
