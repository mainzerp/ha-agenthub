# TODO

## Pending Features

- [ ] **HA Entities auf Exposed Entities beschraenken**: Entity-Zugriff auf die in Home Assistant als "exposed" markierten Entities einschraenken, damit nur freigegebene Geraete gesteuert/abgefragt werden koennen.

- [ ] **Nutzer- und Agent-Memory**: Persistente Profile, Memory-Tool (speichern/abrufen/aktualisieren), Limits/Eviction, optional UI im Dashboard; Mehrschichtige Nutzerzuordnung wo sinnvoll.

- [x] **Cancel-Intent / Dismiss**: Orchestrator-LLM routet zu virtuellem Agent **cancel-interaction**; der Container liefert einen kurzen ACK **ohne** Domain-Dispatch (Manifest **0.5.5**). HA-Integration leitet den User-Text immer an den Container weiter (kein lokales Keyword-Shortcut).

- [ ] **HA-Service fuer Automationen (`ai_task`-Aequivalent)**: Service oder klarer Contract fuer Automatisierungen (z. B. strukturierter Output / `generate_data`-Pattern), der den Container ohne manuelles HTTP-Basteln nutzbar macht.

- [ ] **Kalender: lesen, proaktive Reminder, Zuordnungen**: Kalenderereignisse lesen, gestufte/proaktive Erinnerungen, optional Nutzer-zu-Kalender-Mappings (wie im Smart-Assist-Prompt-Pattern); Dashboard/Traces wo passend.

climate-agent:
wetterdaten aus HA

security agent:
sentinel mode, wenn ein/oder mehrere explizit dem security agent zugewisener sensor z.b. ein bewegungsmelder getriggert wird, wird automatisch ein security agent run getriggert, der eine definierte analyse durchführt (z.b. kameras prüfen usw). erfordert wahrscheinlich eine separate ui page.

timer-agent:
wecker erweitern
wecken mit infos anreichern, wetter, news usw. (bereistellung der daten durch div. agents) dann rewite-agent um einen gelungenen text zu bilden.

beisp.

Guten Morgen! Heite ist Sonntag der 26. April 2026
draußen ist, Strahlender Sonnenschein, aktuell 19 °C – heute bleibt es trocken und schön. Die Woche startet ähnlich mild, perfektes Frühlingswetter!
Kurze News
USA: Beim White House Correspondents' Dinner in Washington gab es einen Zwischenfall – ein Verdächtiger eröffnete das Feuer und verletzte einen Secret-Service-Agenten, bevor er gestoppt wurde.
Jahrestag: Heute vor 40 Jahren ereignete sich die Katastrophe von Tschernobyl – ein Datum, das Deutschlands Energiepolitik bis heute prägt.
Schönen Sonntag!