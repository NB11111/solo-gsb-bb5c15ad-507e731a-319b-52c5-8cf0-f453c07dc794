"""轨迹精简：Douglas-Peucker 算法按误差阈值抽稀轨迹点。

对每个原始 trkseg 独立处理（不跨分段连线）：

- 把段内经纬度投影到以该段首点为原点的局部米制平面（等距圆柱投影，
  与 geo.py 中口径一致），在平面内计算点到**有限线段**的距离；
- 迭代式 Douglas-Peucker：每个区间取到首尾连线距离最远的点，
  仅当该距离**超过**误差阈值时才保留该点并继续拆分两侧区间；
  首尾重合（零长度线段）时退化为点到端点的距离；
- 每段首尾点始终保留；选中点直接引用原始轨迹点，
  经纬度、海拔、时间原样保留，不做插值或修改；
- 单点段、两点段、重复点均可处理；显式栈迭代实现，不用递归，
  十万点级别的输入不会触发递归栈溢出。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .geo import EARTH_RADIUS_M, haversine_m

MIN_TOLERANCE_M = 1.0      # 误差阈值下限（米）
MAX_TOLERANCE_M = 100.0    # 误差阈值上限（米）
DEFAULT_TOLERANCE_M = 10.0  # 默认误差阈值（米）


@dataclass
class SimplifyResult:
    """轨迹精简结果；simplified_segments 与输入 segments 结构一一对应。"""

    simplified_segments: list  # list[list[TrackPoint]]，每段对应一个原始 trkseg
    points_before: int = 0
    points_after: int = 0
    distance_before_m: float = 0.0
    distance_after_m: float = 0.0


def _project_to_local_xy(seg: list) -> tuple:
    """把整段投影到以该段首点为原点的局部米制平面，返回 (xs, ys) 两个列表。"""
    lat0, lon0 = seg[0].lat, seg[0].lon
    cos0 = math.cos(math.radians(lat0))
    xs = [math.radians(p.lon - lon0) * cos0 * EARTH_RADIUS_M for p in seg]
    ys = [math.radians(p.lat - lat0) * EARTH_RADIUS_M for p in seg]
    return xs, ys


def _keep_indices(xs: list, ys: list, tolerance_m: float) -> list:
    """迭代式 Douglas-Peucker，返回需保留点的下标（升序，含首尾）。

    用显式栈代替递归；只比较平方距离，避免逐点开方。
    """
    n = len(xs)
    if n <= 2:
        return list(range(n))
    keep = bytearray(n)
    keep[0] = keep[n - 1] = 1
    tol2 = tolerance_m * tolerance_m
    stack = [(0, n - 1)]
    while stack:
        lo, hi = stack.pop()
        ax, ay = xs[lo], ys[lo]
        dx, dy = xs[hi] - ax, ys[hi] - ay
        seg_len2 = dx * dx + dy * dy
        max_d2 = tol2   # 只关心超过阈值的点：找不到即停止拆分该区间
        max_k = -1
        for k in range(lo + 1, hi):
            px = xs[k] - ax
            py = ys[k] - ay
            if seg_len2 == 0.0:
                # 首尾重合：退化为点到端点的距离
                d2 = px * px + py * py
            else:
                t = (px * dx + py * dy) / seg_len2
                if t < 0.0:
                    t = 0.0
                elif t > 1.0:
                    t = 1.0
                ddx = px - t * dx
                ddy = py - t * dy
                d2 = ddx * ddx + ddy * ddy
            if d2 > max_d2:
                max_d2 = d2
                max_k = k
        if max_k >= 0:
            keep[max_k] = 1
            stack.append((lo, max_k))
            stack.append((max_k, hi))
    return [k for k in range(n) if keep[k]]


def simplify_track(segments: list, tolerance_m: float = DEFAULT_TOLERANCE_M) -> SimplifyResult:
    """对整条轨迹（若干原始 trkseg）做 Douglas-Peucker 精简。

    每个 trkseg 独立处理，精简结果不跨分段连线；首尾点始终保留，
    选中点直接引用原始 TrackPoint（经纬度、海拔、时间不变）。
    tolerance_m 超出 1～100 范围时抛出 ValueError。
    """
    if not (MIN_TOLERANCE_M <= tolerance_m <= MAX_TOLERANCE_M):
        raise ValueError(
            f"误差阈值须在 {MIN_TOLERANCE_M:.0f}～{MAX_TOLERANCE_M:.0f} 米之间"
        )

    simplified: list = []
    points_before = points_after = 0
    distance_before = distance_after = 0.0

    for seg in segments:
        n = len(seg)
        if n == 0:
            continue
        points_before += n
        for a, b in zip(seg, seg[1:]):
            distance_before += haversine_m(a, b)

        xs, ys = _project_to_local_xy(seg)
        kept = [seg[k] for k in _keep_indices(xs, ys, tolerance_m)]

        points_after += len(kept)
        for a, b in zip(kept, kept[1:]):
            distance_after += haversine_m(a, b)
        simplified.append(kept)

    return SimplifyResult(
        simplified_segments=simplified,
        points_before=points_before,
        points_after=points_after,
        distance_before_m=distance_before,
        distance_after_m=distance_after,
    )
