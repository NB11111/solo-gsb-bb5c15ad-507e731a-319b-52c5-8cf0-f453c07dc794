"""参考路线偏离分析的单元测试。

可直接运行：python3 tests/test_deviation.py
或使用 pytest：python3 -m pytest tests/
"""
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.deviation import CORRIDOR_M, MAX_GAP_S, find_deviations
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


def offset(m_north, m_east):
    """HOME 正北 m_north 米、正东 m_east 米的位置。"""
    return (HOME[0] + m_north * DEG_PER_M, HOME[1] + m_east * DEG_PER_M / COS_HOME)


def main_track(*points):
    """单 trkseg 主轨迹：points 为 (北米, 东米, 秒偏移)。"""
    return [make_seg([(*offset(n, e), s) for n, e, s in points])]


def ref_track(*segments):
    """参考路线：每个元素是一个 trkseg 的 (北米, 东米) 点列；时间故意倒序填入，
    验证参考路线时间被忽略。"""
    out = []
    for points in segments:
        n = len(points)
        out.append([
            TrackPoint(lat=la, lon=lo, ele=None,
                       time=T0 + timedelta(seconds=(n - 1 - i) * 10))
            for i, (nn, ee) in enumerate(points)
            for la, lo in [offset(nn, ee)]
        ])
    return out


def test_whole_track_inside_corridor_no_deviation():
    """主轨迹全程平行于参考线 20 米：无偏离。"""
    main = main_track((-500, 20, 0), (0, 20, 500), (500, 20, 1000))
    ref = ref_track([(-500, 0), (500, 0)])
    r = find_deviations(main, ref)
    assert r.deviations == []
    assert r.deviation_total_s == 0.0


def test_whole_track_outside_corridor_single_deviation():
    """主轨迹全程在走廊外（80 米）：跨多条采样线段合并为一次偏离。"""
    main = main_track((-500, 80, 0), (0, 80, 500), (500, 80, 1000))
    ref = ref_track([(-500, 0), (500, 0)])
    r = find_deviations(main, ref)
    assert len(r.deviations) == 1
    d = r.deviations[0]
    assert d.start_time == T0
    assert d.end_time == T0 + timedelta(seconds=1000)
    assert abs(d.duration_s - 1000.0) < 1e-6
    assert abs(r.deviation_total_s - 1000.0) < 1e-6
    assert d.seg_index == 1


def test_crossing_corridor_enter_exit_interpolated():
    """主轨迹垂直穿越走廊：两次偏离，进出时刻与位置按线段比例插值。"""
    # 从北 100 米匀速走到南 100 米，200 秒；参考线为东西向直线（北 0 米）
    main = main_track((100, 0, 0), (-100, 0, 200))
    ref = ref_track([(0, -500), (0, 500)])
    r = find_deviations(main, ref)
    assert len(r.deviations) == 2
    d1, d2 = r.deviations
    # 第一次：[0, 50] 秒，从 (100, 0) 到 (50, 0)
    assert d1.start_time == T0
    assert abs((d1.end_time - T0).total_seconds() - 50.0) < 1e-3
    assert abs(d1.duration_s - 50.0) < 1e-3
    la, lo = offset(50, 0)
    assert abs(d1.end_lat - la) < 1e-9 and abs(d1.end_lon - lo) < 1e-9
    # 第二次：[150, 200] 秒，从 (-50, 0) 到 (-100, 0)
    assert abs((d2.start_time - T0).total_seconds() - 150.0) < 1e-3
    assert d2.end_time == T0 + timedelta(seconds=200)
    la, lo = offset(-50, 0)
    assert abs(d2.start_lat - la) < 1e-9 and abs(d2.start_lon - lo) < 1e-9


def test_mid_segment_deviation_with_inside_endpoints():
    """两端都在走廊内、途中却偏离：V 形参考线，主轨迹走弦，中段被识别为偏离。"""
    # 参考线：(-150, 0) → (0, -150) → (150, 0)（东向 150 米、南向 150 米的 V）
    # 主轨迹：沿东向直线 (西150,0) → (东150,0)，300 秒走完
    main = main_track((0, -150, 0), (0, 150, 300))
    ref = ref_track([(0, -150), (-150, 0), (0, 150)])
    r = find_deviations(main, ref)
    assert len(r.deviations) == 1
    d = r.deviations[0]
    # 走廊边界在 x = ±(150 − 50√2) 米处：t = 150 ∓ (150 − 50√2)·... 按 1 m/s：
    # 东向速度 1 m/s，x = -150 + t；边界 t = 50√2 与 300 − 50√2
    edge = 50.0 * math.sqrt(2.0)
    assert abs((d.start_time - T0).total_seconds() - edge) < 1e-2
    assert abs((d.end_time - T0).total_seconds() - (300.0 - edge)) < 1e-2
    assert abs(d.duration_s - (300.0 - 2.0 * edge)) < 1e-2
    # 两端确实在走廊内（偏离不覆盖 t=0 与 t=300）
    assert d.start_time > T0 and d.end_time < T0 + timedelta(seconds=300)


def test_gap_breaks_deviation():
    """断档（> 600 秒）断开：断档两侧各 60 秒的偏离分别列出，不合并。"""
    main = main_track((0, 100, 0), (0, 100, 60), (0, 100, 761), (0, 100, 821))
    ref = ref_track([(-500, 0), (500, 0)])
    r = find_deviations(main, ref)
    # 若跨断档合并会得到一次 [0, 821] 的长偏离；正确结果是断档两侧各一次
    assert len(r.deviations) == 2
    assert abs(r.deviations[0].duration_s - 60.0) < 1e-6
    assert abs(r.deviations[1].duration_s - 60.0) < 1e-6
    assert r.deviations[1].start_time == T0 + timedelta(seconds=761)


def test_gap_exactly_600s_counts():
    """相邻采样间隔恰好 600 秒：不算断档，正常构造线段。"""
    main = main_track((0, 100, 0), (0, 100, MAX_GAP_S))
    ref = ref_track([(-500, 0), (500, 0)])
    r = find_deviations(main, ref)
    assert len(r.deviations) == 1
    assert abs(r.deviations[0].duration_s - MAX_GAP_S) < 1e-6


def test_trkseg_boundary_not_merged():
    """不跨 trkseg 合并：两个分段各 50 秒偏离，时间相接也不并成一次。"""
    main = [
        make_seg([(*offset(0, 100), 0), (*offset(0, 100), 50)]),
        make_seg([(*offset(0, 100), 50), (*offset(0, 100), 100)]),
    ]
    ref = ref_track([(-500, 0), (500, 0)])
    r = find_deviations(main, ref)
    assert len(r.deviations) == 2
    assert all(abs(d.duration_s - 50.0) < 1e-6 for d in r.deviations)
    assert {d.seg_index for d in r.deviations} == {1, 2}


def test_reference_trksegs_not_connected():
    """参考路线的不同 trkseg 不相连：两个分离路段之间的空隙没有走廊。"""
    # 参考：两个独立 trkseg，东向路段 [0,100] 与 [300,400]（米）
    ref = ref_track([(0, 0), (0, 100)], [(0, 300), (0, 400)])
    # 主轨迹：在 x=200 米处南北向穿过（若参考两 trkseg 被相连，此处会在走廊内）
    main = main_track((-10, 200, 0), (10, 200, 20))
    r = find_deviations(main, ref)
    assert len(r.deviations) == 1
    assert abs(r.deviations[0].duration_s - 20.0) < 1e-6


def test_reference_single_trkseg_connected():
    """同一 trkseg 内的相邻点相连：连线走廊覆盖中间空隙，无偏离。"""
    # 参考：单个 trkseg 依次经过 (0,0)→(0,100)→(0,300)→(0,400)，含中间连线
    ref = ref_track([(0, 0), (0, 100), (0, 300), (0, 400)])
    main = main_track((-10, 200, 0), (10, 200, 20))
    r = find_deviations(main, ref)
    assert r.deviations == []


def test_tangent_merges_to_single_deviation():
    """主轨迹与走廊边界相切：瞬时接触不切开偏离，合并为一次。"""
    # 参考线为南北向直线（东 0 米）；主轨迹沿东 50 米平行经过（恰好相切）
    main = main_track((-100, 50, 0), (100, 50, 200))
    ref = ref_track([(-500, 0), (500, 0)])
    r = find_deviations(main, ref)
    assert len(r.deviations) == 1
    assert abs(r.deviations[0].duration_s - 200.0) < 1e-3


def test_corridor_boundary_50m():
    """走廊边界：约 49.9 米算在走廊内，约 50.1 米算偏离。"""
    ref = ref_track([(-500, 0), (500, 0)])
    inside = find_deviations(main_track((0, 49.9, 0), (0, 49.9, 100)), ref)
    outside = find_deviations(main_track((0, 50.1, 0), (0, 50.1, 100)), ref)
    assert inside.deviations == []
    assert len(outside.deviations) == 1
    assert abs(outside.deviations[0].duration_s - 100.0) < 1e-6


def test_finite_segment_not_infinite_line():
    """距离按有限参考路段计算：延长线上 60 米处的点不在走廊内。"""
    ref = ref_track([(0, 0), (0, 100)])  # 东向 0~100 米路段
    # 主轨迹静止在路段延长线上（东 160 米）：到无限直线距离 0，到有限路段 60 米
    r = find_deviations(main_track((0, 160, 0), (0, 160, 100)), ref)
    assert len(r.deviations) == 1
    assert abs(r.deviations[0].duration_s - 100.0) < 1e-6


def test_endpoint_disk_counts_as_corridor():
    """参考路段端点半径 50 米的圆盘也算走廊（端点外 42 米处无偏离）。"""
    ref = ref_track([(0, 0), (0, 100)])
    # (北 30, 东 130)：到路段东端点 (0,100) 的距离 √(30²+30²) ≈ 42.4 ≤ 50
    r = find_deviations(main_track((30, 130, 0), (30, 130, 100)), ref)
    assert r.deviations == []


def test_multiple_deviations_sorted_and_total():
    """进-出-进-出：三次偏离按时间先后排列，累计时长正确。"""
    # 参考：两个南北向路段，分别在东 -200 与 +200 米处（各自由独立 trkseg）
    ref = ref_track([(-100, -200), (100, -200)], [(-100, 200), (100, 200)])
    # 主轨迹：沿北 0 米从西 500 米匀速走到东 500 米，1000 秒（1 m/s，中途补一个采样点）
    main = main_track((0, -500, 0), (0, 0, 500), (0, 500, 1000))
    r = find_deviations(main, ref)
    # 走廊覆盖 x∈[-250,-150] 与 [150,250]：偏离为 [0,250)、(350,650)、(750,1000]
    assert len(r.deviations) == 3
    durs = [d.duration_s for d in r.deviations]
    assert abs(durs[0] - 250.0) < 1e-2
    assert abs(durs[1] - 300.0) < 1e-2
    assert abs(durs[2] - 250.0) < 1e-2
    starts = [d.start_time for d in r.deviations]
    assert starts == sorted(starts)
    assert abs(r.deviation_total_s - 800.0) < 1e-2


def test_deviation_path_points():
    """偏离段折线：含插值端点与途经的原始轨迹点。"""
    ref = ref_track([(-500, 0), (500, 0)])  # 南北向直线（东 0 米）
    # 主轨迹沿北 0 米向东：0→100→200→100→0 米，每段 100 秒
    main = main_track(
        (0, 0, 0), (0, 100, 100), (0, 200, 200), (0, 100, 300), (0, 0, 400),
    )
    r = find_deviations(main, ref)
    assert len(r.deviations) == 1
    d = r.deviations[0]
    # 东 50 米处进出：t=50 离开走廊，t=350 回到走廊
    assert abs((d.start_time - T0).total_seconds() - 50.0) < 1e-3
    assert abs((d.end_time - T0).total_seconds() - 350.0) < 1e-3
    assert abs(d.duration_s - 300.0) < 1e-3
    # 折线：插值起点 + 3 个原始中间点 + 插值终点
    assert len(d.path) == 5
    la, lo = offset(0, 50)
    assert abs(d.path[0].lat - la) < 1e-9 and abs(d.path[0].lon - lo) < 1e-9
    assert abs(d.path[-1].lat - la) < 1e-9 and abs(d.path[-1].lon - lo) < 1e-9
    for k, east in enumerate((100, 200, 100), start=1):
        la, lo = offset(0, east)
        assert abs(d.path[k].lat - la) < 1e-12
        assert abs(d.path[k].lon - lo) < 1e-12
    # 起止位置与折线端点一致
    assert d.start_lat == d.path[0].lat and d.start_lon == d.path[0].lon
    assert d.end_lat == d.path[-1].lat and d.end_lon == d.path[-1].lon


def test_stationary_outside_corridor():
    """原地不动（零长度线段）但在走廊外：正时长偏离。"""
    main = main_track((0, 100, 0), (0, 100, 120))
    ref = ref_track([(-500, 0), (500, 0)])
    r = find_deviations(main, ref)
    assert len(r.deviations) == 1
    assert abs(r.deviations[0].duration_s - 120.0) < 1e-6


def test_stationary_inside_corridor():
    """原地不动且在走廊内：无偏离。"""
    main = main_track((0, 30, 0), (0, 30, 120))
    ref = ref_track([(-500, 0), (500, 0)])
    assert find_deviations(main, ref).deviations == []


def test_single_point_main_trkseg_no_deviation():
    """主轨迹单点 trkseg 构造不出线段：无偏离且不报错。"""
    main = main_track((0, 100, 0))
    ref = ref_track([(-500, 0), (500, 0)])
    assert find_deviations(main, ref).deviations == []


def test_reference_without_segments_no_corridor():
    """参考路线只有单点 trkseg（构不成路段）：走廊为空，主轨迹全程偏离。"""
    main = main_track((0, 0, 0), (0, 0, 100))
    ref = [[TrackPoint(lat=HOME[0], lon=HOME[1], ele=None, time=T0)]]
    r = find_deviations(main, ref)
    assert len(r.deviations) == 1
    assert abs(r.deviations[0].duration_s - 100.0) < 1e-6


def test_reference_time_ignored():
    """参考路线时间被忽略：参考点时间倒序也不报错、不影响结果。"""
    main = main_track((-500, 20, 0), (0, 20, 500), (500, 20, 1000))
    ref = ref_track([(-500, 0), (0, 0), (500, 0)])  # ref_track 内时间故意倒序
    times = [p.time for seg in ref for p in seg]
    assert all(b < a for a, b in zip(times, times[1:]))  # 确认参考时间确实倒序
    assert find_deviations(main, ref).deviations == []


def test_zero_time_diff_rejected():
    """主轨迹段内时间零时差：报明确错误。"""
    main = main_track((0, 0, 0), (0, 0, 0))
    ref = ref_track([(-500, 0), (500, 0)])
    try:
        find_deviations(main, ref)
    except ValueError as exc:
        assert "未递增" in str(exc)
    else:
        raise AssertionError("零时差应报明确错误")


def test_reversed_time_rejected():
    """主轨迹段内时间倒序：报明确错误并指明分段序号。"""
    main = [
        make_seg([(*offset(0, 0), 0), (*offset(0, 0), 60)]),
        make_seg([(*offset(0, 0), 120), (*offset(0, 0), 110)]),
    ]
    ref = ref_track([(-500, 0), (500, 0)])
    try:
        find_deviations(main, ref)
    except ValueError as exc:
        assert "未递增" in str(exc) and "第 2 个 trkseg" in str(exc)
    else:
        raise AssertionError("倒序时间应报明确错误")


def test_corridor_radius_constant():
    """走廊半径口径为 50 米。"""
    assert CORRIDOR_M == 50.0


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
