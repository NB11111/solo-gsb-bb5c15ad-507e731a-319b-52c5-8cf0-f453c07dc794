"""双人轨迹相遇分析：找出两条轨迹中两人距离不超过 50 米的连续时间区间。

- 两条轨迹各自按原始 trkseg 的相邻点构造带时间的线段，线段内位置按时间
  线性插值；同一 trkseg 内相邻点时间必须严格递增，否则抛出 ValueError；
- 相邻采样间隔超过 10 分钟视为断档：断档处不构造线段，不跨断档插值；
  trkseg 边界同样不连接（每条线段只在其采样间隔内有效）；
- 仅比较时间重叠的线段对：把端点投影到以四端点平均位置为原点的局部米制
  平面（等距圆柱投影），相对位置关于时间是线性的，解 |r(t)|² = 50² 得到
  距离不超过 50 米的候选区间；
- 候选区间按时间排序后合并相接（或重叠）的区间；合并后持续不足 60 秒的
  不列出，不跨断档或 trkseg 虚构相遇。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .geo import EARTH_RADIUS_M

MAX_GAP_S = 600.0     # 断档阈值（秒）：相邻采样间隔超过 10 分钟不构造线段
MEET_RADIUS_M = 50.0  # 相遇距离阈值（米）：两人距离不超过该值视为相遇
MIN_MEET_S = 60.0     # 最短相遇时长（秒）：合并后不足该时长的区间不列出

_MERGE_EPS_S = 1e-3   # 区间相接判定的浮点容差（秒）


@dataclass
class Encounter:
    """一次相遇：两人距离持续不超过 50 米的合并后区间。"""

    start_time: datetime
    end_time: datetime
    duration_s: float
    min_distance_m: float   # 区间内两人的最近距离（米）
    closest_time: datetime  # 最近距离出现的时刻
    lat: float              # 最近时刻两人位置的中点（用于地图标记）
    lon: float


@dataclass
class EncounterResult:
    """相遇分析结果。"""

    encounters: list = field(default_factory=list)  # list[Encounter]，按时间先后排列


@dataclass
class _Segment:
    """一条带时间的采样线段：t0～t1（epoch 秒）内位置从 (lat0, lon0) 线性变化到 (lat1, lon1)。"""

    t0: float
    t1: float
    lat0: float
    lon0: float
    lat1: float
    lon1: float


@dataclass
class _Hit:
    """一对线段的时间重叠部分中，距离 ≤ 50 米的候选区间（epoch 秒）。"""

    start_s: float
    end_s: float
    min_dist_m: float
    min_t_s: float   # 最近距离出现的时刻
    lat: float       # 最近时刻两人位置的中点
    lon: float


def _build_segments(segments: list, label: str) -> list:
    """把一条轨迹各 trkseg 的相邻点构造为带时间线段。

    时间必须严格递增（零时差或倒序抛 ValueError）；相邻采样间隔超过
    10 分钟视为断档，断档处不构造线段。
    """
    out = []
    for seg_idx, seg in enumerate(segments, start=1):
        for i, (a, b) in enumerate(zip(seg, seg[1:]), start=1):
            dt = (b.time - a.time).total_seconds()
            if dt <= 0:
                raise ValueError(
                    f"{label}第 {seg_idx} 个 trkseg 中第 {i}→{i + 1} 个轨迹点的时间未递增"
                    "（零时差或倒序），无法进行相遇分析"
                )
            if dt > MAX_GAP_S:
                continue  # 断档：不跨断档插值
            out.append(_Segment(
                t0=a.time.timestamp(), t1=b.time.timestamp(),
                lat0=a.lat, lon0=a.lon, lat1=b.lat, lon1=b.lon,
            ))
    return out


def _pos_at(seg: _Segment, t: float) -> tuple:
    """线段在时刻 t（epoch 秒）的线性插值位置 (lat, lon)。"""
    f = (t - seg.t0) / (seg.t1 - seg.t0)
    return seg.lat0 + (seg.lat1 - seg.lat0) * f, seg.lon0 + (seg.lon1 - seg.lon0) * f


def _overlap_hit(sa: _Segment, sb: _Segment):
    """两条带时间线段的时间重叠部分中，距离 ≤ 50 米的候选区间；无则返回 None。"""
    lo = max(sa.t0, sb.t0)
    hi = min(sa.t1, sb.t1)
    if lo >= hi:
        return None

    # 局部米制平面：原点取两条线段四端点的平均位置；
    # 相遇判定尺度（50 米）下投影误差可忽略
    lat0 = (sa.lat0 + sa.lat1 + sb.lat0 + sb.lat1) / 4.0
    lon0 = (sa.lon0 + sa.lon1 + sb.lon0 + sb.lon1) / 4.0
    ky = math.radians(EARTH_RADIUS_M)          # 每度纬度的米数
    kx = ky * math.cos(math.radians(lat0))     # 每度经度的米数

    def xy(lat, lon):
        return (lon - lon0) * kx, (lat - lat0) * ky

    # 位置与投影对时间都是线性的 → 相对位置 r(t) = r0 + v·(t − lo)
    ax0, ay0 = xy(*_pos_at(sa, lo))
    bx0, by0 = xy(*_pos_at(sb, lo))
    ax1, ay1 = xy(*_pos_at(sa, hi))
    bx1, by1 = xy(*_pos_at(sb, hi))
    span = hi - lo
    rx, ry = ax0 - bx0, ay0 - by0
    vx = ((ax1 - bx1) - rx) / span
    vy = ((ay1 - by1) - ry) / span

    # 解 |r0 + v·s|² ≤ R²（s ∈ [0, span]）：开口向上的二次不等式
    aa = vx * vx + vy * vy
    bb = 2.0 * (rx * vx + ry * vy)
    cc = rx * rx + ry * ry - MEET_RADIUS_M * MEET_RADIUS_M
    if aa <= 0.0:
        # 相对位置不变：整段重叠要么都在圈内，要么都在圈外
        if cc > 0.0:
            return None
        s_lo, s_hi = 0.0, span
    else:
        disc = bb * bb - 4.0 * aa * cc
        if disc <= 0.0:
            return None  # 无交点或相切（相切只是瞬时接触，不构成区间）
        sq = math.sqrt(disc)
        s_lo = max((-bb - sq) / (2.0 * aa), 0.0)
        s_hi = min((-bb + sq) / (2.0 * aa), span)
        if s_lo >= s_hi:
            return None

    # 区间内最近距离：相对速度顶点（若有）截断到 [s_lo, s_hi]
    if aa > 0.0:
        s_star = min(max(-bb / (2.0 * aa), s_lo), s_hi)
    else:
        s_star = 0.5 * (s_lo + s_hi)
    min_dist = math.hypot(rx + vx * s_star, ry + vy * s_star)
    t_star = lo + s_star
    lat_a, lon_a = _pos_at(sa, t_star)
    lat_b, lon_b = _pos_at(sb, t_star)
    return _Hit(
        start_s=lo + s_lo,
        end_s=lo + s_hi,
        min_dist_m=min_dist,
        min_t_s=t_star,
        lat=(lat_a + lat_b) / 2.0,
        lon=(lon_a + lon_b) / 2.0,
    )


def find_encounters(segments_a: list, segments_b: list) -> EncounterResult:
    """分析两条轨迹（各为 list[list[TrackPoint]]）的相遇区间。

    segments_a 为当前轨迹，segments_b 为对比轨迹；任一轨迹时间未递增时
    抛出 ValueError，由接口层转为明确提示。
    """
    segs_a = _build_segments(segments_a, "当前轨迹")
    segs_b = _build_segments(segments_b, "对比轨迹")

    # 按开始时间排序后扫描：sb.t0 超过 sa 结束时间即可停止内层循环
    segs_b.sort(key=lambda s: s.t0)
    hits = []
    for sa in segs_a:
        for sb in segs_b:
            if sb.t0 >= sa.t1:
                break
            if sb.t1 <= sa.t0:
                continue  # 时间不相交
            hit = _overlap_hit(sa, sb)
            if hit is not None:
                hits.append(hit)
    hits.sort(key=lambda h: (h.start_s, h.end_s))

    # 合并相接（或重叠）的候选区间；最近距离取各子区间之最
    merged: list = []
    for h in hits:
        if merged and h.start_s <= merged[-1].end_s + _MERGE_EPS_S:
            cur = merged[-1]
            cur.end_s = max(cur.end_s, h.end_s)
            if h.min_dist_m < cur.min_dist_m:
                cur.min_dist_m = h.min_dist_m
                cur.min_t_s = h.min_t_s
                cur.lat = h.lat
                cur.lon = h.lon
        else:
            merged.append(_Hit(**vars(h)))

    def to_dt(t):
        return datetime.fromtimestamp(t, tz=timezone.utc)

    encounters = [
        Encounter(
            start_time=to_dt(m.start_s),
            end_time=to_dt(m.end_s),
            duration_s=m.end_s - m.start_s,
            min_distance_m=m.min_dist_m,
            closest_time=to_dt(m.min_t_s),
            lat=m.lat,
            lon=m.lon,
        )
        for m in merged
        if m.end_s - m.start_s >= MIN_MEET_S  # 持续不足 60 秒的不列出
    ]
    return EncounterResult(encounters=encounters)
