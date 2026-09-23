import numpy as np

from repair.repair import route


def test_route():
    clean = np.full((100, 100, 3), 255, np.uint8)
    clean[:10] = 0
    damaged = np.full_like(clean, 155)
    damaged[:10] = 0
    faded = np.full_like(clean, 255)
    faded[:10] = 220
    assert route(clean)[0]
    assert not route(damaged)[0]  # strong ink alone cannot mask yellowed paper
    assert not route(faded)[0]    # white paper alone cannot mask faded ink


if __name__ == "__main__":
    test_route()
    print("gate checks passed")
