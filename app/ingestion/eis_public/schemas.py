from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass
class EISCandidate:
    external_id: str
    title: str | None = None
    customer_name: str | None = None
    region: str | None = None
    procurement_type: str | None = None
    nmck: Decimal | None = None
    published_at: datetime | None = None
    submission_deadline: datetime | None = None
    url_to_card: str | None = None
    url_to_viewxml: str | None = None
