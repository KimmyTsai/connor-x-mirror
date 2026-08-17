"""
detect.py 的 cluster() 去重分群 —— CLAUDE.md 設計決策 #4：
「不分群的話清冊數字會膨脹三四倍」，這裡驗證半徑內合併、半徑外不合併，
以及 support（觀測支持數）與信心值的聚合規則。
"""

from detect import Observation, cluster, destination, CLUSTER_RADIUS_M

ORIGIN = (22.9997, 120.2270)

# 框中心對齊 heading，bearing == heading，方便控制估計座標
CENTERED_BOX = [100, 500, 900, 500]


def make_obs(pano, heading=0.0, confidence=0.8, intersection_id="INT_001"):
    return Observation(
        pano_lat=pano[0], pano_lng=pano[1],
        heading=heading, fov=50,
        box_2d=CENTERED_BOX, shape="round",
        evidence="test", confidence=confidence,
        intersection_id=intersection_id,
    )


def test_two_observations_same_pano_merge_into_one():
    obs = [make_obs(ORIGIN, confidence=0.6), make_obs(ORIGIN, confidence=0.9)]
    cands = cluster(obs)
    assert len(cands) == 1
    assert cands[0].support == 2
    assert cands[0].confidence == 0.9  # 取群內最高信心


def test_nearby_panos_within_radius_merge():
    # 第二個全景點在正東方 5m 處，遠小於 12m 分群半徑
    pano2 = destination(*ORIGIN, 90.0, 5.0)
    obs = [make_obs(ORIGIN), make_obs(pano2)]
    cands = cluster(obs)
    assert len(cands) == 1
    assert cands[0].support == 2


def test_distant_panos_beyond_radius_stay_separate():
    # 第二個全景點在正東方 30m 處，超過 12m 分群半徑
    pano2 = destination(*ORIGIN, 90.0, 30.0)
    obs = [make_obs(ORIGIN), make_obs(pano2)]
    cands = cluster(obs)
    assert len(cands) == 2
    assert all(c.support == 1 for c in cands)


def test_cluster_radius_boundary_is_the_configured_constant():
    # 確保測試假設跟程式碼常數同步，而不是各自硬編碼 12m
    assert CLUSTER_RADIUS_M == 12.0


def test_single_observation_keeps_its_own_confidence():
    obs = [make_obs(ORIGIN, confidence=0.55)]
    cands = cluster(obs)
    assert len(cands) == 1
    assert cands[0].support == 1
    assert cands[0].confidence == 0.55


def test_cluster_center_is_mean_of_group_points():
    pano2 = destination(*ORIGIN, 90.0, 5.0)
    obs = [make_obs(ORIGIN), make_obs(pano2)]
    cands = cluster(obs)

    p1 = obs[0].estimated_point
    p2 = obs[1].estimated_point
    expected_lat = (p1[0] + p2[0]) / 2
    expected_lng = (p1[1] + p2[1]) / 2

    assert cands[0].lat == expected_lat
    assert cands[0].lng == expected_lng
