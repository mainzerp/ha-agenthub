"""Self-description tool for the general agent.

Answers questions about HA-AgentHub itself from two sources:

* ``overview``: a runtime snapshot (product, version, registered agents,
  loaded plugins, MCP server names and connection state). It never includes
  entities, settings values, URLs, commands or credentials.
* ``docs``: lexical search over the user docs bundled into the image at
  ``app/knowledge/`` (copied by ``scripts/sync_self_docs.py``).

Lives next to ``tool_calling`` because it is an agent-side tool, not a
domain service; only ``general-agent`` registers it.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agents.tool_calling import MCP_TOOL_RESULT_MAX_CHARS, LocalTool
from app.models.agent import INTERNAL_ONLY_AGENTS

logger = logging.getLogger(__name__)

SELF_INFO_TOOL_NAME = "agenthub_self_info"
PRODUCT_NAME = "HA-AgentHub"
PRODUCT_PURPOSE = (
    "a multi-agent Home Assistant assistant. A container execution engine classifies each request, "
    "routes it to a specialized agent and executes Home Assistant actions; a thin Home Assistant "
    "integration bridges conversations and voice to it. An admin dashboard configures agents, LLM "
    "models, MCP servers, plugins, caching and voice features."
)

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"

# Total characters of one docs answer; stays below the executor cut.
DOCS_RESULT_BUDGET_CHARS = 6000
DEFAULT_DOCS_LIMIT = 4
MAX_DOCS_LIMIT = 8
_MIN_PARTIAL_SECTION_CHARS = 400
_MAX_DESCRIPTION_CHARS = 300

_GENERATED_MARKER_RE = re.compile(r"^\s*<!--\s*GENERATED\b.*-->\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_IMAGE_LINE_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
# Doc sections that are pure navigation noise for a spoken answer.
_SKIPPED_HEADINGS = frozenset({"appendix: screenshot index"})

# Filler words dropped from queries; the query language is English (the
# docs are English), so this is lexical hygiene, not intent routing.
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "can",
        "do",
        "does",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "the",
        "to",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "you",
        "your",
        "this",
        "that",
        "be",
    ]
)
_SUFFIXES = ("ations", "ation", "ings", "ing", "ers", "ies", "er", "es", "ed", "s")
_MIN_PREFIX_LEN = 4
# Heading bonus (in term-frequency units) for the section's own heading and
# for its ancestor headings; BM25 parameters for the body.
_OWN_HEADING_WEIGHT = 3.0
_PARENT_HEADING_WEIGHT = 1.0
_PREFIX_WEIGHT = 0.5
_BM25_K1 = 1.2
_BM25_B = 0.75


@dataclass(frozen=True)
class DocSection:
    """One heading-delimited section of a bundled doc."""

    source: str
    heading_path: tuple[str, ...]
    body: str
    body_stems: Counter = field(default_factory=Counter, compare=False, repr=False)
    own_heading_stems: frozenset[str] = field(default_factory=frozenset, compare=False, repr=False)
    parent_heading_stems: frozenset[str] = field(default_factory=frozenset, compare=False, repr=False)

    @property
    def body_length(self) -> int:
        return sum(self.body_stems.values())

    @property
    def label(self) -> str:
        path = " > ".join(self.heading_path)
        return f"{self.source} > {path}" if path else self.source


def stem(word: str) -> str:
    """Lower-case a word and strip one common English suffix."""
    word = word.lower()
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            if suffix == "ies":
                return word[:-3] + "y"
            return word[: -len(suffix)]
    return word


def _stems(text: str) -> list[str]:
    return [stem(word) for word in _WORD_RE.findall(text)]


def _make_section(source: str, heading_path: tuple[str, ...], lines: list[str]) -> DocSection | None:
    body = "\n".join(lines).strip()
    if not body:
        return None
    if any(heading.lower() in _SKIPPED_HEADINGS for heading in heading_path):
        return None
    return DocSection(
        source=source,
        heading_path=heading_path,
        body=body,
        body_stems=Counter(_stems(body)),
        own_heading_stems=frozenset(_stems(heading_path[-1])) if heading_path else frozenset(),
        parent_heading_stems=frozenset(_stems(" ".join(heading_path[:-1]))),
    )


def parse_markdown_sections(source: str, text: str) -> list[DocSection]:
    """Split markdown into sections keyed by their heading path.

    Headings inside fenced code blocks are content, not structure. The
    generated-file marker and standalone image lines are dropped.
    """
    sections: list[DocSection] = []
    stack: list[tuple[int, str]] = []
    lines: list[str] = []
    in_fence = False

    def flush() -> None:
        section = _make_section(source, tuple(title for _, title in stack), lines)
        if section is not None:
            sections.append(section)
        lines.clear()

    for line in text.replace("\r\n", "\n").split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            lines.append(line)
            continue
        if not in_fence:
            if _GENERATED_MARKER_RE.match(line) or _IMAGE_LINE_RE.match(line):
                continue
            heading = _HEADING_RE.match(line)
            if heading:
                flush()
                level = len(heading.group(1))
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, heading.group(2).strip()))
                continue
        lines.append(line)
    flush()
    return sections


def load_sections(knowledge_dir: Path = KNOWLEDGE_DIR) -> list[DocSection]:
    """Parse every bundled markdown file (blocking; call off the event loop)."""
    sections: list[DocSection] = []
    if not knowledge_dir.is_dir():
        logger.warning("Bundled self-docs directory missing: %s", knowledge_dir)
        return sections
    for path in sorted(knowledge_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("Failed to read bundled doc %s", path, exc_info=True)
            continue
        sections.extend(parse_markdown_sections(path.name, text))
    return sections


_sections_cache: dict[Path, list[DocSection]] = {}


async def get_sections(knowledge_dir: Path = KNOWLEDGE_DIR) -> list[DocSection]:
    """Return the parsed bundled docs, loading them once off the event loop."""
    cached = _sections_cache.get(knowledge_dir)
    if cached is None:
        cached = await asyncio.to_thread(load_sections, knowledge_dir)
        _sections_cache[knowledge_dir] = cached
    return cached


def _query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for word in _WORD_RE.findall(query.lower()):
        if word in _STOPWORDS:
            continue
        term = stem(word)
        if term and term not in terms:
            terms.append(term)
    return terms


def _term_frequency(section: DocSection, term: str) -> float:
    """Body frequency of ``term``: exact stem, else down-weighted prefix matches."""
    count = section.body_stems.get(term, 0)
    if count or len(term) < _MIN_PREFIX_LEN:
        return float(count)
    prefix_count = sum(
        n
        for word, n in section.body_stems.items()
        if word.startswith(term) or (len(word) >= _MIN_PREFIX_LEN and term.startswith(word))
    )
    return _PREFIX_WEIGHT * prefix_count


def _heading_bonus(section: DocSection, term: str) -> float:
    def hit(stems: frozenset[str]) -> bool:
        if term in stems:
            return True
        return len(term) >= _MIN_PREFIX_LEN and any(word.startswith(term) for word in stems)

    if hit(section.own_heading_stems):
        return _OWN_HEADING_WEIGHT
    if hit(section.parent_heading_stems):
        return _PARENT_HEADING_WEIGHT
    return 0.0


def rank_sections(sections: list[DocSection], query: str) -> list[tuple[float, DocSection]]:
    """Score sections against ``query`` (BM25 body + heading bonus); highest first, zeros dropped."""
    terms = _query_terms(query)
    if not terms or not sections:
        return []
    total = len(sections)
    avg_len = max(1.0, sum(s.body_length for s in sections) / total)
    doc_freq = {term: sum(1 for s in sections if term in s.body_stems or term in s.own_heading_stems) for term in terms}
    ranked: list[tuple[float, DocSection]] = []
    for section in sections:
        norm = _BM25_K1 * (1.0 - _BM25_B + _BM25_B * section.body_length / avg_len)
        score = 0.0
        matched = 0
        for term in terms:
            tf = _term_frequency(section, term)
            bonus = _heading_bonus(section, term)
            if not tf and not bonus:
                continue
            matched += 1
            idf = math.log(1.0 + (total - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5))
            score += idf * (tf * (_BM25_K1 + 1.0) / (tf + norm) + bonus)
        if score > 0:
            # Sections covering more distinct query terms rank higher.
            score *= 1.0 + matched / len(terms)
            ranked.append((score, section))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked


def format_doc_hits(hits: Iterable[DocSection], query: str, budget: int = DOCS_RESULT_BUDGET_CHARS) -> str:
    """Render search hits labeled with source and heading path, within ``budget`` chars."""
    parts = [f"HA-AgentHub documentation excerpts for '{query}' (most relevant first):"]
    used = len(parts[0])
    for section in hits:
        header = f"\n\n[{section.label}]\n"
        remaining = budget - used - len(header)
        if remaining <= 0:
            break
        body = section.body
        if len(body) > remaining:
            if remaining < _MIN_PARTIAL_SECTION_CHARS and len(parts) > 1:
                break
            body = body[: max(0, remaining - 4)].rstrip() + " ..."
        parts.append(header + body)
        used += len(header) + len(body)
    return "".join(parts)


async def search_docs(
    query: str,
    limit: int = DEFAULT_DOCS_LIMIT,
    *,
    knowledge_dir: Path = KNOWLEDGE_DIR,
    budget: int = DOCS_RESULT_BUDGET_CHARS,
) -> str:
    """Search the bundled docs; return labeled top sections or a no-match message."""
    query = (query or "").strip()
    if not query:
        return "No query given. Call again with topic 'docs' and a few English keywords, for example 'add MCP server'."
    sections = await get_sections(knowledge_dir)
    if not sections:
        return "The bundled HA-AgentHub documentation is not available in this installation."
    limit = max(1, min(int(limit or DEFAULT_DOCS_LIMIT), MAX_DOCS_LIMIT))
    ranked = rank_sections(sections, query)
    if not ranked:
        return (
            f"No matching section in the HA-AgentHub documentation for '{query}'. "
            "Try other English keywords, or tell the user this is not covered by the documentation."
        )
    return format_doc_hits([section for _, section in ranked[:limit]], query, budget)


def _short(text: Any, limit: int = _MAX_DESCRIPTION_CHARS) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 3].rstrip() + "..."


def build_overview(
    *,
    version: str,
    agents: list[Any] | None = None,
    plugins: list[Any] | None = None,
    mcp_servers: list[dict[str, Any]] | None = None,
) -> str:
    """Plain-text runtime overview; ``None`` omits that part.

    ``agents`` are AgentCards (internal-only pipeline agents are skipped),
    ``plugins`` are BasePlugin instances, ``mcp_servers`` are
    ``{"name", "connected"}`` dicts. Only names, descriptions, skills,
    versions and connection state are rendered.
    """
    lines = [f"{PRODUCT_NAME} version {version}: {PRODUCT_PURPOSE}"]

    if agents is not None:
        visible = sorted(
            (card for card in agents if getattr(card, "agent_id", None) not in INTERNAL_ONLY_AGENTS),
            key=lambda card: card.agent_id,
        )
        lines.append("")
        lines.append(f"Agents ({len(visible)}):" if visible else "Agents: none registered.")
        for card in visible:
            skills = ", ".join(getattr(card, "skills", None) or [])
            entry = f"- {card.agent_id} ({_short(card.name, 80)}): {_short(card.description)}"
            if skills:
                entry += f" Skills: {skills}."
            lines.append(entry)

    if plugins is not None:
        lines.append("")
        lines.append(f"Plugins ({len(plugins)}):" if plugins else "Plugins: none loaded.")
        for plugin in plugins:
            name = _short(getattr(plugin, "name", "") or "unnamed", 80)
            version_str = _short(getattr(plugin, "version", "") or "", 40)
            description = _short(getattr(plugin, "description", "") or "")
            entry = f"- {name}" + (f" {version_str}" if version_str else "")
            if description:
                entry += f": {description}"
            lines.append(entry)

    if mcp_servers is not None:
        lines.append("")
        lines.append(f"MCP servers ({len(mcp_servers)}):" if mcp_servers else "MCP servers: none configured.")
        for server in mcp_servers:
            state = "connected" if server.get("connected") else "disconnected"
            lines.append(f"- {_short(server.get('name', ''), 80)}: {state}")

    lines.append("")
    lines.append(
        "For how to use, configure or troubleshoot HA-AgentHub, call this tool with topic 'docs' and English keywords."
    )
    return "\n".join(lines)


_TOOL_DESCRIPTION = (
    "Information about HA-AgentHub, the assistant you are part of. Use it for questions about the assistant "
    "itself: what it is, which agents, capabilities, plugins and MCP servers it has, and how to use, configure "
    "or troubleshoot it. topic 'overview' returns the product summary, version and the active agents, plugins "
    "and MCP servers; topic 'docs' searches the HA-AgentHub user documentation (English) with 'query'. "
    "Do not use it for general knowledge or smart-home device state."
)

_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "topic": {
            "type": "string",
            "enum": ["overview", "docs"],
            "description": "'overview' for what HA-AgentHub is and what it can do; 'docs' for how-to, "
            "configuration and troubleshooting questions.",
        },
        "query": {
            "type": "string",
            "description": "For topic 'docs': a few English keywords (the documentation is English), "
            "for example 'add MCP server' or 'voice follow-up not working'. Translate the user's words.",
        },
    },
    "required": ["topic"],
}


class SelfInfoProvider:
    """Executes the ``agenthub_self_info`` tool against live runtime registries.

    Every dependency is optional; a missing one omits its overview part.
    ``plugin_loader_provider`` is a callable because plugins load after the
    agents are constructed.
    """

    def __init__(
        self,
        *,
        agent_registry: Any = None,
        mcp_registry: Any = None,
        plugin_loader_provider: Callable[[], Any] | None = None,
        knowledge_dir: Path = KNOWLEDGE_DIR,
        version: str | None = None,
    ) -> None:
        self._agent_registry = agent_registry
        self._mcp_registry = mcp_registry
        self._plugin_loader_provider = plugin_loader_provider
        self._knowledge_dir = knowledge_dir
        self._version = version

    def as_local_tool(self) -> LocalTool:
        return LocalTool(
            name=SELF_INFO_TOOL_NAME,
            description=_TOOL_DESCRIPTION,
            parameters=_TOOL_PARAMETERS,
            handler=self.handle,
        )

    async def handle(self, arguments: dict[str, Any]) -> str:
        topic = str(arguments.get("topic") or "").strip().lower()
        query = str(arguments.get("query") or "").strip()
        if not topic:
            topic = "docs" if query else "overview"
        if topic == "overview":
            return await self.overview()
        if topic == "docs":
            return await search_docs(query, knowledge_dir=self._knowledge_dir)
        return f"Error: unknown topic '{topic}'. Use 'overview' or 'docs'."

    async def overview(self) -> str:
        version = self._version
        if version is None:
            from app import __version__

            version = __version__
        text = build_overview(
            version=version,
            agents=await self._list_agents(),
            plugins=self._list_plugins(),
            mcp_servers=self._list_mcp_servers(),
        )
        return text[:MCP_TOOL_RESULT_MAX_CHARS]

    async def _list_agents(self) -> list[Any] | None:
        if self._agent_registry is None:
            return None
        try:
            return list(await self._agent_registry.list_agents())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Self-info: listing agents failed", exc_info=True)
            return None

    def _list_plugins(self) -> list[Any] | None:
        if self._plugin_loader_provider is None:
            return None
        try:
            loader = self._plugin_loader_provider()
            if loader is None:
                return None
            loaded = loader.loaded_plugins
            return list(loaded.values()) if isinstance(loaded, dict) else list(loaded or [])
        except Exception:
            logger.warning("Self-info: listing plugins failed", exc_info=True)
            return None

    def _list_mcp_servers(self) -> list[dict[str, Any]] | None:
        if self._mcp_registry is None:
            return None
        try:
            # Only name + connection state leave this function.
            return [
                {"name": str(item.get("name", "")), "connected": bool(item.get("connected"))}
                for item in self._mcp_registry.list_servers()
            ]
        except Exception:
            logger.warning("Self-info: listing MCP servers failed", exc_info=True)
            return None
