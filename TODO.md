# TODO

## Pending Features

- [ ] **HA Entities auf Exposed Entities beschraenken**: Entity-Zugriff auf die in Home Assistant als "exposed" markierten Entities einschraenken, damit nur freigegebene Geraete gesteuert/abgefragt werden koennen.

- [ ] **Nutzer- und Agent-Memory**: Persistente Profile, Memory-Tool (speichern/abrufen/aktualisieren), Limits/Eviction, optional UI im Dashboard; Mehrschichtige Nutzerzuordnung wo sinnvoll.

- [x] **Cancel-Intent / Dismiss**: Orchestrator-LLM routet zu virtuellem Agent **cancel-interaction**; der Container liefert eine kurze, LLM-generierte gesprochene Bestaetigung **ohne** Domain-Dispatch (z. B. fuer "Abbrechen", "Vergiss es"). Nutzt die `filler-agent`-Konfiguration mit per-call Guardrails. Hartes Timeout + deterministischer Fallback ("Okay." / "Alles klar.") verhindert haengende Sprachsatelliten. HA-Integration leitet den User-Text immer an den Container weiter (kein lokales Keyword-Shortcut).

- [ ] **HA-Service fuer Automationen (`ai_task`-Aequivalent)**: Service oder klarer Contract fuer Automatisierungen (z. B. strukturierter Output / `generate_data`-Pattern), der den Container ohne manuelles HTTP-Basteln nutzbar macht.

- [ ] **Kalender: lesen, schreiben, proaktive Reminder, Zuordnungen**: Kalenderereignisse lesen, schreiben, gestufte/proaktive Erinnerungen, optional Nutzer-zu-Kalender-Mappings (wie im Smart-Assist-Prompt-Pattern); Dashboard/Traces wo passend.

security agent:
sentinel mode bleibt vorerst deferred. wenn ein/oder mehrere explizit dem security agent zugewiesene sensoren (z. b. bewegungsmelder) automatisch einen security-agent-run triggern sollen, braucht das einen separaten trigger-contract und wahrscheinlich eine eigene ui page.

Debug-Logging aktivieren

Aktiviert ausfuehrliches Logging zur Fehlersuche.
