"""Safe-wins preprocessing: crop/deskew/flatten round-trip. No network."""

import cv2
import numpy as np

from app.preprocess import Mapper, prepare


def _page_with_bars():
    """White page, 40px dark scan border, and five dark 'text' bars."""
    img = np.full((1400, 1000, 3), 255, dtype=np.uint8)
    img[:40, :] = 0
    img[-40:, :] = 0
    img[:, :40] = 0
    img[:, -40:] = 0
    for i in range(5):
        y = 200 + i * 100
        img[y:y + 40, 200:800] = 0
    return img


def _top_dark_box(gray):
    """bbox of the topmost dark component in a prepared page."""
    n, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        (gray < 128).astype(np.uint8), connectivity=8)
    boxes = [tuple(int(v) for v in stats[i]) for i in range(1, n)]
    x, y, w, h, _area = min(boxes, key=lambda s: s[1])
    return x, y, w, h


def test_prepare_round_trips_boxes_to_the_original_page():
    original = _page_with_bars()
    prepared, mapper = prepare(original)

    assert prepared.ndim == 3 and prepared.shape[2] == 3
    assert not mapper.identity, "border crop should have changed the frame"

    # The topmost dark bar was at (200, 200) in the original page.
    x, y, w, h = _top_dark_box(cv2.cvtColor(prepared, cv2.COLOR_BGR2GRAY))
    mapped = mapper.to_page([{"body": "x", "bbox": [x, y, w, h], "confidence": 1.0}])[0]

    assert abs(mapped["bbox"][0] - 200) <= 6, mapped["bbox"]
    assert abs(mapped["bbox"][1] - 200) <= 6, mapped["bbox"]
    assert abs(mapped["bbox"][2] - 600) <= 8, mapped["bbox"]


def test_blank_page_is_left_alone():
    blank = np.full((1600, 1200, 3), 255, dtype=np.uint8)
    prepared, mapper = prepare(blank)
    assert mapper.identity
    assert prepared.shape == blank.shape


def test_identity_mapper_passes_lines_through():
    lines = [{"body": "அ", "bbox": [1, 2, 3, 4], "confidence": 0.9}]
    assert Mapper((0, 0), 0.0, 1.0, (10, 10)).to_page(lines) == lines


if __name__ == "__main__":
    test_prepare_round_trips_boxes_to_the_original_page()
    test_blank_page_is_left_alone()
    test_identity_mapper_passes_lines_through()
    print("preprocess checks passed")
