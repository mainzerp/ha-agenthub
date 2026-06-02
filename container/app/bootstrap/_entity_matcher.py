"""Bootstrap: EntityMatcher creation with optional query expansion wiring."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from app.entity.matcher import EntityMatcher

if TYPE_CHECKING:
    from fastapi import FastAPI

    from app.entity.aliases import AliasResolver
    from app.entity.index import EntityIndex
    from app.ha_client.rest import HARestClient

logger = logging.getLogger(__name__)


async def setup_entity_matcher(
    app: FastAPI,
    source: str,
    ha_client: HARestClient,
    entity_index: EntityIndex,
    alias_resolver: AliasResolver,
) -> EntityMatcher:
    """Create EntityMatcher, configure expansion service, load language.

    Stores on ``app.state.entity_matcher``.
    """
    entity_matcher = getattr(app.state, "entity_matcher", None)
    if entity_matcher is None:
        entity_matcher = EntityMatcher(entity_index, alias_resolver)
        await entity_matcher.load_config()
        # 0.23.0: wire optional language-agnostic on-demand expansion.
        try:
            from app.entity.expansion import (
                QueryExpansionService,
                load_query_expansion_prompt_template,
            )

            async def _llm_expand(prompt: str) -> str:
                # Use the orchestrator-tier LLM for expansion (cheap,
                # cached). Fail-soft: any error returns empty string so
                # the matcher falls through.
                try:
                    from app.llm.client import complete

                    return await complete(
                        "orchestrator",
                        [{"role": "user", "content": prompt}],
                        max_tokens=200,
                        temperature=0.0,
                    )
                except Exception:
                    return ""

            prompt_template = await asyncio.to_thread(load_query_expansion_prompt_template)
            entity_matcher._expansion_service = QueryExpansionService(
                llm_call=_llm_expand,
                prompt_template=prompt_template,
            )
        except Exception:
            logger.debug("Expansion service wiring skipped", exc_info=True)
        try:
            entity_matcher._index_language = await ha_client.get_user_language()
        except Exception:
            entity_matcher._index_language = None
        app.state.entity_matcher = entity_matcher

    return entity_matcher
