from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class DecisionRecomputeRequest(BaseModel):
    force: bool = False


class DecisionEngineReadResponse(BaseModel):
    recommendation: Literal["go", "no_go", "unsure"]
    decision_engine_v1: dict | None


class DecisionRecomputeResponse(BaseModel):
    recommendation: Literal["go", "no_go", "unsure"]
    decision_engine_v1: dict
