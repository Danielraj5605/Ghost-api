"""How much to trust each endpoint, from evidence only.

Points:
    verified by live tests          +2
    failed after repairs            -3
    callable directly (replayable)  +1
    pagination confirmed            +1
    samples: 2-4 -> +1, 5+ -> +2

High is 4 or more, Medium 2-3, Low below 2.
"""

from __future__ import annotations

from typing import Literal

from ghost_api.analysis.models import Endpoint

Level = Literal["High", "Medium", "Low"]


def confidence_points(ep: Endpoint) -> int:
    points = 0
    if ep.status == "verified":
        points += 2
    elif ep.status == "failed":
        points -= 3
    if ep.replayable:
        points += 1
    if ep.pagination and ep.pagination.get("confirmed"):
        points += 1
    if ep.sample_count >= 5:
        points += 2
    elif ep.sample_count >= 2:
        points += 1
    return points


def confidence(ep: Endpoint) -> Level:
    points = confidence_points(ep)
    if points >= 4:
        return "High"
    if points >= 2:
        return "Medium"
    return "Low"
