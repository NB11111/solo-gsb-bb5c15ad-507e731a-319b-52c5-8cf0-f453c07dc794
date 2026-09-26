"""检查点到访分析的单元测试。

可直接运行：python3 tests/test_checkpoint.py
或使用 pytest：python3 -m pytest tests/
"""
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.checkpoint import (
    DEFAULT_RADIUS_M,
    MAX_GAP_S,
    Checkpoint,
    find_checkpoint_visits,
)
from app.geo import EARTH_RADIUS_M, TrackPoint

T0 = datetime(2026, 5, 1, 8, 0, 0, tzinfo=timezone.utc)

# 测试轨迹围绕 (31.0, 121.0)：1° 纬度 ≈ 111195 m
HOME = (31.0, 121.0)
DEG_PER_M = 1.0 / math.radians(EARTH_RADIUS_M)  # 每米对应的纬度数（与实现同一投影常量）
COS_HOME = math.cos(math.radians(HOME[0]))


def make_seg(points):
    """points: [(lat, lon, 秒偏移), ...]"""
    return [
        TrackPoint(lat=la, lon=lo, ele=None, time=T0 + timedelta(seconds=s))
        for la, lo, s in points
    ]


def north(meters, base=HOME):
    """base 正北方向约 meters 米的位置。"""
    return (base[0] + meters * DEG_PER_M, base[1])


def east(meters, base=HOME):
    """base 正东方向约 meters 米的位置。"""
    return (base[0], base[1] + meters * DEG_PER_M / COS_HOME)


def offset(m_north, m_east):
    """HOME 正北 m_north 米、正东 m_east 米的位置。"""
    return (HOME[0] + m_north * DEG_PER_M, HOME[1] + m_east * DEG_PER_M / COS_HOME)


def cp(m_north=0.0, m_east=0.0, radius=50.0, name=""):
    """圆心在 HOME 北 m_north 米、东 m_east 米处的检查点。"""
    la, lo = offset(m_north, m_east)
    return Checkpoint(lat=la, lon=lo, radius_m=radius, name=name)


def still(latlon, *times):
    """在 latlon 原地不动的分段。"""
    return make_seg([(latlon[0], latlon[1], t) for t in times])


def test_crossing_outside_endpoints_interpolated():
    """两端都在圆外、线段穿圆：识别到访，进出时间按线段比例插值。"""
    # 从西 1000 米匀速到东 1000 米，500 秒走完（4 m/s），圆心 HOME 半径 50
    seg = make_seg([(*east(-1000), 0), (*east(1000), 500)])
    (r,) = find_checkpoint_visits([seg], [cp(radius=50.0)])
    assert len(r.visits) == 1
    v = r.visits[0]
    # x=-50 处进入（走了 950 米 → 237.5 s），x=50 处离开（262.5 s）
    assert abs((v.start_time - T0).total_seconds() - 237.5) < 1e-3
    assert abs((v.end_time - T0).total_seconds() - 262.5) < 1e-3
    assert abs(v.duration_s - 25.0) < 1e-3
    assert v.seg_index == 1
    assert abs(r.total_duration_s - 25.0) < 1e-3


def test_start_inside_uses_segment_start_time():
    """trkseg 起点在圆内：到访开始时间用该连续记录的首点原始时间，不外推。"""
    seg = make_seg([(*north(20), 0), (*north(200), 180)])  # 1 m/s 向北
    (r,) = find_checkpoint_visits([seg], [cp(radius=50.0)])
    assert len(r.visits) == 1
    v = r.visits[0]
    assert v.start_time == T0  # 首点在圆内，直接用首点原始时间
    assert abs((v.end_time - T0).total_seconds() - 30.0) < 1e-3  # y=50 处离开


def test_end_inside_uses_segment_end_time():
    """trkseg 终点在圆内：到访结束时间用该连续记录的末点原始时间。"""
    seg = make_seg([(*north(200), 0), (*north(20), 180)])  # 1 m/s 向南
    (r,) = find_checkpoint_visits([seg], [cp(radius=50.0)])
    assert len(r.visits) == 1
    v = r.visits[0]
    assert abs((v.start_time - T0).total_seconds() - 150.0) < 1e-3  # y=50 处进入
    assert v.end_time == T0 + timedelta(seconds=180)  # 末点在圆内，用末点原始时间


def test_whole_track_inside_merged_to_endpoint_times():
    """多条线段全程在圆内：合并为一次到访，起止为首末点原始时间。"""
    seg = make_seg([(*north(10), 0), (*east(10), 100), (*offset(-10, -10), 200)])
    (r,) = find_checkpoint_visits([seg], [cp(radius=100.0)])
    assert len(r.visits) == 1
    v = r.visits[0]
    assert v.start_time == T0
    assert v.end_time == T0 + timedelta(seconds=200)
    assert abs(v.duration_s - 200.0) < 1e-6


def test_gap_not_interpolated_or_merged():
    """断档（> 600 秒）不插值、不合并：断档两侧各 60 秒分别列出。"""
    seg = make_seg([
        (*HOME, 0), (*HOME, 60),      # 线段 1：圆内 [0, 60]
        (*HOME, 761), (*HOME, 821),   # 60→761 相隔 701 秒断档；线段 2：圆内 [761, 821]
    ])
    (r,) = find_checkpoint_visits([seg], [cp(radius=50.0)])
    # 若跨断档插值合并会得到一次 [0, 821] 的长到访；正确结果是断档两侧各一次
    assert len(r.visits) == 2
    assert abs(r.visits[0].duration_s - 60.0) < 1e-6
    assert abs(r.visits[1].duration_s - 60.0) < 1e-6
    assert r.visits[1].start_time == T0 + timedelta(seconds=761)


def test_gap_exactly_600s_counts():
    """相邻采样间隔恰好 600 秒：不算断档，正常构造线段。"""
    seg = make_seg([(*HOME, 0), (*HOME, MAX_GAP_S)])
    (r,) = find_checkpoint_visits([seg], [cp(radius=50.0)])
    assert len(r.visits) == 1
    assert abs(r.visits[0].duration_s - MAX_GAP_S) < 1e-6


def test_visit_around_gap_uses_record_endpoint_times():
    """断档前后的圆内点：到访分别以该连续记录的端点原始时间结算，不跨断档插值。"""
    seg = make_seg([
        (*north(200), 0),
        (*north(20), 180),    # 圆内
        (*north(20), 900),    # 与上一点相隔 720 秒 → 断档
        (*north(200), 1080),  # 断档后从圆内走出
    ])
    (r,) = find_checkpoint_visits([seg], [cp(radius=50.0)])
    assert len(r.visits) == 2
    v1, v2 = r.visits
    # 第一次：y=50 进入（t=150）→ 断档前的圆内点 t=180（用该点原始时间结算）
    assert abs((v1.start_time - T0).total_seconds() - 150.0) < 1e-3
    assert v1.end_time == T0 + timedelta(seconds=180)
    # 第二次：断档后的圆内点 t=900（从该点原始时间起）→ y=50 离开（t=930）
    assert v2.start_time == T0 + timedelta(seconds=900)
    assert abs((v2.end_time - T0).total_seconds() - 930.0) < 1e-3


def test_trkseg_boundary_not_merged():
    """不跨 trkseg 合并：两个分段各 50 秒在圆内，时间相接也不并成一次。"""
    segs = [
        make_seg([(*HOME, 0), (*HOME, 50)]),
        make_seg([(*HOME, 50), (*HOME, 100)]),
    ]
    (r,) = find_checkpoint_visits(segs, [cp(radius=50.0)])
    # 若跨分段合并会得到一次 100 秒到访；正确结果是两次各 50 秒
    assert len(r.visits) == 2
    assert all(abs(v.duration_s - 50.0) < 1e-6 for v in r.visits)
    assert {v.seg_index for v in r.visits} == {1, 2}


def test_two_visits_merged_across_segments_sorted():
    """进-出-进-出：跨线段的相接区间各自合并，两次到访按时间先后排列。"""
    seg = make_seg([
        (*north(200), 0),
        (*north(0), 200),
        (*north(200), 400),
        (*north(0), 600),
        (*north(200), 800),
    ])
    (r,) = find_checkpoint_visits([seg], [cp(radius=50.0)])
    assert len(r.visits) == 2
    v1, v2 = r.visits
    # 第一次：y=50 进入（t=150）→ 圆内折返 → y=50 离开（t=250）
    assert abs((v1.start_time - T0).total_seconds() - 150.0) < 1e-3
    assert abs((v1.end_time - T0).total_seconds() - 250.0) < 1e-3
    # 第二次：y=50 进入（t=550）→ y=50 离开（t=650）
    assert abs((v2.start_time - T0).total_seconds() - 550.0) < 1e-3
    assert abs((v2.end_time - T0).total_seconds() - 650.0) < 1e-3
    assert v1.start_time < v2.start_time


def test_tangent_not_counted():
    """线段与圆相切（判别式 = 0）：瞬时接触不计入到访。"""
    # 沿 HOME 正北 50 米处东西向经过，线段与半径 50 的圆相切
    seg = make_seg([(*offset(50, -100), 0), (*offset(50, 100), 100)])
    (r,) = find_checkpoint_visits([seg], [cp(radius=50.0)])
    assert r.visits == []


def test_grazing_outside_not_counted():
    """线段从圆外 1 毫米处擦过（不相交）：无到访。"""
    seg = make_seg([(*offset(50.001, -100), 0), (*offset(50.001, 100), 100)])
    (r,) = find_checkpoint_visits([seg], [cp(radius=50.0)])
    assert r.visits == []


def test_radius_boundary():
    """半径边界：约 49.9 米计入，约 50.1 米不计入。"""
    inside = find_checkpoint_visits([still(north(49.9), 0, 100)], [cp(radius=50.0)])
    outside = find_checkpoint_visits([still(north(50.1), 0, 100)], [cp(radius=50.0)])
    assert len(inside[0].visits) == 1
    assert abs(inside[0].visits[0].duration_s - 100.0) < 1e-6
    assert outside[0].visits == []


def test_multiple_checkpoints_independent():
    """多个检查点独立分析：到访与未到访各自正确，名称与序号保留。"""
    seg = still(HOME, 0, 100)
    results = find_checkpoint_visits([seg], [
        cp(radius=50.0, name="家"),
        cp(5000, 0, radius=50.0, name="公司"),
    ])
    assert len(results) == 2
    r1, r2 = results
    assert r1.index == 1 and r1.name == "家"
    assert len(r1.visits) == 1
    assert abs(r1.visits[0].duration_s - 100.0) < 1e-6
    assert abs(r1.total_duration_s - 100.0) < 1e-6
    assert r2.index == 2 and r2.name == "公司"
    assert r2.visits == []
    assert r2.total_duration_s == 0.0


def test_no_visit_when_far():
    """轨迹全程远离检查点：无到访。"""
    (r,) = find_checkpoint_visits([still(north(1000), 0, 100)], [cp(radius=50.0)])
    assert r.visits == []


def test_single_point_segment_no_visit():
    """单点 trkseg 构造不出线段：无到访且不报错。"""
    (r,) = find_checkpoint_visits([still(HOME, 0)], [cp(radius=50.0)])
    assert r.visits == []


def test_no_checkpoints_empty_result():
    """检查点列表为空：返回空结果。"""
    assert find_checkpoint_visits([still(HOME, 0, 100)], []) == []


def test_default_radius_50():
    """检查点半径默认 50 米。"""
    c = Checkpoint(lat=HOME[0], lon=HOME[1])
    assert c.radius_m == DEFAULT_RADIUS_M == 50.0


def test_zero_time_diff_rejected():
    """段内时间零时差：报明确错误。"""
    seg = make_seg([(*HOME, 0), (*HOME, 0)])
    try:
        find_checkpoint_visits([seg], [cp()])
    except ValueError as exc:
        assert "未递增" in str(exc)
    else:
        raise AssertionError("零时差应报明确错误")


def test_reversed_time_rejected():
    """段内时间倒序：报明确错误并指明分段序号。"""
    segs = [
        make_seg([(*HOME, 0), (*HOME, 60)]),
        make_seg([(*HOME, 120), (*HOME, 110)]),
    ]
    try:
        find_checkpoint_visits(segs, [cp()])
    except ValueError as exc:
        assert "未递增" in str(exc) and "第 2 个 trkseg" in str(exc)
    else:
        raise AssertionError("倒序时间应报明确错误")


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
