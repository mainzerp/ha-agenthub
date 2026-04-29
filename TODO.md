# TODO

## Pending Features

- [ ] **HA Entities auf Exposed Entities beschraenken**: Entity-Zugriff auf die in Home Assistant als "exposed" markierten Entities einschraenken, damit nur freigegebene Geraete gesteuert/abgefragt werden koennen.

- [ ] **Nutzer- und Agent-Memory**: Persistente Profile, Memory-Tool (speichern/abrufen/aktualisieren), Limits/Eviction, optional UI im Dashboard; Mehrschichtige Nutzerzuordnung wo sinnvoll.

- [x] **Cancel-Intent / Dismiss**: Orchestrator-LLM routet zu virtuellem Agent **cancel-interaction**; der Container liefert eine kurze, LLM-generierte gesprochene Bestaetigung **ohne** Domain-Dispatch (z. B. fuer "Abbrechen", "Vergiss es"). Nutzt die `filler-agent`-Konfiguration mit per-call Guardrails. Hartes Timeout + deterministischer Fallback ("Okay." / "Alles klar.") verhindert haengende Sprachsatelliten. HA-Integration leitet den User-Text immer an den Container weiter (kein lokales Keyword-Shortcut).

- [ ] **HA-Service fuer Automationen (`ai_task`-Aequivalent)**: Service oder klarer Contract fuer Automatisierungen (z. B. strukturierter Output / `generate_data`-Pattern), der den Container ohne manuelles HTTP-Basteln nutzbar macht.

security agent:
sentinel mode bleibt vorerst deferred. wenn ein/oder mehrere explizit dem security agent zugewiesene sensoren (z. b. bewegungsmelder) automatisch einen security-agent-run triggern sollen, braucht das einen separaten trigger-contract und wahrscheinlich eine eigene ui page.

Debug-Logging aktivieren

Aktiviert ausfuehrliches Logging zur Fehlersuche.


logs endpoint um system von remote debuggen zu können


ggf auch ein fehler

ha-agenthub  | 2026-04-29 19:44:02,257 INFO [app.middleware.tracing] [6c13eebeb8f9496d] GET /api/admin/traces/1829efb48d524fc4 -> 200 (17.0ms)
ha-agenthub  | 2026-04-29 19:44:09,568 INFO [app.middleware.tracing] [c6ea9cf5f8ad4089] GET /api/health started
ha-agenthub  | INFO:     127.0.0.1:43748 - "GET /api/health HTTP/1.1" 200 OK
ha-agenthub  | 2026-04-29 19:44:09,570 INFO [app.middleware.tracing] [c6ea9cf5f8ad4089] GET /api/health -> 200 (0.2ms)
ha-agenthub  | 19:44:16 - LiteLLM:INFO: utils.py:4004 - 
ha-agenthub  | LiteLLM completion() model= openai/gpt-oss-120b; provider = groq
ha-agenthub  | 2026-04-29 19:44:16,395 INFO [LiteLLM] 
ha-agenthub  | LiteLLM completion() model= openai/gpt-oss-120b; provider = groq
ha-agenthub  | 19:44:16 - LiteLLM:INFO: utils.py:4004 - 
ha-agenthub  | LiteLLM completion() model= openai/gpt-oss-20b; provider = groq
ha-agenthub  | 2026-04-29 19:44:16,568 INFO [LiteLLM] 
ha-agenthub  | LiteLLM completion() model= openai/gpt-oss-20b; provider = groq
ha-agenthub  | 2026-04-29 19:44:16,717 ERROR [app.llm.client] LLM call failed for agent=light-agent model=groq/openai/gpt-oss-20b
ha-agenthub  | Traceback (most recent call last):
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/litellm/llms/custom_httpx/llm_http_handler.py", line 175, in _make_common_async_call
ha-agenthub  |     response = await async_httpx_client.post(
ha-agenthub  |                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/litellm/litellm_core_utils/logging_utils.py", line 297, in async_wrapper
ha-agenthub  |     result = await func(*args, **kwargs)
ha-agenthub  |              ^^^^^^^^^^^^^^^^^^^^^^^^^^^
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/litellm/llms/custom_httpx/http_handler.py", line 513, in post
ha-agenthub  |     raise e
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/litellm/llms/custom_httpx/http_handler.py", line 469, in post
ha-agenthub  |     response.raise_for_status()
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/httpx/_models.py", line 829, in raise_for_status
ha-agenthub  |     raise HTTPStatusError(message, request=request, response=self)
ha-agenthub  | httpx.HTTPStatusError: Client error '400 Bad Request' for url 'https://api.groq.com/openai/v1/chat/completions'
ha-agenthub  | For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/400
ha-agenthub  | 
ha-agenthub  | During handling of the above exception, another exception occurred:
ha-agenthub  | 
ha-agenthub  | Traceback (most recent call last):
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/litellm/main.py", line 620, in acompletion
ha-agenthub  |     response = await init_response
ha-agenthub  |                ^^^^^^^^^^^^^^^^^^^
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/litellm/llms/custom_httpx/llm_http_handler.py", line 307, in async_completion
ha-agenthub  |     response = await self._make_common_async_call(
ha-agenthub  |                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/litellm/llms/custom_httpx/llm_http_handler.py", line 200, in _make_common_async_call
ha-agenthub  |     raise self._handle_error(e=e, provider_config=provider_config)
ha-agenthub  |           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/litellm/llms/custom_httpx/llm_http_handler.py", line 4720, in _handle_error
ha-agenthub  |     raise provider_config.get_error_class(
ha-agenthub  | litellm.llms.openai.common_utils.OpenAIError: {"error":{"message":"Tool choice is none, but model called a tool","type":"invalid_request_error","code":"tool_use_failed","failed_generation":"{\"name\": \"light_control_agent\", \"arguments\": {\"action\":\"turn_off\",\"entity\":\"Keller\",\"parameters\":{}}}"}}
ha-agenthub  | 
ha-agenthub  | 
ha-agenthub  | During handling of the above exception, another exception occurred:
ha-agenthub  | 
ha-agenthub  | Traceback (most recent call last):
ha-agenthub  |   File "/app/app/llm/client.py", line 65, in complete
ha-agenthub  |     response = await litellm.acompletion(**call_kwargs)
ha-agenthub  |                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/litellm/utils.py", line 2090, in wrapper_async
ha-agenthub  |     raise e
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/litellm/utils.py", line 1889, in wrapper_async
ha-agenthub  |     result = await original_function(*args, **kwargs)
ha-agenthub  |              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/litellm/main.py", line 639, in acompletion
ha-agenthub  |     raise exception_type(
ha-agenthub  |           ^^^^^^^^^^^^^^^
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/litellm/litellm_core_utils/exception_mapping_utils.py", line 2456, in exception_type
ha-agenthub  |     raise e
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/litellm/litellm_core_utils/exception_mapping_utils.py", line 478, in exception_type
ha-agenthub  |     raise BadRequestError(
ha-agenthub  | litellm.exceptions.BadRequestError: litellm.BadRequestError: GroqException - {"error":{"message":"Tool choice is none, but model called a tool","type":"invalid_request_error","code":"tool_use_failed","failed_generation":"{\"name\": \"light_control_agent\", \"arguments\": {\"action\":\"turn_off\",\"entity\":\"Keller\",\"parameters\":{}}}"}}
ha-agenthub  | 
ha-agenthub  | 2026-04-29 19:44:16,719 ERROR [app.agents.actionable] LLM call failed for light-agent
ha-agenthub  | Traceback (most recent call last):
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/litellm/llms/custom_httpx/llm_http_handler.py", line 175, in _make_common_async_call
ha-agenthub  |     response = await async_httpx_client.post(
ha-agenthub  |                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/litellm/litellm_core_utils/logging_utils.py", line 297, in async_wrapper
ha-agenthub  |     result = await func(*args, **kwargs)
ha-agenthub  |              ^^^^^^^^^^^^^^^^^^^^^^^^^^^
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/litellm/llms/custom_httpx/http_handler.py", line 513, in post
ha-agenthub  |     raise e
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/litellm/llms/custom_httpx/http_handler.py", line 469, in post
ha-agenthub  |     response.raise_for_status()
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/httpx/_models.py", line 829, in raise_for_status
ha-agenthub  |     raise HTTPStatusError(message, request=request, response=self)
ha-agenthub  | httpx.HTTPStatusError: Client error '400 Bad Request' for url 'https://api.groq.com/openai/v1/chat/completions'
ha-agenthub  | For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/400
ha-agenthub  | 
ha-agenthub  | During handling of the above exception, another exception occurred:
ha-agenthub  | 
ha-agenthub  | Traceback (most recent call last):
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/litellm/main.py", line 620, in acompletion
ha-agenthub  |     response = await init_response
ha-agenthub  |                ^^^^^^^^^^^^^^^^^^^
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/litellm/llms/custom_httpx/llm_http_handler.py", line 307, in async_completion
ha-agenthub  |     response = await self._make_common_async_call(
ha-agenthub  |                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/litellm/llms/custom_httpx/llm_http_handler.py", line 200, in _make_common_async_call
ha-agenthub  |     raise self._handle_error(e=e, provider_config=provider_config)
ha-agenthub  |           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/litellm/llms/custom_httpx/llm_http_handler.py", line 4720, in _handle_error
ha-agenthub  |     raise provider_config.get_error_class(
ha-agenthub  | litellm.llms.openai.common_utils.OpenAIError: {"error":{"message":"Tool choice is none, but model called a tool","type":"invalid_request_error","code":"tool_use_failed","failed_generation":"{\"name\": \"light_control_agent\", \"arguments\": {\"action\":\"turn_off\",\"entity\":\"Keller\",\"parameters\":{}}}"}}
ha-agenthub  | 
ha-agenthub  | 
ha-agenthub  | During handling of the above exception, another exception occurred:
ha-agenthub  | 
ha-agenthub  | Traceback (most recent call last):
ha-agenthub  |   File "/app/app/agents/actionable.py", line 116, in _handle_task_inner
ha-agenthub  |     response = await self._call_llm(messages, span_collector=span_collector)
ha-agenthub  |                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
ha-agenthub  |   File "/app/app/agents/base.py", line 244, in _call_llm
ha-agenthub  |     return await complete(self.agent_card.agent_id, self._normalize_llm_messages(messages), **overrides)
ha-agenthub  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
ha-agenthub  |   File "/app/app/llm/client.py", line 65, in complete
ha-agenthub  |     response = await litellm.acompletion(**call_kwargs)
ha-agenthub  |                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/litellm/utils.py", line 2090, in wrapper_async
ha-agenthub  |     raise e
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/litellm/utils.py", line 1889, in wrapper_async
ha-agenthub  |     result = await original_function(*args, **kwargs)
ha-agenthub  |              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/litellm/main.py", line 639, in acompletion
ha-agenthub  |     raise exception_type(
ha-agenthub  |           ^^^^^^^^^^^^^^^
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/litellm/litellm_core_utils/exception_mapping_utils.py", line 2456, in exception_type
ha-agenthub  |     raise e
ha-agenthub  |   File "/usr/local/lib/python3.12/site-packages/litellm/litellm_core_utils/exception_mapping_utils.py", line 478, in exception_type
ha-agenthub  |     raise BadRequestError(
ha-agenthub  | litellm.exceptions.BadRequestError: litellm.BadRequestError: GroqException - {"error":{"message":"Tool choice is none, but model called a tool","type":"invalid_request_error","code":"tool_use_failed","failed_generation":"{\"name\": \"light_control_agent\", \"arguments\": {\"action\":\"turn_off\",\"entity\":\"Keller\",\"parameters\":{}}}"}}
ha-agenthub  | 
ha-agenthub  | 19:44:16 - LiteLLM:INFO: utils.py:4004 - 
ha-agenthub  | LiteLLM completion() model= openai/gpt-oss-120b; provider = groq
ha-agenthub  | 2026-04-29 19:44:16,813 INFO [LiteLLM] 
ha-agenthub  | LiteLLM completion() model= openai/gpt-oss-120b; provider = groq
ha-agenthub  | 2026-04-29 19:44:20,759 INFO [app.middleware.tracing] [37c3ee5f40b94312] GET /dashboard/traces started
ha-agenthub  | INFO:     192.168.120.43:13636 - "GET /dashboard/traces HTTP/1.1" 200 OK
ha-agenthub  | 2026-04-29 19:44:20,772 INFO [app.middleware.tracing] [37c3ee5f40b94312] GET /dashboard/traces -> 200 (2.7ms)
ha-agenthub  | 2026-04-29 19:44:20,793 INFO [app.middleware.tracing] [c7b985928b5e4922] GET /dashboard/static/style.css started
ha-agenthub  | INFO:     192.168.120.43:13636 - "GET /dashboard/static/style.css?v=8 HTTP/1.1" 304 Not Modified
ha-agenthub  | 2026-04-29 19:44:20,796 INFO [app.middleware.tracing] [c7b985928b5e4922] GET /dashboard/static/style.css -> 304 (1.2ms)
ha-agenthub  | 2026-04-29 19:44:20,980 INFO [app.middleware.tracing] [7734d7e95d104f5e] GET /api/admin/traces started
ha-agenthub  | INFO:     192.168.120.43:13636 - "GET /api/admin/traces?page=1&