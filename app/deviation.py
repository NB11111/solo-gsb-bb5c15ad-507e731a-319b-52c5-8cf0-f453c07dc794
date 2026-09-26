"""参考路线偏离分析：找出主轨迹偏离参考路线 50 米走廊的连续时间区间。

- 参考路线来自第二条上传的 GPX（复用 /api/upload 入口），只取其原始 trkseg
  折线几何，完全忽略其时间；主轨迹使用上传时保存的原始数据；
- 参考路线的每个 trkseg 内相邻点构成有限参考路段，不同 trkseg 之间不相连；
  到任一有限参考路段的最短距离 ≤ 50 米即在走廊内（走廊 = 参考路段外扩 50 米
  的凸"胶囊"区域）；零长度参考路段退化为以该点为圆心的 50 米圆；
- 对主轨迹每条采样线段，逐一求其落在各参考路段走廊内的参数区间并取并集，
  并集在 [0, 1] 上的补集即该线段的偏离区间——两端都在走廊内、途中却偏离
  （如直穿弯道）的线段由此自然识别；相切（恰好 50 米）只是瞬时接触，不计偏离；
- 偏离的进出时刻与位置沿主轨迹按线段比例线性插值；主轨迹同一 trkseg 内
  相邻点时间必须严格递增，否则抛出 ValueError；
- 相邻采样间隔超过 600 秒视为断档：断档处不构造线段、不插值，断档两侧的
  偏离区间不合并；trkseg 边界同样断开，仅合并同一 trkseg 内相接的偏离区间，
  且仅列出正时长的偏离（浮点噪声以 1 毫秒容差过滤）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .geo import TrackPoint, haversine_m, to_local_xy

CORRIDOR_RADIUS_M = 50.0  # 走廊半径（米）：到任一有限参考路段的最短距离不超过该值即在走廊内
MAX_GAP_S = 600.0         # 断档阈值（秒）：相邻采样间隔超过 10 分钟不构造线段、不合并
CELL_M = 500.0            # 参考路段网格索引的单元边长（米）

_MERGE_EPS_S = 1e-3       # 相邻偏离区间相接判定的浮点容差（秒）
_MIN_DURATION_S = 1e-3    # 正时长判定的浮点容差（秒）
_UNION_EPS_T = 1e-9       # 走廊参数区间并集相接判定的浮点容差
_MIN_PARAM = 1e-12        # 偏离参数区间的最小长度（更短的视为浮点噪声）


@dataclass
class Deviation:
    """一次偏离：同一 trkseg 内相接偏离区间合并后的结果。"""

    seg_index: int        # 主轨迹原始 trkseg 序号（从 1 开始）
    start_time: datetime  # 驶出走廊的时刻（穿越走廊边界时按线段比例插值）
    end_time: datetime    # 回到走廊的时刻
    duration_s: float     # 偏离秒数（end_time - start_time，恒为正）
    distance_m: float     # 偏离段里程（haversine 逐段累计，含插值端点）
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float


@dataclass
class DeviationResult:
    """偏离分析结果。"""

    deviations: list = field(default_factory=list)          # list[Deviation]，按时间先后
    deviation_segments: list = field(default_factory=list)  # list[list[TrackPoint]]，预览用折线
    total_duration_s: float = 0.0
    total_distance_m: float = 0.0


class _RefIndex:
    """参考路段（局部米制坐标）的均匀网格索引：按外扩走廊半径的包围盒分桶，
    避免主轨迹线段与全部参考路段两两比较。"""

    def __init__(self):
        self.segs = []   # (cx, cy, dx, dy)
        self.grid = {}   # (i, j) -> [路段序号]

    @staticmethod
    def _cells(x0, y0, x1, y1):
        i0, i1 = math.floor(x0 / CELL_M), math.floor(x1 / CELL_M)
        j0, j1 = math.floor(y0 / CELL_M), math.floor(y1 / CELL_M)
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                yield (i, j)

    def add(self, cx, cy, dx, dy):
        idx = len(self.segs)
        self.segs.append((cx, cy, dx, dy))
        r = CORRIDOR_RADIUS_M
        for key in self._cells(min(cx, dx) - r, min(cy, dy) - r,
                               max(cx, dx) + r, max(cy, dy) + r):
            self.grid.setdefault(key, []).append(idx)

    def candidates(self, ax, ay, bx, by):
        """可能与线段 ab 的走廊相交的参考路段序号（包围盒外扩走廊半径粗筛）。"""
        r = CORRIDOR_RADIUS_M
        out = set()
        for key in self._cells(min(ax, bx) - r, min(ay, by) - r,
                               max(ax, bx) + r, max(ay, by) + r):
            out.update(self.grid.get(key, ()))
        return out


def _check_times(segments: list) -> None:
    """校验主轨迹每个 trkseg 内相邻时间严格递增；零时差或倒序时抛出 ValueError。"""
    for seg_idx, seg in enumerate(segments, start=1):
        for i, (a, b) in enumerate(zip(seg, seg[1:]), start=1):
            if b.time <= a.time:
                raise ValueError(
                    f"第 {seg_idx} 个 trkseg 中第 {i}→{i + 1} 个轨迹点的时间未递增"
                    "（零时差或倒序），无法进行参考路线偏离分析"
                )


def _abs_leq_interval(alpha, beta, bound):
    """|alpha + beta·t| ≤ bound 的 t 区间 (lo, hi)；恒不成立返回 None。"""
    if abs(beta) < 1e-12:
        return (-math.inf, math.inf) if abs(alpha) <= bound else None
    t1 = (-bound - alpha) / beta
    t2 = (bound - alpha) / beta
    return (min(t1, t2), max(t1, t2))


def _range_interval(alpha, beta, lo, hi):
    """lo ≤ alpha + beta·t ≤ hi 的 t 区间；恒不成立返回 None。"""
    if abs(beta) < 1e-12:
        return (-math.inf, math.inf) if lo <= alpha <= hi else None
    t1 = (lo - alpha) / beta
    t2 = (hi - alpha) / beta
    return (min(t1, t2), max(t1, t2))


def _intersect(i1, i2):
    if i1 is None or i2 is None:
        return None
    lo, hi = max(i1[0], i2[0]), min(i1[1], i2[1])
    return (lo, hi) if lo <= hi else None


def _disk_interval(bx, by, px, py, r):
    """|t·(bx,by) − (px,py)| ≤ r 的 t 区间；无（或仅相切）返回 None。"""
    aa = bx * bx + by * by
    if aa == 0.0:  # 主轨迹采样线段退化为点（原地不动）
        return (-math.inf, math.inf) if px * px + py * py <= r * r else None
    bb = -2.0 * (bx * px + by * py)
    cc = px * px + py * py - r * r
    disc = bb * bb - 4.0 * aa * cc
    if disc <= 0.0:
        return None  # 相切只是瞬时接触，不构成区间
    sq = math.sqrt(disc)
    return ((-bb - sq) / (2.0 * aa), (-bb + sq) / (2.0 * aa))


def _capsule_interval(bx, by, cx, cy, dx, dy, r):
    """主轨迹采样线段 P(t) = t·(bx,by)（t∈[0,1]，起点为局部原点）上，到有限
    参考路段 (cx,cy)—(dx,dy) 的最短距离 ≤ r 的参数区间；无（或仅相切）返回 None。

    走廊是凸集（参考路段与半径 r 圆盘的闵可夫斯基和），可分解为
    "矩形部分 + 两端圆盘"；直线与凸集的交必为单一区间，
    故三部分各自求区间后取包络即可。
    """
    parts = []
    ux, uy = dx - cx, dy - cy
    len2 = ux * ux + uy * uy
    if len2 > 0.0:
        length = math.sqrt(len2)
        # 矩形部分 = 垂直方向 |cross(P(t)−C, u)| ≤ r·|u| 且沿线方向 0 ≤ dot(P(t)−C, u) ≤ |u|²
        rect = _intersect(
            _abs_leq_interval(-(cx * uy - cy * ux), bx * uy - by * ux, r * length),
            _range_interval(-(cx * ux + cy * uy), bx * ux + by * uy, 0.0, len2),
        )
        if rect is not None:
            parts.append(rect)
    # 两端圆盘：|P(t) − C| ≤ r、|P(t) − D| ≤ r
    for px, py in ((cx, cy), (dx, dy)):
        disk = _disk_interval(bx, by, px, py, r)
        if disk is not None:
            parts.append(disk)
    if not parts:
        return None
    lo = max(min(p[0] for p in parts), 0.0)
    hi = min(max(p[1] for p in parts), 1.0)
    if lo >= hi:
        return None  # 仅相切或擦边：零长度区间不构成"在走廊内"
    return (lo, hi)


def _deviation_params(corridors):
    """走廊参数区间并集在 [0, 1] 上的补集，即该采样线段的偏离参数区间。"""
    union = []
    for lo, hi in sorted(corridors):
        if union and lo <= union[-1][1] + _UNION_EPS_T:
            union[-1][1] = max(union[-1][1], hi)
        else:
            union.append([lo, hi])
    out = []
    cur = 0.0
    for lo, hi in union:
        if lo > cur:
            out.append((cur, lo))
        cur = max(cur, hi)
    if cur < 1.0:
        out.append((cur, 1.0))
    return [(s, e) for s, e in out if e - s > _MIN_PARAM]


def _interp(a, b, t):
    """沿主轨迹按比例 t 线性插值经纬度、海拔与时间。"""
    ele = None
    if a.ele is not None and b.ele is not None:
        ele = a.ele + (b.ele - a.ele) * t
    time = None
    if a.time is not None and b.time is not None:
        time = a.time + (b.time - a.time) * t
    return TrackPoint(
        lat=a.lat + (b.lat - a.lat) * t,
        lon=a.lon + (b.lon - a.lon) * t,
        ele=ele,
        time=time,
    )


def find_deviations(segments: list, ref_segments: list) -> DeviationResult:
    """分析主轨迹相对参考路线的偏离区间。

    segments 为主轨迹 list[list[TrackPoint]]（上传时保存的原始数据），
    ref_segments 为参考路线 list[list[TrackPoint]]（只取折线几何，忽略时间）；
    主轨迹时间不递增时抛出 ValueError，由接口层转为明确提示。
    """
    _check_times(segments)
    result = DeviationResult()
    if not segments or not any(segments):
        return result

    # 局部米制投影原点取主轨迹首点；50 米判定尺度下投影误差可忽略
    first = next(p for seg in segments for p in seg)
    lat0, lon0 = first.lat, first.lon

    # 参考折线：trkseg 内相邻点构成有限参考路段，不同 trkseg 之间不相连
    refs = _RefIndex()
    for seg in ref_segments:
        for c, d in zip(seg, seg[1:]):
            cx, cy = to_local_xy(c.lat, c.lon, lat0, lon0)
            dx, dy = to_local_xy(d.lat, d.lon, lat0, lon0)
            refs.add(cx, cy, dx, dy)

    found = []  # (Deviation, 偏离段折线)
    for seg_idx, seg in enumerate(segments, start=1):
        pieces = []  # (start_epoch, end_epoch, [TrackPoint, ...])，按时间先后
        for a, b in zip(seg, seg[1:]):
            dt = (b.time - a.time).total_seconds()
            if dt > MAX_GAP_S:
                continue  # 断档：不跨断档插值
            ax, ay = to_local_xy(a.lat, a.lon, lat0, lon0)
            bx, by = to_local_xy(b.lat, b.lon, lat0, lon0)
            corridors = []
            for ri in refs.candidates(ax, ay, bx, by):
                cx, cy, dx, dy = refs.segs[ri]
                iv = _capsule_interval(bx - ax, by - ay,
                                       cx - ax, cy - ay, dx - ax, dy - ay,
                                       CORRIDOR_RADIUS_M)
                if iv is not None:
                    corridors.append(iv)
            t0 = a.time.timestamp()
            for u0, u1 in _deviation_params(corridors):
                p0 = a if u0 == 0.0 else _interp(a, b, u0)
                p1 = b if u1 == 1.0 else _interp(a, b, u1)
                pieces.append((t0 + u0 * dt, t0 + u1 * dt, [p0, p1]))

        # 仅合并同一 trkseg 内相接的偏离区间；断档处没有线段，天然断开
        merged = []
        for s, e, pts in pieces:
            if merged and s <= merged[-1][1] + _MERGE_EPS_S:
                merged[-1][1] = max(merged[-1][1], e)
                merged[-1][2].extend(pts[1:])  # 相接点（同一采样点）只保留一次
            else:
                merged.append([s, e, list(pts)])

        for s, e, pts in merged:
            if e - s <= _MIN_DURATION_S:
                continue  # 仅列出正时长偏离
            dist = sum(haversine_m(p, q) for p, q in zip(pts, pts[1:]))
            dev = Deviation(
                seg_index=seg_idx,
                start_time=datetime.fromtimestamp(s, tz=timezone.utc),
                end_time=datetime.fromtimestamp(e, tz=timezone.utc),
                duration_s=e - s,
                distance_m=dist,
                start_lat=pts[0].lat,
                start_lon=pts[0].lon,
                end_lat=pts[-1].lat,
                end_lon=pts[-1].lon,
            )
            found.append((dev, pts))

    found.sort(key=lambda item: item[0].start_time)
    result.deviations = [d for d, _ in found]
    result.deviation_segments = [pts for _, pts in found]
    result.total_duration_s = sum(d.duration_s for d in result.deviations)
    result.total_distance_m = sum(d.distance_m for d in result.deviations)
    return result
