"""Decide whether a STAC acquisition notification should trigger a pipeline pass.

The SNS topic should already be filtered to relevant scenes, but a topic subscribed to a
whole collection delivers every tile worldwide. This is a second, cheap guard: skip an
event only when it is *positively* irrelevant — a collection the pipeline does not ingest
(anything but Sentinel-2) or a footprint outside the monitored area. A message whose shape
we cannot read is processed rather than dropped, because a redundant pass is harmless
(observations are idempotent) while a missed scene delays an alert.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

BBox = tuple[float, float, float, float]  # west, south, east, north (EPSG:4326)

# Approximate Gilgit-Baltistan extent, padded slightly. Override with STAC_EVENT_BBOX.
DEFAULT_BBOX: BBox = (72.0, 34.0, 78.0, 37.2)
BBOX_ENV = "STAC_EVENT_BBOX"
_SENTINEL_2 = re.compile(r"sentinel-?s?2")  # sentinel-2-l2a, sentinel-s2-l2a, sentinel2


def configured_bbox() -> BBox:
    raw = os.environ.get(BBOX_ENV)
    if not raw:
        return DEFAULT_BBOX
    try:
        west, south, east, north = (float(part) for part in raw.split(","))
    except ValueError as exc:
        raise ValueError(f"{BBOX_ENV} must be 'west,south,east,north' in degrees") from exc
    if not (west < east and south < north):
        raise ValueError(f"{BBOX_ENV} must satisfy west < east and south < north")
    return (west, south, east, north)


def _coordinates(node: Any):
    """Yield every (x, y) position from a GeoJSON coordinates array."""
    if (
        isinstance(node, (list, tuple))
        and len(node) >= 2
        and all(isinstance(v, (int, float)) for v in node[:2])
    ):
        yield float(node[0]), float(node[1])
    elif isinstance(node, (list, tuple)):
        for child in node:
            yield from _coordinates(child)


def _item_bbox(item: dict) -> BBox | None:
    raw = item.get("bbox")
    if isinstance(raw, list) and len(raw) in (4, 6) and all(isinstance(v, (int, float)) for v in raw):
        if len(raw) == 4:
            return (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
        return (float(raw[0]), float(raw[1]), float(raw[3]), float(raw[4]))  # 3D: w,s,minz,e,n,maxz

    geometry = item.get("geometry")
    if isinstance(geometry, dict):
        points = list(_coordinates(geometry.get("coordinates")))
        if points:
            xs, ys = zip(*points, strict=True)
            return (min(xs), min(ys), max(xs), max(ys))
    return None


def _intersects(a: BBox, b: BBox) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _collection(item: dict) -> str | None:
    value = item.get("collection")
    if isinstance(value, str) and value:
        return value.lower()
    properties = item.get("properties")
    if isinstance(properties, dict):
        platform = properties.get("platform")
        if isinstance(platform, str) and platform:
            return platform.lower()
    return None


def should_process(message: object, area: BBox | None = None) -> tuple[bool, str]:
    """Return (process?, reason) for the Message body of an SNS notification."""
    area = area or configured_bbox()

    item = message
    if isinstance(message, str):
        try:
            item = json.loads(message)
        except ValueError:
            return True, "message is not JSON; processing"
    if not isinstance(item, dict):
        return True, "message is not a STAC item; processing"

    collection = _collection(item)
    if collection is not None and not _SENTINEL_2.search(collection):
        return False, f"unsupported collection {collection!r} (only Sentinel-2 is ingested)"

    footprint = _item_bbox(item)
    if footprint is not None and not _intersects(footprint, area):
        return False, "scene footprint is outside the monitored area"
    return True, "relevant"
