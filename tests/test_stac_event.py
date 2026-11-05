import json

import pytest

from pipeline.stac_event import DEFAULT_BBOX, configured_bbox, should_process

# Inside / outside the default Gilgit-Baltistan extent.
GB_BBOX = [74.5, 35.8, 74.9, 36.1]
LONDON_BBOX = [-0.6, 51.2, 0.4, 51.7]


def _item(collection="sentinel-2-l2a", bbox=None, **extra):
    item = {"type": "Feature", "collection": collection, "bbox": bbox or GB_BBOX, **extra}
    return json.dumps(item)


def test_sentinel2_scene_over_monitored_area_is_processed():
    assert should_process(_item()) == (True, "relevant")


@pytest.mark.parametrize("collection", ["sentinel-2-l2a", "sentinel-s2-l2a", "SENTINEL-2-L1C", "sentinel2"])
def test_sentinel2_collection_name_variants_are_accepted(collection):
    assert should_process(_item(collection))[0] is True


@pytest.mark.parametrize("collection", ["sentinel-1-grd", "sentinel-s1-l1c", "landsat-c2-l2"])
def test_other_collections_are_ignored(collection):
    process, reason = should_process(_item(collection))
    assert process is False
    assert "only Sentinel-2" in reason


def test_scene_outside_the_monitored_area_is_ignored():
    process, reason = should_process(_item(bbox=LONDON_BBOX))
    assert process is False
    assert "outside the monitored area" in reason


def test_footprint_is_derived_from_geometry_when_bbox_is_absent():
    ring = [[-0.6, 51.2], [0.4, 51.2], [0.4, 51.7], [-0.6, 51.7], [-0.6, 51.2]]
    message = json.dumps(
        {"collection": "sentinel-2-l2a", "geometry": {"type": "Polygon", "coordinates": [ring]}}
    )
    assert should_process(message)[0] is False


def test_platform_property_is_used_when_collection_is_missing():
    message = json.dumps({"bbox": GB_BBOX, "properties": {"platform": "sentinel-1a"}})
    assert should_process(message)[0] is False


def test_unreadable_messages_are_processed_not_dropped():
    assert should_process("not json")[0] is True
    assert should_process("[1, 2]")[0] is True
    assert should_process(None)[0] is True
    assert should_process(json.dumps({"collection": "sentinel-2-l2a"}))[0] is True  # no footprint


def test_bbox_can_be_overridden_from_the_environment(monkeypatch):
    monkeypatch.setenv("STAC_EVENT_BBOX", "-1,51,1,52")
    assert configured_bbox() == (-1.0, 51.0, 1.0, 52.0)
    assert should_process(_item(bbox=LONDON_BBOX))[0] is True
    assert should_process(_item())[0] is False


def test_default_bbox_is_used_when_unset(monkeypatch):
    monkeypatch.delenv("STAC_EVENT_BBOX", raising=False)
    assert configured_bbox() == DEFAULT_BBOX


@pytest.mark.parametrize("value", ["1,2,3", "a,b,c,d", "5,5,1,1"])
def test_invalid_bbox_override_is_rejected(monkeypatch, value):
    monkeypatch.setenv("STAC_EVENT_BBOX", value)
    with pytest.raises(ValueError):
        configured_bbox()
