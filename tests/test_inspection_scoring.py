"""
inspection.py 的 condition_score() 加權公式，對照 CLAUDE.md 判讀 rubric：
角度偏移 0.30（最危險）／髒污 0.25／破損 0.20／遮蔽 0.15／鏽蝕 0.10。

這個測試同時鎖住兩件事：算式本身，以及 WEIGHTS 沒有偷偷偏離文件上的權重表。
"""

from inspection import condition_score, WEIGHTS


def make_verdict(**overrides):
    v = {
        "dirt_score": 0, "angle_score": 0, "damage_score": 0,
        "occlusion_score": 0, "corrosion_score": 0,
    }
    v.update(overrides)
    return v


def test_weights_match_documented_rubric():
    assert WEIGHTS == {
        "dirt_score": 0.25,
        "angle_score": 0.30,
        "damage_score": 0.20,
        "occlusion_score": 0.15,
        "corrosion_score": 0.10,
    }


def test_all_zero_scores_zero():
    assert condition_score(make_verdict()) == 0.0


def test_all_max_scores_hundred():
    v = make_verdict(dirt_score=3, angle_score=3, damage_score=3,
                      occlusion_score=3, corrosion_score=3)
    assert condition_score(v) == 100.0


def test_angle_offset_weighted_highest():
    # 角度偏移權重最高，同樣是單一維度打滿分，角度分數應該 > 其他任一維度
    angle_only = condition_score(make_verdict(angle_score=3))
    dirt_only = condition_score(make_verdict(dirt_score=3))
    damage_only = condition_score(make_verdict(damage_score=3))
    occlusion_only = condition_score(make_verdict(occlusion_score=3))
    corrosion_only = condition_score(make_verdict(corrosion_score=3))

    assert angle_only > dirt_only > damage_only > occlusion_only > corrosion_only


def test_angle_only_matches_expected_value():
    # raw = 3 * 0.30 = 0.9 → /3*100 = 30.0
    assert condition_score(make_verdict(angle_score=3)) == 30.0


def test_dirt_only_matches_expected_value():
    # raw = 3 * 0.25 = 0.75 → /3*100 = 25.0
    assert condition_score(make_verdict(dirt_score=3)) == 25.0
