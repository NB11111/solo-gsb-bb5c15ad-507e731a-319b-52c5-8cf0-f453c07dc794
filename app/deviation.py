"""参考路线偏离分析：找出主轨迹偏离参考路线 50 米走廊的连续时间区间。

- 主轨迹使用上传时保存的原始轨迹（各原始 trkseg）；参考路线复用第二条上传的
  GPX，只取其原始 trkseg 的相邻点连线作为**有限**参考路段（折线），忽略其时间；
  参考路线的不同 trkseg 之间不相连；
- 主轨迹上某点到**任一**有限参考路段的最短距离 ≤ 50 米即算在走廊内；
- 对每条主轨迹采样线段，在主段首点的局部米制投影（等距圆柱）中求它落在各参考
  路段 50 米走廊（圆角矩形：中间矩形 + 两端半圆）内的参数区间并取并集，
  补集即该线段的偏离区间；两端都在走廊内、途中却偏离的线段同样被识别；
- 偏离的进出时刻与位置沿主轨迹采样线段按参数线性插值；
- 同一 trkseg 内相邻点时间必须严格递增，否则抛出 ValueError；
- 相邻采样间隔超过 600 秒视为断档：断档处不构造线段、不插值，断档两侧的偏离
  区间不合并；trkseg 边界同样断开——仅合并同一 trkseg 内相接的正时长偏离区间
  （相切只产生零时长区间，浮点噪声以 1 毫秒容差过滤）。
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .geo import EARTH_RADIUS_M, TrackPoint, to_local_xy

CORRIDOR_M = 50.0   # 走廊半径（米）：到任一有限参考路段的最短距离不超过该值算在走廊内
MAX_GAP_S = 600.0   # 断档阈值（秒）：相邻采样间隔超过 10 分钟不构造线段、不合并

_MERGE_EPS_S = 1e-3    # 偏离区间相接判定的浮点容差（秒）
_MIN_DURATION_S = 1e-3  # 正时长判定的浮点容差（秒）：相切只产生零时长（浮点噪声级）区间
_U_EPS = 1e-9          # 线段参数区间合并/补集的浮点容差

_M_PER_DEG = math.radians(EARTH_RADIUS_M)  # 每度纬度对应的米数（与 geo.py 同一投影常量）
_R2 = CORRIDOR_M * CORRIDOR_M


@dataclass
class Deviation:
    """一次偏离：同一 trkseg 内相接偏离区间合并后的结果。"""

    seg_index: int        # 原始 trkseg 序号（从 1 开始）
    start_time: datetime  # 偏离开始时刻（穿越走廊边界时为按线段比例插值的时刻）
    end_time: datetime    # 偏离结束时刻
    duration_s: float     # 偏离秒数（end_time - start_time，恒为正）
    start_lat: float      # 偏离开始位置（主轨迹上）
    start_lon: float
    end_lat: float        # 偏离结束位置（主轨迹上）
    end_lon: float
    path: list = field(default_factory=list)  # list[TrackPoint]，偏离段折线（含插值端点），用于预览


@dataclass
class DeviationResult:
    """偏离分析结果。"""

    deviations: list = field(default_factory=list)  # list[Deviation]，按时间先后排列
    deviation_total_s: float = 0.0                  # 各次偏离秒数之和


@dataclass
class _RefSeg:
    """一条有限参考路段（无时间），附经纬度包围盒用于预筛选。"""

    lat0: float
    lon0: float
    lat1: float
    lon1: float
    min_lon: float
    max_lon: float
    min_lat: float
    max_lat: float


def _check_times(segments: list) -> None:
    """校验主轨迹每个 trkseg 内相邻时间严格递增；零时差或倒序时抛出 ValueError。"""
    for seg_idx, seg in enumerate(segments, start=1):
        for i, (a, b) in enumerate(zip(seg, seg[1:]), start=1):
            if b.time <= a.time:
                raise ValueError(
                    f"第 {seg_idx} 个 trkseg 中第 {i}→{i + 1} 个轨迹点的时间未递增"
                    "（零时差或倒序），无法进行参考路线偏离分析"
                )


def _disk_interval(bx: float, by: float, px: float, py: float):
    """主线段上满足 |P(u) − p|² ≤ R² 的参数 u 区间（裁剪到 [0,1]）；无则返回 None。

    主线段首点为投影原点，P(u) = u·(bx, by)；p 为参考路段端点的投影坐标。
    相切（判别式 = 0）只产生零宽度区间，不影响结果，按无区间处理。
    """
    aa = bx * bx + by * by
    cc = px * px + py * py - _R2
    if aa == 0.0:  # 主线段两端重合（原地不动）：整段要么都在圆盘内，要么都在外
        return (0.0, 1.0) if cc <= 0.0 else None
    bb = -2.0 * (px * bx + py * by)
    disc = bb * bb - 4.0 * aa * cc
    if disc <= 0.0:
        return None
    sq = math.sqrt(disc)
    u0 = max((-bb - sq) / (2.0 * aa), 0.0)
    u1 = min((-bb + sq) / (2.0 * aa), 1.0)
    return (u0, u1) if u0 < u1 else None


def _rect_interval(bx: float, by: float, cx: float, cy: float, dx: float, dy: float):
    """主线段上投影落在参考路段纵向范围内、且垂直距离 ≤ R 的参数 u 区间。

    即参考路段走廊的"中间矩形"部分：w(u) ∈ [0,1] 且 |perp(u)| ≤ R，
    两者对 u 都是线性的，分别解线性不等式后求交；退化参考路段（两端重合）
    没有矩形部分，由端点圆盘覆盖。
    """
    wx, wy = dx - cx, dy - cy
    len2 = wx * wx + wy * wy
    if len2 == 0.0:
        return None
    length = math.sqrt(len2)
    # w(u) = ((P(u) − C)·w) / |w|²，P(u) = u·(bx, by)
    a1 = (bx * wx + by * wy) / len2
    a0 = -(cx * wx + cy * wy) / len2
    # perp(u) = cross(w, P(u) − C) / |w|
    b1 = (wx * by - wy * bx) / length
    b0 = -(wx * cy - wy * cx) / length
    lo, hi = 0.0, 1.0
    if a1 > 0.0:
        lo = max(lo, -a0 / a1)
        hi = min(hi, (1.0 - a0) / a1)
    elif a1 < 0.0:
        lo = max(lo, (1.0 - a0) / a1)
        hi = min(hi, -a0 / a1)
    elif not 0.0 <= a0 <= 1.0:
        return None
    if b1 > 0.0:
        lo = max(lo, (-CORRIDOR_M - b0) / b1)
        hi = min(hi, (CORRIDOR_M - b0) / b1)
    elif b1 < 0.0:
        lo = max(lo, (CORRIDOR_M - b0) / b1)
        hi = min(hi, (-CORRIDOR_M - b0) / b1)
    elif abs(b0) > CORRIDOR_M:
        return None
    return (lo, hi) if lo < hi else None


def _merge_u(intervals: list) -> list:
    """合并相接（或重叠）的参数区间；输出按起点升序、互不重叠。"""
    merged: list = []
    for u0, u1 in sorted(intervals):
        if merged and u0 <= merged[-1][1] + _U_EPS:
            merged[-1][1] = max(merged[-1][1], u1)
        else:
            merged.append([u0, u1])
    return merged


def _corridor_intervals(a: TrackPoint, b: TrackPoint, refs: list, min_lons: list) -> list:
    """主线段 ab 落在任一参考路段 50 米走廊内的参数区间并集（合并后、升序）。

    以 a 为原点做局部米制投影；参考路段先按经纬度包围盒（外扩 50 米）预筛选，
    只排除投影距离必然超过 50 米的路段，不影响结果。
    """
    bx, by = to_local_xy(b.lat, b.lon, a.lat, a.lon)
    cos_lat = max(math.cos(math.radians(max(abs(a.lat), abs(b.lat)))), 1e-9)
    dlat = CORRIDOR_M / _M_PER_DEG
    dlon = dlat / cos_lat
    lo_lon = min(a.lon, b.lon) - dlon
    hi_lon = max(a.lon, b.lon) + dlon
    lo_lat = min(a.lat, b.lat) - dlat
    hi_lat = max(a.lat, b.lat) + dlat

    intervals = []
    # refs 按 min_lon 升序：只需考察 min_lon ≤ hi_lon 的前缀
    for r in refs[: bisect.bisect_right(min_lons, hi_lon)]:
        if r.max_lon < lo_lon or r.min_lat > hi_lat or r.max_lat < lo_lat:
            continue
        cx, cy = to_local_xy(r.lat0, r.lon0, a.lat, a.lon)
        dx, dy = to_local_xy(r.lat1, r.lon1, a.lat, a.lon)
        rect = _rect_interval(bx, by, cx, cy, dx, dy)
        if rect is not None:
            intervals.append(rect)
        for px, py in ((cx, cy), (dx, dy)):
            disk = _disk_interval(bx, by, px, py)
            if disk is not None:
                intervals.append(disk)
    return _merge_u(intervals)


def _complement(merged: list) -> list:
    """合并后走廊区间在 [0,1] 内的补集，即该采样线段的偏离参数区间。"""
    out = []
    prev = 0.0
    for u0, u1 in merged:
        if u0 - prev > _U_EPS:
            out.append((prev, u0))
        prev = max(prev, u1)
    if 1.0 - prev > _U_EPS:
        out.append((prev, 1.0))
    return out


def _point_at(seg: list, i: int, u: float) -> TrackPoint:
    """采样线段 seg[i]→seg[i+1] 上参数 u 处的位置；端点处直接引用原始轨迹点。"""
    if u <= 0.0:
        return seg[i]
    if u >= 1.0:
        return seg[i + 1]
    a, b = seg[i], seg[i + 1]
    return TrackPoint(lat=a.lat + (b.lat - a.lat) * u, lon=a.lon + (b.lon - a.lon) * u)


def _segment_deviations(seg: list, seg_idx: int, refs: list, min_lons: list) -> list:
    """一个主轨迹 trkseg 内的偏离区间列表（按时间先后）。

    逐条采样线段求偏离参数区间，把相接（时间差不超过浮点容差）的区间合并；
    断档（> 600 秒）处不构造线段，区间在断档处天然断开；仅保留正时长偏离。
    """
    times = [p.time.timestamp() for p in seg]
    groups = []  # 每个元素 [首线段序号, 起始 u, 末线段序号, 结束 u]
    for i in range(len(seg) - 1):
        dt = times[i + 1] - times[i]
        if dt > MAX_GAP_S:
            continue  # 断档：不跨断档插值
        for u0, u1 in _complement(_corridor_intervals(seg[i], seg[i + 1], refs, min_lons)):
            if groups:
                g = groups[-1]
                g_end = times[g[2]] + (times[g[2] + 1] - times[g[2]]) * g[3]
                if times[i] + dt * u0 - g_end <= _MERGE_EPS_S:
                    g[2], g[3] = i, u1  # 相接：并入上一段偏离
                    continue
            groups.append([i, u0, i, u1])

    out = []
    for i0, u0, i1, u1 in groups:
        t0 = times[i0] + (times[i0 + 1] - times[i0]) * u0
        t1 = times[i1] + (times[i1 + 1] - times[i1]) * u1
        if t1 - t0 <= _MIN_DURATION_S:
            continue  # 仅列出正时长偏离（相切等零时长区间不计）
        path = [_point_at(seg, i0, u0)]
        path.extend(seg[k + 1] for k in range(i0, i1))
        path.append(_point_at(seg, i1, u1))
        out.append(Deviation(
            seg_index=seg_idx,
            start_time=datetime.fromtimestamp(t0, tz=timezone.utc),
            end_time=datetime.fromtimestamp(t1, tz=timezone.utc),
            duration_s=t1 - t0,
            start_lat=path[0].lat,
            start_lon=path[0].lon,
            end_lat=path[-1].lat,
            end_lon=path[-1].lon,
            path=path,
        ))
    return out


def find_deviations(segments: list, ref_segments: list) -> DeviationResult:
    """分析主轨迹偏离参考路线 50 米走廊的区间。

    segments 为主轨迹 list[list[TrackPoint]]（含时间，须严格递增）；
    ref_segments 为参考路线 list[list[TrackPoint]]（只取相邻点连线，忽略时间，
    不同 trkseg 不相连）。主轨迹时间未递增时抛出 ValueError，由接口层转为提示。
    """
    _check_times(segments)
    refs = [
        _RefSeg(p.lat, p.lon, q.lat, q.lon,
                min(p.lon, q.lon), max(p.lon, q.lon),
                min(p.lat, q.lat), max(p.lat, q.lat))
        for rseg in ref_segments
        for p, q in zip(rseg, rseg[1:])
    ]
    refs.sort(key=lambda r: r.min_lon)
    min_lons = [r.min_lon for r in refs]

    deviations = []
    for seg_idx, seg in enumerate(segments, start=1):
        deviations.extend(_segment_deviations(seg, seg_idx, refs, min_lons))
    deviations.sort(key=lambda d: d.start_time)
    return DeviationResult(
        deviations=deviations,
        deviation_total_s=sum(d.duration_s for d in deviations),
    )
