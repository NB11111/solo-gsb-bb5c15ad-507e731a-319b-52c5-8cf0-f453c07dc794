"""参考路线偏离分析的单元测试。

可直接运行：python3 tests/test_deviation.py
或使用 pytest：python3 -m pytest tests/
"""
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.deviation import (
    CORRIDOR_RADIUS_M,
    MAX_GAP_S,
    find_deviations,
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


def make_ref(*segs):
    """参考路线：每个 seg 是 [(lat, lon), ...]；时间不参与分析，一律不设。"""
    return [[TrackPoint(lat=la, lon=lo) for la, lo in seg] for seg in segs]


def offset(m_north, m_east):
    """HOME 正北 m_north 米、正东 m_east 米的位置。"""
    return (HOME[0] + m_north * DEG_PER_M, HOME[1] + m_east * DEG_PER_M / COS_HOME)


def test_follow_reference_no_deviation():
    """主轨迹全程沿参考路线行进：无偏离，空结果。"""
    ref = make_ref([offset(0, 0), offset(0, 1000)])
    main = [make_seg([(offset(0, 100 * i)[0], offset(0, 100 * i)[1], 100 * i)
                      for i in range(10)])]
    res = find_deviations(main, ref)
    assert res.deviations == []
    assert res.deviation_segments == []
    assert res.total_duration_s == 0.0
    assert res.total_distance_m == 0.0


def test_follow_corner_reference_no_deviation():
    """主轨迹沿带拐点的参考折线行进：跨参考路段的走廊并集覆盖全程，无偏离。"""
    ref = make_ref([offset(0, 0), offset(0, 500), offset(500, 500)])
    main = [make_seg([(*offset(0, 0), 0), (*offset(0, 500), 450), (*offset(500, 500), 900)])]
    res = find_deviations(main, ref)
    assert res.deviations == []


def test_parallel_outside_corridor_one_deviation():
    """主轨迹在参考路线北侧 100 米平行行进：全程一次偏离。"""
    ref = make_ref([offset(0, 0), offset(0, 1000)])
    main = [make_seg([
        (*offset(100, 0), 0),
        (*offset(100, 500), 450),
        (*offset(100, 1000), 900),
    ])]
    res = find_deviations(main, ref)
    assert len(res.deviations) == 1
    d = res.deviations[0]
    assert d.start_time == T0
    assert d.end_time == T0 + timedelta(seconds=900)
    assert abs(d.duration_s - 900.0) < 1e-6
    assert abs(d.distance_m - 1000.0) < 1.0
    assert abs(res.total_duration_s - 900.0) < 1e-6
    assert abs(res.total_distance_m - 1000.0) < 1.0
    # 偏离段折线途经中间原始采样点
    (line,) = res.deviation_segments
    assert len(line) == 3


def test_leave_and_rejoin_interpolated():
    """驶出走廊又回到走廊：一次偏离，进出时刻与位置沿主轨迹线性插值。"""
    ref = make_ref([offset(0, 0), offset(0, 1000)])
    main = [make_seg([
        (*offset(0, 0), 0),
        (*offset(100, 500), 450),
        (*offset(0, 1000), 900),
    ])]
    res = find_deviations(main, ref)
    assert len(res.deviations) == 1
    d = res.deviations[0]
    # 第一线段 (0,0)→(100,500)：距参考 50 米处在 (50,250)，t=225s 驶出
    assert abs((d.start_time - T0).total_seconds() - 225.0) < 1e-3
    # 第二线段 (100,500)→(0,1000)：距参考 50 米处在 (50,750)，t=675s 回到走廊
    assert abs((d.end_time - T0).total_seconds() - 675.0) < 1e-3
    assert abs(d.duration_s - 450.0) < 1e-3
    n0, e0 = offset(50, 250)
    n1, e1 = offset(50, 750)
    assert abs(d.start_lat - n0) < 1e-9 and abs(d.start_lon - e0) < 1e-9
    assert abs(d.end_lat - n1) < 1e-9 and abs(d.end_lon - e1) < 1e-9
    # 偏离里程：两段 √(250²+50²)
    assert abs(d.distance_m - 2 * math.hypot(250, 50)) < 0.5
    # 偏离段折线：插值起点 → 原始中间点 → 插值终点
    (line,) = res.deviation_segments
    assert len(line) == 3
    mid = offset(100, 500)
    assert abs(line[1].lat - mid[0]) < 1e-12 and abs(line[1].lon - mid[1]) < 1e-12


def test_chord_deviates_between_inside_endpoints():
    """两端都在走廊内、途中却偏离：直穿弯道（抄近路）的线段必须识别。"""
    # 参考路线先向东 200 米再向北 200 米；主轨迹一条直线连接首尾
    ref = make_ref([offset(0, 0), offset(0, 200), offset(200, 200)])
    main = [make_seg([(*offset(0, 0), 0), (*offset(200, 200), 400)])]
    res = find_deviations(main, ref)
    assert len(res.deviations) == 1
    d = res.deviations[0]
    # 对角线上距任一参考路段 ≤ 50 米的只有两端各 1/4；中段 (0.25, 0.75) 偏离
    assert abs((d.start_time - T0).total_seconds() - 100.0) < 1e-3
    assert abs((d.end_time - T0).total_seconds() - 300.0) < 1e-3
    assert abs(d.duration_s - 200.0) < 1e-3
    n0, e0 = offset(50, 50)
    n1, e1 = offset(150, 150)
    assert abs(d.start_lat - n0) < 1e-9 and abs(d.start_lon - e0) < 1e-9
    assert abs(d.end_lat - n1) < 1e-9 and abs(d.end_lon - e1) < 1e-9
    assert abs(d.distance_m - 100 * math.sqrt(2)) < 0.5


def test_gap_breaks_deviation():
    """断档（> 600 秒）不插值、不合并：断档两侧各算一次偏离。"""
    ref = make_ref([offset(0, 0), offset(0, 400)])
    main = [make_seg([
        (*offset(100, 0), 0),
        (*offset(100, 100), 100),   # 线段 1：偏离 [0, 100]
        (*offset(100, 200), 800),   # 100→800 相隔 700 秒断档
        (*offset(100, 300), 900),   # 线段 3：偏离 [800, 900]
    ])]
    res = find_deviations(main, ref)
    # 若跨断档合并会得到一次 [0, 900] 的偏离；正确结果是断档两侧各一次
    assert len(res.deviations) == 2
    d1, d2 = res.deviations
    assert abs(d1.duration_s - 100.0) < 1e-6
    assert d2.start_time == T0 + timedelta(seconds=800)
    assert abs(d2.duration_s - 100.0) < 1e-6
    assert abs(res.total_duration_s - 200.0) < 1e-6
    assert abs(res.total_distance_m - 200.0) < 1.0


def test_gap_exactly_600s_connected():
    """相邻采样间隔恰好 600 秒：不算断档，偏离区间跨线段相接合并。"""
    ref = make_ref([offset(0, 0), offset(0, 1000)])
    main = [make_seg([
        (*offset(100, 0), 0),
        (*offset(100, 0), MAX_GAP_S),
        (*offset(100, 0), MAX_GAP_S + 100),
    ])]
    res = find_deviations(main, ref)
    assert len(res.deviations) == 1
    d = res.deviations[0]
    assert abs(d.duration_s - (MAX_GAP_S + 100)) < 1e-6
    assert d.distance_m == 0.0  # 原地不动


def test_trkseg_boundary_not_merged():
    """不跨 trkseg 合并：两个分段各自全程偏离，时间相接也不并成一次。"""
    ref = make_ref([offset(0, 0), offset(0, 1000)])
    segs = [
        make_seg([(*offset(100, 0), 0), (*offset(100, 0), 50)]),
        make_seg([(*offset(100, 0), 50), (*offset(100, 0), 100)]),
    ]
    res = find_deviations(segs, ref)
    assert len(res.deviations) == 2
    assert all(abs(d.duration_s - 50.0) < 1e-6 for d in res.deviations)
    assert {d.seg_index for d in res.deviations} == {1, 2}


def test_reference_trksegs_not_connected():
    """参考路线的不同 trkseg 不相连：两分段之间的空档没有走廊。"""
    # 参考路线两段：东 0~100 米、东 300~400 米，中间 200 米断开
    ref = make_ref([offset(0, 0), offset(0, 100)], [offset(0, 300), offset(0, 400)])
    main = [make_seg([(*offset(0, 0), 0), (*offset(0, 400), 400)])]  # 1 m/s 向东
    res = find_deviations(main, ref)
    # 若把参考 trkseg 相连，全程都在走廊内；正确结果是空档处偏离 [150, 250]
    assert len(res.deviations) == 1
    d = res.deviations[0]
    assert abs((d.start_time - T0).total_seconds() - 150.0) < 1e-3
    assert abs((d.end_time - T0).total_seconds() - 250.0) < 1e-3
    assert abs(d.duration_s - 100.0) < 1e-3
    assert abs(d.distance_m - 100.0) < 0.5


def test_corridor_radius_boundary():
    """走廊边界：约 49.9 米在走廊内，约 50.1 米全程偏离。"""
    ref = make_ref([offset(0, 0), offset(0, 1000)])
    inside = find_deviations(
        [make_seg([(*offset(49.9, 0), 0), (*offset(49.9, 1000), 100)])], ref)
    outside = find_deviations(
        [make_seg([(*offset(50.1, 0), 0), (*offset(50.1, 1000), 100)])], ref)
    assert inside.deviations == []
    assert len(outside.deviations) == 1
    assert abs(outside.deviations[0].duration_s - 100.0) < 1e-6
    assert CORRIDOR_RADIUS_M == 50.0


def test_stationary_outside_corridor():
    """原地不动（零长度采样线段）且在走廊外：正时长、零里程的偏离。"""
    ref = make_ref([offset(0, 0), offset(0, 1000)])
    main = [make_seg([(*offset(100, 0), 0), (*offset(100, 0), 120)])]
    res = find_deviations(main, ref)
    assert len(res.deviations) == 1
    d = res.deviations[0]
    assert abs(d.duration_s - 120.0) < 1e-6
    assert d.distance_m == 0.0


def test_stationary_inside_corridor():
    """原地不动且在走廊内：无偏离。"""
    ref = make_ref([offset(0, 0), offset(0, 1000)])
    main = [make_seg([(*offset(20, 0), 0), (*offset(20, 0), 120)])]
    assert find_deviations(main, ref).deviations == []


def test_degenerate_reference_segment_is_point_corridor():
    """零长度参考路段（重复点）退化为 50 米圆：40 米处在走廊内，60 米处偏离。"""
    ref = make_ref([offset(0, 0), offset(0, 0)])
    near = find_deviations(
        [make_seg([(*offset(0, 40), 0), (*offset(0, 40), 100)])], ref)
    far = find_deviations(
        [make_seg([(*offset(0, 60), 0), (*offset(0, 60), 100)])], ref)
    assert near.deviations == []
    assert len(far.deviations) == 1
    assert abs(far.deviations[0].duration_s - 100.0) < 1e-6


def test_reference_without_segments_everything_deviates():
    """参考路线只有单点 trkseg（构不成参考路段）：主轨迹全程偏离。"""
    ref = make_ref([offset(0, 0)])
    main = [make_seg([(*offset(0, 0), 0), (*offset(0, 100), 100)])]
    res = find_deviations(main, ref)
    assert len(res.deviations) == 1
    assert abs(res.deviations[0].duration_s - 100.0) < 1e-6


def test_single_point_main_segment_no_deviation():
    """主轨迹单点 trkseg 构造不出采样线段：无偏离且不报错。"""
    ref = make_ref([offset(0, 0), offset(0, 1000)])
    main = [make_seg([(*offset(100, 0), 0)])]
    assert find_deviations(main, ref).deviations == []


def test_deviations_sorted_across_trksegs():
    """多个 trkseg 的偏离按开始时间先后排列，并附原始 seg_index。"""
    ref = make_ref([offset(0, 0), offset(0, 1000)])
    segs = [
        make_seg([(*offset(100, 0), 500), (*offset(100, 0), 600)]),
        make_seg([(*offset(100, 0), 0), (*offset(100, 0), 100)]),
    ]
    res = find_deviations(segs, ref)
    assert len(res.deviations) == 2
    assert res.deviations[0].seg_index == 2
    assert res.deviations[0].start_time == T0
    assert res.deviations[1].seg_index == 1
    assert res.deviations[1].start_time == T0 + timedelta(seconds=500)
    assert len(res.deviation_segments) == 2


def test_zero_time_diff_rejected():
    """主轨迹段内时间零时差：报明确错误。"""
    ref = make_ref([offset(0, 0), offset(0, 1000)])
    main = [make_seg([(*offset(0, 0), 0), (*offset(0, 10), 0)])]
    try:
        find_deviations(main, ref)
    except ValueError as exc:
        assert "未递增" in str(exc)
    else:
        raise AssertionError("零时差应报明确错误")


def test_reversed_time_rejected():
    """主轨迹段内时间倒序：报明确错误并指明分段序号。"""
    ref = make_ref([offset(0, 0), offset(0, 1000)])
    segs = [
        make_seg([(*offset(0, 0), 0), (*offset(0, 10), 60)]),
        make_seg([(*offset(0, 0), 120), (*offset(0, 10), 110)]),
    ]
    try:
        find_deviations(segs, ref)
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
