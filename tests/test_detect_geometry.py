"""
detect.py 的定位數學：destination()（單射線投影）與 haversine()（距離）。

這兩個函式是 CLAUDE.md 設計決策 #8（單射線投影）的基礎，
也是 cluster() 分群半徑判斷的依據，錯了會讓整批去重跟著錯。
"""

import math

import pytest

from detect import destination, haversine, bearing_between, EARTH_R


def test_haversine_same_point_is_zero():
    p = (22.9997, 120.2270)
    assert haversine(p, p) == 0.0


def test_haversine_symmetric():
    a = (22.9997, 120.2270)
    b = (23.0010, 120.2290)
    assert haversine(a, b) == haversine(b, a)


def test_haversine_one_degree_latitude():
    # 沿子午線移動 1 度緯度的大圓距離有解析解：R * pi / 180
    a = (0.0, 0.0)
    b = (1.0, 0.0)
    expected = EARTH_R * math.pi / 180
    assert haversine(a, b) == pytest.approx(expected, rel=1e-6)


def test_destination_round_trip_matches_distance():
    origin = (22.9997, 120.2270)
    for bearing in (0, 45, 90, 135, 180, 225, 270, 315):
        dest = destination(*origin, bearing, 8.0)
        assert haversine(origin, dest) == pytest.approx(8.0, rel=1e-6)


def test_destination_north_increases_latitude_only():
    origin = (22.9997, 120.2270)
    lat, lng = destination(*origin, 0.0, 100.0)
    assert lat > origin[0]
    assert lng == pytest.approx(origin[1], abs=1e-9)


def test_destination_east_increases_longitude_only():
    origin = (22.9997, 120.2270)
    lat, lng = destination(*origin, 90.0, 100.0)
    assert lng > origin[1]
    assert lat == pytest.approx(origin[0], abs=1e-6)


def test_destination_zero_distance_is_noop():
    origin = (22.9997, 120.2270)
    dest = destination(*origin, 137.0, 0.0)
    assert dest[0] == pytest.approx(origin[0], abs=1e-12)
    assert dest[1] == pytest.approx(origin[1], abs=1e-12)


# ── bearing_between()：Stage 2 變焦特寫用來瞄準鏡子的方位角計算 ──

def test_bearing_between_is_inverse_of_destination():
    origin = (22.9997, 120.2270)
    for bearing in (0, 45, 90, 135, 180, 225, 270, 315):
        dest = destination(*origin, bearing, 8.0)
        got = bearing_between(origin, dest)
        # 0 度／360 度是同一個方位，浮點數在邊界上會落在任一側，取模比較
        diff = min(abs(got - bearing), 360 - abs(got - bearing))
        assert diff == pytest.approx(0.0, abs=1e-4)


def test_bearing_between_north_is_zero():
    a = (22.9997, 120.2270)
    b = (22.9998, 120.2270)  # 正北方
    assert bearing_between(a, b) == pytest.approx(0.0, abs=1e-6)


def test_bearing_between_east_is_90():
    a = (22.9997, 120.2270)
    b = (22.9997, 120.2280)  # 正東方
    assert bearing_between(a, b) == pytest.approx(90.0, abs=1e-3)


def test_bearing_between_same_point_is_zero():
    p = (22.9997, 120.2270)
    assert bearing_between(p, p) == 0.0
