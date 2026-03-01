from pydantic import BaseModel


class RiskRecomputeRequest(BaseModel):
    use_latest_extracted: bool = True


class RiskRecomputeResponse(BaseModel):
    risk_v1: dict
    risk_flags: list
