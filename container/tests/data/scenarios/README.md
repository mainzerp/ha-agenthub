# Real-Pipeline Scenario Tests

YAML-driven end-to-end scenarios that exercise the production OrchestratorAgent
pipeline against a curated HA snapshot, a deterministic LLM stub, and an
in-memory recording HA client. No network, no real LLM, no real HA.

## Layout

```
data/
  ha_snapshots/        # Curated HA entity / area / device fixtures
  scenarios/           # YAML scenarios, one file per case
    light/
    climate/
    media/
    music/
    scene/
    security/
    automation/
    timer/
    general/
    orchestrator/
scenarios/             # Python framework (loader, runner, stubs)
test_real_scenarios.py # Parametrised pytest entry
```

## Running

```bash
# Just the real-scenario suite
python -m pytest tests/test_real_scenarios.py -v

# A single scenario
python -m pytest tests/test_real_scenarios.py -v -k turn_on_kitchen

# All tests except real scenarios
python -m pytest -m "not real_scenarios"
```

## YAML schema cheat sheet

```yaml
id: light.turn_on_kitchen          # Unique scenario id (also used for pytest id)
agent: light-agent                 # Documentation only -- expected.routed_agent enforces routing
description: "Short human prose"
snapshot: home_default             # Stem under data/ha_snapshots/
language: en                       # auto | en | de | ...

context:
  source: ha                       # ha | chat | api
  area_id: kitchen
  area_name: Kitchen
  device_id: satellite_kitchen
  device_name: "Kitchen Satellite"
  conversation_id: optional        # Defaults to scenario-<id>
  user_id: optional

preconditions:
  entity_overrides:
    - entity_id: light.kitchen_ceiling
      state: "off"
      attributes: {brightness: 0}
  settings:
    "entity_matching.confidence_threshold": "0.45"
  send_device_mappings:            # Reserved for send-agent scenarios
    - {alias: kitchen, entity_id: notify.kitchen}
  frozen_time: "2026-04-20T08:00:00Z"  # Reserved -- runner currently uses real clock

llm:
  classify: |                      # Reply for the orchestrator's classifier
    light-agent (95%): condensed task description
  agents:
    light-agent: |
      Free-form agent reply. Action is parsed from a fenced JSON block:
      ```json
      {"action": "turn_on", "entity": "kitchen ceiling", "parameters": {}}
      ```

request:
  text: "turn on the kitchen light"

expected:
  routed_agent: light-agent
  service_calls:
    - domain: light
      service: turn_on
      target_entity: light.kitchen_ceiling
      service_data_keys: ["brightness"]   # Required keys must be present
      service_data:                       # Required exact key/value matches
        brightness: 77
  speech_contains: ["kitchen"]
  speech_excludes: ["error"]
  action_executed: {action: turn_on}      # Subset match against response.action_executed
  error: {code: ENTITY_NOT_FOUND}         # Mutually exclusive with service_calls
  allow_extra_calls: false                # Set true to permit additional HA calls

follow_up:                                # Optional multi-turn dialogue
  - text: "make it red"
    llm:
      agents:
        light-agent: |
          ```json
          {"action": "set_color", "entity": "kitchen ceiling", "parameters": {"color_name": "red"}}
          ```
    expected:
      service_calls:
        - {domain: light, service: turn_on, target_entity: light.kitchen_ceiling}
```

Either `request: {text: "..."}` (preferred) or top-level `request_text` are
supported by the loader.

## Adding a scenario

1. Pick the right agent folder under `data/scenarios/`.
2. Confirm the entities you reference exist in `data/ha_snapshots/home_default.json`
   (extend the snapshot only if you genuinely need a new device class).
3. Use the action name expected by the executor, not the user's verb. For
   example, the timer agent expects `start_timer` not `start`; the scene
   agent expects `activate_scene` not `activate`.
4. Run `python -m pytest tests/test_real_scenarios.py -v -k <id-fragment>`
   and iterate.

## Debugging `LLMStubMissError`

The deterministic LLM stub raises `LLMStubMissError` when an agent calls
`complete(...)` and no reply has been queued for that `agent_id`. The error
includes the agent id, a prompt excerpt, and the scenario id. Common causes:

- Classifier reply uses the wrong agent id. The orchestrator asks under the
  agent id `"orchestrator"`; supply that via `llm.classify`.
- A follow-up turn forgot to enqueue a reply. Add another entry to
  `follow_up[i].llm.agents.<agent-id>`.
- An agent re-asked the LLM (e.g. for tool use). Either widen the queued
  replies list, or set a default with `LLMStubMissError`-aware tooling.

## Coverage status

The corpus now covers the full 78-scenario matrix (94 YAMLs total: 14 original
plus 80 net-new). Per-agent breakdown:

- light (13), climate (11), media (9), music (10), scene (5),
  security (11), automation (5), timer (10), general (6), send (5),
  orchestrator (9).

Four scenarios are marked `xfail: <reason>` for cases that require
framework features not wired yet: cache-replay paths (need a real
CacheManager), per-agent dispatch timeouts (need injected latency),
DelayedTaskManager-backed `delayed_action` / `sleep_timer`, and the
`security_executor` cross-domain matcher tiebreaker. Remaining 90
scenarios pass against the production pipeline. New files added to any
subdirectory under `data/scenarios/` are picked up automatically by
`tests/test_real_scenarios.py`.
