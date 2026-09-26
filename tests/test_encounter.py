"""双人轨迹相遇分析的单元测试。

可直接运行：python3 tests/test_encounter.py
或使用 pytest：python3 -m pytest tests/
"""
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.encounter import (
    MAX_GAP_S,
    MEET_RADIUS_M,
    MIN_MEET_S,
    find_encounters,
)
from app.geo import EARTH_RADIUS_M, TrackPoint

T0 = datetime(2026, 5, 1, 8, 0, 0, tzinfo=timezone.utc)

# 测试轨迹围绕 (31.0, 121.0)：1° 纬度 ≈ 111195 m
HOME = (31.0, 121.0)
DEG_PER_M = 1.0 / math.radians(EARTH_RADIUS_M)  # 每米对应的纬度数（与实现同一投影常量）


def make_seg(points):
    """points: [(lat, lon, 秒偏移), ...]"""
    return [
        TrackPoint(lat=la, lon=lo, ele=None, time=T0 + timedelta(seconds=s))
        for la, lo, s in points
    ]


def north(meters, base=HOME):
    """base 正北方向约 meters 米的位置。"""
    return (base[0] + meters * DEG_PER_M, base[1])


def _dense_times(t0, t1, step):
    """[t0, t1] 按 step 秒加密的时间点（末点恰为 t1），避免采样间隔触发断档。"""
    ts = []
    t = t0
    while t < t1:
        ts.append(t)
        t += step
    ts.append(t1)
    return ts


def still(latlon, t0, t1, step=60):
    """在 latlon 原地不动的分段（按 step 秒加密采样）。"""
    return make_seg([(latlon[0], latlon[1], t) for t in _dense_times(t0, t1, step)])


def move(p0, p1, t0, t1, step=100):
    """从 p0 匀速移动到 p1 的分段（按 step 秒加密采样，几何与单条线段一致）。"""
    pts = []
    for t in _dense_times(t0, t1, step):
        f = (t - t0) / (t1 - t0)
        pts.append((p0[0] + (p1[0] - p0[0]) * f, p0[1] + (p1[1] - p0[1]) * f, t))
    return make_seg(pts)


def test_together_one_encounter():
    """两人全程相距 20 米：一次相遇，起止与重叠区间一致，最近距离 20 米。"""
    a = [still(HOME, 0, 600)]
    b = [still(north(20), 0, 600)]
    res = find_encounters(a, b)

    assert len(res.encounters) == 1
    e = res.encounters[0]
    assert e.start_time == T0
    assert e.end_time == T0 + timedelta(seconds=600)
    assert abs(e.duration_s - 600.0) < 1e-6
    assert abs(e.min_distance_m - 20.0) < 1e-6
    # 最近时刻两人中点：HOME 正北约 10 米
    assert abs(e.lat - north(10)[0]) < 1e-6
    assert abs(e.lon - HOME[1]) < 1e-9


def test_passing_times_and_min_distance():
    """B 匀速穿过 A 所在点：相遇起止与解析解一致，最近距离约 0。"""
    a = [still(HOME, 0, 2000)]
    b = [move((31.0, 120.999), (31.0, 121.001), 0, 2000)]  # t=1000 经过 HOME
    res = find_encounters(a, b)

    assert len(res.encounters) == 1
    e = res.encounters[0]
    kx = math.radians(EARTH_RADIUS_M) * math.cos(math.radians(31.0))  # 每度经度的米数
    speed = 0.002 * kx / 2000.0            # B 的移速（m/s）
    t_half = MEET_RADIUS_M / speed         # 进入/离开 50 米圈的半宽（秒）
    t0 = (e.start_time - T0).total_seconds()
    t1 = (e.end_time - T0).total_seconds()
    assert abs(t0 - (1000.0 - t_half)) < 2.0
    assert abs(t1 - (1000.0 + t_half)) < 2.0
    assert abs(e.duration_s - 2.0 * t_half) < 4.0
    assert e.min_distance_m < 0.5          # 恰好穿过 A 点
    t_star = (e.closest_time - T0).total_seconds()
    assert abs(t_star - 1000.0) < 2.0


def test_short_encounter_not_listed():
    """距离 ≤ 50 米但持续不足 60 秒：不列出。"""
    a = [still(HOME, 0, 1000)]
    b = [still(north(40), 0, 50)]          # 仅 50 秒在圈内
    res = find_encounters(a, b)
    assert res.encounters == []


def test_exactly_60s_listed():
    """持续恰好 60 秒：满足最短时长，列出。"""
    a = [still(HOME, 0, 1000)]
    b = [still(north(40), 0, MIN_MEET_S)]
    res = find_encounters(a, b)
    assert len(res.encounters) == 1
    assert abs(res.encounters[0].duration_s - 60.0) < 1e-6


def test_touching_intervals_merged():
    """相接的候选区间合并：两段各 50 秒相接成 100 秒，达到 60 秒门槛。"""
    a = [still(HOME, 0, 200)]
    b = [make_seg([
        (north(40)[0], north(40)[1], 0),
        (north(40)[0], north(40)[1], 50),
        (north(40)[0], north(40)[1], 100),
    ])]
    res = find_encounters(a, b)
    # 不合并时两段各 50 秒都会被过滤；合并后 [0,100] 列出一次
    assert len(res.encounters) == 1
    e = res.encounters[0]
    assert abs(e.duration_s - 100.0) < 1e-6
    assert abs(e.min_distance_m - 40.0) < 1e-6


def test_separated_encounters_listed_in_order():
    """互不相接的两段相遇分别列出，按时间先后排列。"""
    a = [still(HOME, 0, 400)]
    b = [
        still(north(40), 0, 70),      # 相遇 1：[0, 70]
        still(north(200), 150, 220),  # 相距 200 米，不算相遇
        still(north(40), 300, 370),   # 相遇 2：[300, 370]
    ]
    res = find_encounters(a, b)
    assert len(res.encounters) == 2
    e1, e2 = res.encounters
    assert e1.start_time < e2.start_time
    assert abs(e1.duration_s - 70.0) < 1e-6
    assert abs(e2.duration_s - 70.0) < 1e-6
    assert abs((e2.start_time - T0).total_seconds() - 300.0) < 1e-6


def test_no_time_overlap():
    """时间完全不重叠：即使位置相同也无相遇。"""
    a = [still(HOME, 0, 100)]
    b = [still(HOME, 200, 300)]
    res = find_encounters(a, b)
    assert res.encounters == []


def test_beyond_radius_empty():
    """全程相距 100 米（> 50 米）：无相遇。"""
    a = [still(HOME, 0, 300)]
    b = [still(north(100), 0, 300)]
    res = find_encounters(a, b)
    assert res.encounters == []


def test_radius_boundary():
    """50 米边界：约 49.9 米计入，约 50.1 米不计入。"""
    a = [still(HOME, 0, 100)]
    inside = find_encounters(a, [still(north(49.9), 0, 100)])
    outside = find_encounters(a, [still(north(50.1), 0, 100)])
    assert len(inside.encounters) == 1
    assert abs(inside.encounters[0].min_distance_m - 49.9) < 1e-6
    assert outside.encounters == []


def test_gap_not_bridged():
    """断档（> 10 分钟）不插值：断档两侧各 60 秒相遇分别列出，不虚构整段相遇。"""
    a = [make_seg([
        (HOME[0], HOME[1], 0),
        (HOME[0], HOME[1], 60),
        (HOME[0], HOME[1], 1260),   # 与上一点相隔 1200 s > 10 分钟 → 断档
        (HOME[0], HOME[1], 1320),
    ])]
    b = [make_seg([(HOME[0], HOME[1], s) for s in (0, 300, 600, 900, 1200, 1320)])]
    res = find_encounters(a, b)
    # 若跨断档插值会得到一次 [0,1320] 的长相遇；正确结果是断档两侧各一次 60 秒
    assert len(res.encounters) == 2
    e1, e2 = res.encounters
    assert abs(e1.duration_s - 60.0) < 1e-6
    assert abs(e2.duration_s - 60.0) < 1e-6
    assert abs((e1.start_time - T0).total_seconds() - 0.0) < 1e-6
    assert abs((e2.end_time - T0).total_seconds() - 1320.0) < 1e-6


def test_gap_exactly_10min_counts():
    """相邻采样间隔恰好 10 分钟：不算断档，正常构造线段。"""
    a = [make_seg([(HOME[0], HOME[1], 0), (HOME[0], HOME[1], MAX_GAP_S)])]
    b = [still(north(30), 0, MAX_GAP_S)]
    res = find_encounters(a, b)
    assert len(res.encounters) == 1
    assert abs(res.encounters[0].duration_s - MAX_GAP_S) < 1e-6


def test_trkseg_boundary_not_bridged():
    """不跨 trkseg 虚构相遇：两个分段各 50 秒在圈内，均不足 60 秒 → 空结果。"""
    a = [
        make_seg([(HOME[0], HOME[1], 0), (HOME[0], HOME[1], 50)]),
        make_seg([(HOME[0], HOME[1], 250), (HOME[0], HOME[1], 300)]),
    ]
    b = [make_seg([(HOME[0], HOME[1], s) for s in (0, 150, 300)])]
    res = find_encounters(a, b)
    # 若把两个 trkseg 连接起来，A 在 [50,250] 仍在 HOME，会凑出 300 秒相遇
    assert res.encounters == []


def test_zero_time_diff_rejected():
    """当前轨迹时间零时差：报明确错误并指明“当前轨迹”。"""
    a = [make_seg([(HOME[0], HOME[1], 0), (HOME[0], HOME[1], 0)])]
    b = [still(HOME, 0, 100)]
    try:
        find_encounters(a, b)
    except ValueError as exc:
        assert "未递增" in str(exc) and "当前轨迹" in str(exc)
    else:
        raise AssertionError("零时差应报明确错误")


def test_reversed_time_rejected_other_track():
    """对比轨迹时间倒序：报明确错误并指明“对比轨迹”与分段序号。"""
    a = [still(HOME, 0, 100)]
    b = [make_seg([
        (HOME[0], HOME[1], 0),
        (HOME[0], HOME[1], 60),
    ]), make_seg([
        (HOME[0], HOME[1], 120),
        (HOME[0], HOME[1], 110),
    ])]
    try:
        find_encounters(a, b)
    except ValueError as exc:
        assert "未递增" in str(exc) and "对比轨迹" in str(exc) and "第 2 个 trkseg" in str(exc)
    else:
        raise AssertionError("倒序时间应报明确错误")


def test_min_distance_and_closest_position():
    """B 从 A 正北 30 米处东西向经过：最近距离约 30 米，相遇点为中点。"""
    near = north(30)
    a = [still(HOME, 0, 2000)]
    b = [move((near[0], 120.999), (near[0], 121.001), 0, 2000)]
    res = find_encounters(a, b)

    assert len(res.encounters) == 1
    e = res.encounters[0]
    assert abs(e.min_distance_m - 30.0) < 0.5
    # 最近时刻两人位置的中点：HOME 正北约 15 米
    assert abs(e.lat - north(15)[0]) < 1e-5
    assert abs(e.lon - HOME[1]) < 1e-5
    t_star = (e.closest_time - T0).total_seconds()
    assert abs(t_star - 1000.0) < 2.0


def test_single_point_segments():
    """单点 trkseg 构造不出线段：空结果且不报错。"""
    a = [make_seg([(HOME[0], HOME[1], 0)])]
    b = [still(HOME, 0, 100)]
    res = find_encounters(a, b)
    assert res.encounters == []


def _run_all():
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
