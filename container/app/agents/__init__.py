from app.agents.actionable import (
    AutomationAgent,
    ClimateAgent,
    CoverAgent,
    DomainAgent,
    LightAgent,
    MediaAgent,
    MusicAgent,
    SceneAgent,
    SecurityAgent,
    VacuumAgent,
)
from app.agents.base import BaseAgent
from app.agents.calendar import CalendarAgent
from app.agents.custom_loader import CustomAgentLoader, DynamicAgent
from app.agents.decorator import agent as agent
from app.agents.decorator import install_all_agents
from app.agents.general import GeneralAgent
from app.agents.lists import ListsAgent
from app.agents.orchestrator import OrchestratorAgent
from app.agents.rewrite import RewriteAgent
from app.agents.timer import TimerAgent

__all__ = [
    "AutomationAgent",
    "BaseAgent",
    "CalendarAgent",
    "ClimateAgent",
    "CoverAgent",
    "CustomAgentLoader",
    "DomainAgent",
    "DynamicAgent",
    "GeneralAgent",
    "LightAgent",
    "ListsAgent",
    "MediaAgent",
    "MusicAgent",
    "OrchestratorAgent",
    "RewriteAgent",
    "SceneAgent",
    "SecurityAgent",
    "TimerAgent",
    "VacuumAgent",
    "agent",
    "install_all_agents",
]
