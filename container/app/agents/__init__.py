"""Agent implementations for agent-assist."""

from app.agents.automation import AutomationAgent
from app.agents.base import BaseAgent
from app.agents.calendar import CalendarAgent
from app.agents.climate import ClimateAgent
from app.agents.custom_loader import CustomAgentLoader, DynamicAgent
from app.agents.general import GeneralAgent
from app.agents.light import LightAgent
from app.agents.media import MediaAgent
from app.agents.music import MusicAgent
from app.agents.orchestrator import OrchestratorAgent
from app.agents.rewrite import RewriteAgent
from app.agents.scene import SceneAgent
from app.agents.security import SecurityAgent
from app.agents.timer import TimerAgent

__all__ = [
    "AutomationAgent",
    "BaseAgent",
    "CalendarAgent",
    "ClimateAgent",
    "CustomAgentLoader",
    "DynamicAgent",
    "GeneralAgent",
    "LightAgent",
    "MediaAgent",
    "MusicAgent",
    "OrchestratorAgent",
    "RewriteAgent",
    "SceneAgent",
    "SecurityAgent",
    "TimerAgent",
]
