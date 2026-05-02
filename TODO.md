# TODO

## Pending Features

- [ ] **P1 -- Restrict HA entities to exposed entities only**: Limit entity access to devices marked as "exposed" in Home Assistant so only allowed devices can be controlled or queried.

- [ ] **P1 -- User and agent memory**: Persistent profiles, memory tool (save/retrieve/update), limits/eviction, optional dashboard UI; multi-layer user mapping where appropriate.

- [ ] **P2 -- HA service for automations (`ai_task` equivalent)**: Service or clear contract for automations (e.g. structured output / `generate_data` pattern) that makes the container usable without manual HTTP construction.

- [ ] **P2 -- Security agent sentinel mode**: Deferred. If one or more sensors explicitly assigned to the security agent should automatically trigger a security-agent run, that requires a separate trigger contract and likely a dedicated UI page.
