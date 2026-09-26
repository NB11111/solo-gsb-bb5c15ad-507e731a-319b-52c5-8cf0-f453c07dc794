"""持续陡坡分析的单元测试。

可直接运行：python3 tests/test_steep.py
或使用 pytest：python3 -m pytest tests/
"""
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.geo import EARTH_RADIUS_M, TrackPoint
from app.steep import find_steep_sections

T0 = datetime(2026, 5, 1, 8, 0, 0, tzinfo=timezone.utc)


def make_seg(points):
    """points: [(lat, lon, ele 或 None, 秒偏移), ...]"""
    return [
        TrackPoint(lat=la, lon=lo, ele=ele, time=T0 + timedelta(seconds=s))
        for la, lo, ele, s in points
    ]


def step_lat(meters):
    """纯纬度变化时球面距离恰为 meters 的纬度差。"""
    return math.degrees(meters / EARTH_RADIUS_M)


def north_seg(eles, step_m=20.0, lat0=31.0, lon=121.0):
    """沿正北等距行进、海拔按 eles 给定的单 trkseg 轨迹。"""
    dlat = step_lat(step_m)
    return make_seg([
        (lat0 + i * dlat, lon, e, i * 10.0) for i, e in enumerate(eles)
    ])


def climb_eles(n, per_step, base=100.0):
    """每 20 米上升 per_step 米的等坡度海拔序列（n 个点）。"""
    return [base + i * per_step for i in range(n)]


def test_constant_uphill_detected():
    """恒定 8.5% 上坡（每 20 米 +1.7 m，200 m）：6 个标记合并为 [40,160]。"""
    seg = north_seg(climb_eles(11, 1.7))
    res = find_steep_sections([seg])
    assert res.has_ele
    assert len(res.sections) == 1
    s = res.sections[0]
    assert s.direction == "up"
    assert abs(s.start_m - 40.0) < 1e-6
    assert abs(s.end_m - 160.0) < 1e-6
    assert abs((s.end_m - s.start_m) - 120.0) < 1e-6
    assert abs(s.max_grade_pct - 8.5) < 1e-6
    assert s.window_count == 6
    assert s.seg_index == 1


def test_threshold_exactly_8_percent_listed():
    """恰为 8%（每 20 米 +1.6 m）满足 ≥ 8%，列出；7.9% 不满足，空结果。"""
    res = find_steep_sections([north_seg(climb_eles(11, 1.6))])
    assert len(res.sections) == 1
    assert abs(res.sections[0].max_grade_pct - 8.0) < 1e-6

    res = find_steep_sections([north_seg(climb_eles(11, 1.58))])
    assert res.has_ele and res.sections == []


def test_constant_downhill_detected():
    """恒定 −8.5% 下坡：方向 down，最大绝对坡度为 −8.5%（保留符号）。"""
    seg = north_seg(climb_eles(11, -1.7))
    res = find_steep_sections([seg])
    assert len(res.sections) == 1
    s = res.sections[0]
    assert s.direction == "down"
    assert abs(s.start_m - 40.0) < 1e-6 and abs(s.end_m - 160.0) < 1e-6
    assert abs(s.max_grade_pct - (-8.5)) < 1e-6


def test_min_length_exactly_100():
    """合并后恰为 100 米（5 个标记）列出；短于 100 米（4 个标记）不列出。"""
    # 180 m / 10 点：s=0..4 共 5 个标记 → [40,140]，恰 100 米
    res = find_steep_sections([north_seg(climb_eles(10, 1.7))])
    assert len(res.sections) == 1
    assert abs(res.sections[0].start_m - 40.0) < 1e-6
    assert abs(res.sections[0].end_m - 140.0) < 1e-6
    assert res.sections[0].window_count == 5

    # 160 m / 9 点：s=0..3 共 4 个标记 → [40,120]，80 米，不列出
    res = find_steep_sections([north_seg(climb_eles(9, 1.7))])
    assert res.has_ele and res.sections == []


def test_marks_do_not_reach_run_end():
    """标记中心是窗口中心：首尾各留 50 m；200 m 区间的区间止于 160 m 而非 200 m。"""
    res = find_steep_sections([north_seg(climb_eles(11, 1.7))])
    s = res.sections[0]
    assert abs(s.end_m - 160.0) < 1e-6
    assert abs(s.start_m - 40.0) < 1e-6


def test_partial_tail_window_not_completed():
    """末端不足 100 米不补：215 m 区间末尾的非整 20 米点不参与任何窗口。"""
    seg = north_seg(climb_eles(11, 1.7))  # 0..200 m
    seg.append(TrackPoint(
        lat=seg[-1].lat + step_lat(15.0), lon=121.0,
        ele=100.0 + 0.085 * 215.0, time=T0 + timedelta(seconds=110),
    ))
    res = find_steep_sections([seg])
    assert len(res.sections) == 1
    s = res.sections[0]
    assert abs(s.end_m - 160.0) < 1e-6
    assert abs(s.start_m - 40.0) < 1e-6
    assert s.window_count == 6
    assert abs(res.distance_m - 215.0) < 1e-6


def test_opposite_directions_kept_separate():
    """先长上坡再长下坡：方向不同不合并，得到两个区间（各 120 米）。"""
    eles = (
        climb_eles(10, 2.0)                      # 0..180 m：100 → 118
        + [118.0]                               # 180..200 m：20 m 平
        + [118.0 - 2.0 * i for i in range(1, 10)]  # 200..380 m：116 → 100
    )
    res = find_steep_sections([north_seg(eles)])
    assert len(res.sections) == 2
    up, down = res.sections
    assert up.direction == "up"
    assert abs(up.start_m - 40.0) < 1e-6 and abs(up.end_m - 160.0) < 1e-6
    assert down.direction == "down"
    assert abs(down.start_m - 220.0) < 1e-6 and abs(down.end_m - 340.0) < 1e-6
    assert abs(down.max_grade_pct - (-10.0)) < 1e-6


def test_plateau_splits_same_direction():
    """同向但中间有平缓段导致标记不相邻：分成两个区间，不跨空档合并。"""
    climb1 = climb_eles(10, 1.7)                 # 0..180 m，8.5%
    flat = [climb1[-1]] * 6                     # 180..280 m，100 m 平（衔接点 + 5 点）
    climb2 = [climb1[-1] + 1.7 * i for i in range(1, 10)]  # 280..460 m
    seg = north_seg(climb1 + flat[1:] + climb2)
    res = find_steep_sections([seg])
    assert len(res.sections) == 2
    a, b = res.sections
    assert a.direction == "up" and b.direction == "up"
    assert abs(a.start_m - 40.0) < 1e-6 and abs(a.end_m - 140.0) < 1e-6
    assert abs(b.start_m - 320.0) < 1e-6 and abs(b.end_m - 420.0) < 1e-6
    assert abs((b.end_m - b.start_m) - 100.0) < 1e-6


def test_max_grade_is_extremest_window():
    """合并区间的最大绝对坡度取其中最陡的窗口：8% 段中间夹 12% 段 → 峰值 11.2%。"""
    eles = [100.0]
    for _ in range(6):      # 0..120 m：8%
        eles.append(eles[-1] + 1.6)
    for _ in range(4):      # 120..200 m：12%
        eles.append(eles[-1] + 2.4)
    for _ in range(5):      # 200..300 m：8%
        eles.append(eles[-1] + 1.6)
    res = find_steep_sections([north_seg(eles)])
    assert len(res.sections) == 1
    s = res.sections[0]
    assert abs(s.max_grade_pct - 11.2) < 1e-6
    assert abs(s.start_m - 40.0) < 1e-6 and abs(s.end_m - 260.0) < 1e-6


def test_missing_ele_gap_not_merged():
    """缺失海拔处断开为两个 run：两侧各自的陡坡区间即使同向也不跨缺口合并。"""
    eles = climb_eles(10, 1.7) + [None] + [200.0 + 1.7 * i for i in range(10)]
    seg = north_seg(eles)  # 0..180 m 有效、200 m 缺海拔、220..400 m 有效
    res = find_steep_sections([seg])
    assert len(res.sections) == 2
    a, b = res.sections
    assert a.seg_index == 1 and b.seg_index == 1
    assert abs(a.start_m - 40.0) < 1e-6 and abs(a.end_m - 140.0) < 1e-6
    assert abs(b.start_m - 260.0) < 1e-6 and abs(b.end_m - 360.0) < 1e-6


def test_not_merged_across_trkseg():
    """两个原始 trkseg 各自的陡坡区间分别列出，不跨分段边界合并。"""
    seg1 = north_seg(climb_eles(10, 1.7), lat0=31.0, lon=121.0)
    seg2 = north_seg(climb_eles(10, 1.7), lat0=32.0, lon=122.0)
    res = find_steep_sections([seg1, seg2])
    assert len(res.sections) == 2
    a, b = res.sections
    assert a.seg_index == 1 and b.seg_index == 2
    assert abs(a.start_m - 40.0) < 1e-6 and abs(a.end_m - 140.0) < 1e-6
    # 第二个 trkseg 里程从 180 m 重新接续（全程里程轴）
    assert abs(b.start_m - 220.0) < 1e-6 and abs(b.end_m - 320.0) < 1e-6
    assert abs(res.distance_m - 360.0) < 1e-6


def test_resampled_from_irregular_original_spacing():
    """原始点间隔 50 米、海拔线性 8.5%：20 米重采样后结果与等距 20 米一致。"""
    seg = north_seg([100.0 + 4.25 * i for i in range(5)], step_m=50.0)  # 200 m
    res = find_steep_sections([seg])
    assert len(res.sections) == 1
    s = res.sections[0]
    assert abs(s.start_m - 40.0) < 1e-6 and abs(s.end_m - 160.0) < 1e-6
    assert abs(s.max_grade_pct - 8.5) < 1e-6


def test_flat_track_has_no_sections():
    """有海拔但全程水平：has_ele=True、sections 为空（页面显示明确空态）。"""
    res = find_steep_sections([north_seg([100.0] * 11)])
    assert res.has_ele and res.sections == []


def test_short_runs_have_no_sections():
    """单点区间与不足 100 米的区间无法形成完整窗口，不报错且为空。"""
    seg = north_seg([None, 88.0, None], step_m=50.0)   # 单点有效海拔
    res = find_steep_sections([seg])
    assert res.has_ele and res.sections == []

    seg = north_seg([100.0, 103.0, 106.0])             # 40 m 短区间
    res = find_steep_sections([seg])
    assert res.has_ele and res.sections == []


def test_no_ele_at_all():
    """全无有效海拔：has_ele=False、sections 为空（页面显示"无海拔数据"）。"""
    seg = north_seg([None, None, None, None], step_m=50.0)
    res = find_steep_sections([seg])
    assert not res.has_ele
    assert res.sections == []
    assert abs(res.distance_m - 150.0) < 1e-6


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
