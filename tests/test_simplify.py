"""轨迹精简（Douglas-Peucker）的单元测试。

可直接运行：python3 tests/test_simplify.py
或使用 pytest：python3 -m pytest tests/
"""
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.geo import EARTH_RADIUS_M, TrackPoint, haversine_m
from app.gpxutil import build_gpx, parse_gpx
from app.simplify import (
    DEFAULT_TOLERANCE_M,
    _keep_indices,
    simplify_track,
)

T0 = datetime(2026, 5, 1, 8, 0, 0, tzinfo=timezone.utc)
LAT0, LON0 = 31.0, 121.0


def xy_to_latlon(x, y):
    """把以 (LAT0, LON0) 为原点的局部米制坐标反算回经纬度（与投影互逆）。"""
    lat = LAT0 + math.degrees(y / EARTH_RADIUS_M)
    lon = LON0 + math.degrees(x / (EARTH_RADIUS_M * math.cos(math.radians(LAT0))))
    return lat, lon


def make_seg_xy(points):
    """points: [(x米, y米), ...]；等间隔时间，海拔逐点递增。"""
    seg = []
    for i, (x, y) in enumerate(points):
        lat, lon = xy_to_latlon(x, y)
        seg.append(TrackPoint(lat=lat, lon=lon, ele=500.0 + i,
                              time=T0 + timedelta(seconds=i * 10)))
    return seg


def test_collinear_points_removed():
    """共线点全部去除：11 点直线只留首尾，距离不变。"""
    seg = make_seg_xy([(k * 10.0, 0.0) for k in range(11)])
    res = simplify_track([seg], 10.0)
    assert res.simplified_segments[0] == [seg[0], seg[-1]]
    assert res.points_before == 11 and res.points_after == 2
    assert abs(res.distance_before_m - res.distance_after_m) < 1e-6


def test_outlier_above_tolerance_kept():
    """中间点偏移 30 m > 阈值 10 m：保留，三点全留。"""
    seg = make_seg_xy([(0, 0), (50, 30), (100, 0)])
    res = simplify_track([seg], 10.0)
    assert res.simplified_segments[0] == seg
    assert res.points_after == 3


def test_outlier_below_tolerance_removed():
    """中间点偏移 5 m < 阈值 10 m：去除。"""
    seg = make_seg_xy([(0, 0), (50, 5), (100, 0)])
    res = simplify_track([seg], 10.0)
    assert res.simplified_segments[0] == [seg[0], seg[-1]]


def test_equal_tolerance_not_split():
    """偏差恰好等于阈值时不拆分（超过才拆）：用平面坐标精确验证边界。"""
    xs = [0.0, 50.0, 100.0]
    ys = [0.0, 10.0, 0.0]          # 到首尾线段的距离恰好 10 m
    assert _keep_indices(xs, ys, 10.0) == [0, 2]
    ys_over = [0.0, 10.000001, 0.0]  # 略超阈值才拆分
    assert _keep_indices(xs, ys_over, 10.0) == [0, 1, 2]


def test_two_point_segment_kept():
    """两点段：首尾都保留。"""
    seg = make_seg_xy([(0, 0), (100, 0)])
    res = simplify_track([seg], 10.0)
    assert res.simplified_segments[0] == seg


def test_single_point_segment():
    """单点段：保留该点，不报错，距离为 0。"""
    seg = make_seg_xy([(10, 20)])
    res = simplify_track([seg], 10.0)
    assert res.simplified_segments[0] == seg
    assert res.points_after == 1
    assert res.distance_after_m == 0.0


def test_all_duplicate_points():
    """全部点重合：中间点到端点距离为 0，只留首尾。"""
    seg = make_seg_xy([(50, 50)] * 5)
    res = simplify_track([seg], 10.0)
    assert res.simplified_segments[0] == [seg[0], seg[-1]]
    assert res.points_after == 2


def test_duplicate_points_mixed():
    """中间夹重复点（落在首尾连线上）：不影响精简，只留首尾。"""
    seg = make_seg_xy([(0, 0), (50, 0), (50, 0), (100, 0)])
    res = simplify_track([seg], 10.0)
    assert res.simplified_segments[0] == [seg[0], seg[-1]]


def test_closed_segment_uses_point_distance():
    """首尾重合（零长度线段）：按中间点到端点的距离判定。"""
    seg = make_seg_xy([(0, 0), (30, 40), (0, 0)])   # 中间点距端点 50 m > 10
    res = simplify_track([seg], 10.0)
    assert res.simplified_segments[0] == seg

    seg2 = make_seg_xy([(0, 0), (3, 4), (0, 0)])    # 中间点距端点 5 m < 10
    res2 = simplify_track([seg2], 10.0)
    assert res2.simplified_segments[0] == [seg2[0], seg2[-1]]


def test_segments_processed_independently():
    """多 trkseg 各自精简：输出分段与输入一一对应，不跨段连线。"""
    seg1 = make_seg_xy([(0, 0), (50, 30), (100, 0)])    # 中间点保留
    seg2 = make_seg_xy([(200, 0), (250, 5), (300, 0)])  # 中间点去除
    res = simplify_track([seg1, seg2], 10.0)
    assert len(res.simplified_segments) == 2
    assert res.simplified_segments[0] == seg1
    assert res.simplified_segments[1] == [seg2[0], seg2[2]]
    assert res.points_after == 3 + 2


def test_kept_points_are_originals():
    """选中点直接引用原始轨迹点：经纬度、海拔、时间原样保留。"""
    seg = make_seg_xy([(0, 0), (50, 30), (100, 0)])
    res = simplify_track([seg], 10.0)
    kept = res.simplified_segments[0]
    assert kept[0] is seg[0] and kept[1] is seg[1] and kept[2] is seg[2]
    assert kept[1].lat == seg[1].lat and kept[1].lon == seg[1].lon
    assert kept[1].ele == seg[1].ele and kept[1].time == seg[1].time


def test_distance_stats():
    """前后点数与前后距离统计正确（haversine 逐段累计）。"""
    seg = make_seg_xy([(0, 0), (50, 5), (100, 0)])
    res = simplify_track([seg], 10.0)
    assert res.points_before == 3 and res.points_after == 2
    d_before = haversine_m(seg[0], seg[1]) + haversine_m(seg[1], seg[2])
    d_after = haversine_m(seg[0], seg[2])
    assert abs(res.distance_before_m - d_before) < 1e-6
    assert abs(res.distance_after_m - d_after) < 1e-6


def test_simplified_gpx_roundtrip():
    """精简结果导出 GPX 后可解析：分段结构一致、点数一致、时间与海拔保留。"""
    seg1 = make_seg_xy([(0, 0), (50, 30), (100, 15), (150, 0)])
    seg2 = make_seg_xy([(200, 0), (250, 5), (300, 0)])
    res = simplify_track([seg1, seg2], 10.0)
    # seg1: (50,30) 保留，(100,15) 在 (50,30)-(150,0) 连线上去除 → 3 点
    # seg2: (250,5) 偏移 5 m < 10 去除 → 2 点
    gpx_bytes = build_gpx(res.simplified_segments, "simplified-demo")
    segments, name = parse_gpx(gpx_bytes)
    assert name == "simplified-demo"
    assert len(segments) == 2
    assert len(segments[0]) == 3 and len(segments[1]) == 2
    assert segments[0][0].time == seg1[0].time
    assert segments[0][-1].time == seg1[-1].time
    assert segments[0][1].ele == seg1[1].ele
    assert abs(segments[0][1].lat - seg1[1].lat) < 1e-7


def test_tolerance_out_of_range_rejected():
    """误差阈值超出 1～100 米：报明确错误。"""
    seg = make_seg_xy([(0, 0), (100, 0)])
    for bad in (0.5, 0.0, -1.0, 100.5, 200.0):
        try:
            simplify_track([seg], bad)
        except ValueError as exc:
            assert "误差阈值" in str(exc)
        else:
            raise AssertionError(f"阈值 {bad} 应报明确错误")
    # 边界值合法
    simplify_track([seg], 1.0)
    simplify_track([seg], 100.0)
    assert DEFAULT_TOLERANCE_M == 10.0


def test_deep_split_chain_no_stack_overflow():
    """深度拆分链（振幅衰减锯齿，链深 > 1000）：迭代实现不触发递归栈溢出。"""
    n = 2000
    seg = make_seg_xy(
        [(k * 10.0, ((-1.0) ** k) * 50.0 * (1.0 - k / n)) for k in range(n)]
    )
    res = simplify_track([seg], 10.0)
    # 每轮从链首剥下一个保留点，拆分深度远超 Python 默认递归限制 1000
    assert res.points_after > 1000
    assert res.simplified_segments[0][0] is seg[0]
    assert res.simplified_segments[0][-1] is seg[-1]


def test_hundred_thousand_points():
    """十万点输入：共线长链一次扫描完成，不因递归栈溢出失败。"""
    n = 100_000
    seg = make_seg_xy([(k * 10.0, 0.0) for k in range(n)])
    res = simplify_track([seg], 10.0)
    assert res.points_before == n
    assert res.points_after == 2
    assert res.simplified_segments[0] == [seg[0], seg[-1]]


def test_hundred_thousand_points_with_spike():
    """十万点共线长链中夹一个超限偏移点：正确拆分，尖峰两侧端点因连线倾斜也被保留。"""
    n = 100_000
    pts = [(k * 10.0, 0.0) for k in range(n)]
    pts[50_000] = (500_000.0, 50.0)  # 偏移 50 m > 阈值
    seg = make_seg_xy(pts)
    res = simplify_track([seg], 10.0)
    # 尖峰保留后，两侧子区间的首尾连线倾斜约 1e-4，紧邻尖峰的原共线点
    # 到连线距离约 50 m > 阈值，同样被保留；再往外则共线收敛
    assert res.simplified_segments[0] == [
        seg[0], seg[49_999], seg[50_000], seg[50_001], seg[-1],
    ]
    assert res.points_after == 5


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
