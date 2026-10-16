"""情境載入與 SimWeb 組裝：世界 + 虛擬時鐘 + 伺服器 + 標準答案。"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import yaml

from simweb.clock import Clock, VirtualClock
from simweb.server import SimWebServer
from simweb.spec import ScenarioSpec
from simweb.truth import Truth, derive_truth
from simweb.world import World

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def scenario_dir() -> Path:
    return Path(os.environ.get("SIMWEB_SCENARIO_DIR", PROJECT_ROOT / "evals" / "scenarios"))


def load_scenario(scenario_id: str) -> ScenarioSpec:
    path = scenario_dir() / f"{scenario_id}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"找不到情境 {scenario_id}：{path}")
    with path.open(encoding="utf-8") as f:
        return ScenarioSpec.model_validate(yaml.safe_load(f))


def list_scenarios() -> list[ScenarioSpec]:
    return [load_scenario(p.stem) for p in sorted(scenario_dir().glob("*.yaml"))]


class SimWeb:
    def __init__(self, spec: ScenarioSpec, clock: Clock | None = None) -> None:
        self.spec = spec
        self.world = World(spec)
        self.clock = clock or VirtualClock()
        self.server = SimWebServer(self.world, self.clock)
        self.truth: Truth = derive_truth(spec)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.server.handle)

    def client(self, **kwargs) -> httpx.Client:
        return httpx.Client(transport=self.transport(), timeout=10.0, **kwargs)


def build(scenario_id: str, clock: Clock | None = None) -> SimWeb:
    return SimWeb(load_scenario(scenario_id), clock)
