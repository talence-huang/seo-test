"""SimWeb：可重現的模擬 SEO 世界，作為研究 Agent 的實驗環境（不依賴 seo_intel）。"""

from simweb.clock import RealClock, VirtualClock
from simweb.server import ARCHIVE_HOST, RANK_HOST, AccessLog
from simweb.sim import SimWeb, build, list_scenarios, load_scenario
from simweb.spec import ScenarioSpec
from simweb.truth import Truth

__all__ = [
    "ARCHIVE_HOST",
    "RANK_HOST",
    "AccessLog",
    "RealClock",
    "ScenarioSpec",
    "SimWeb",
    "Truth",
    "VirtualClock",
    "build",
    "list_scenarios",
    "load_scenario",
]
