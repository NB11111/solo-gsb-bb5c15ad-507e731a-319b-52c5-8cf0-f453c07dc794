"""检查点到访分析：找出轨迹进入各检查点圆内的连续时间区间。

- 基于上传时保存的原始轨迹（各原始 trkseg）计算，不修改原轨迹，
  与遮蔽裁切、配速、海拔、漂移修复、停留识别、轨迹精简互不影响；
- 每个检查点独立分析：在圆心局部米制投影（等距圆柱，复用 geo.py 的
  线段-圆求交）中求每条采样线段落在圆内的参数区间，进出时间按线段比例
  在线段起止时间之间线性插值；两端都在圆外但线段穿圆（判别式 > 0）同样
  识别，相切（判别式 = 0）只是瞬时接触，不计入；
- 同一 trkseg 内相邻点时间必须严格递增，否则抛出 ValueError；
- 相邻采样间隔超过 600 秒视为断档：断档处不构造线段、不插值，
  断档两侧的圆内区间不合并；trkseg 边界同样断开，绝不跨分段合并；
- 同一检查点的连续（相接或重叠）圆内区间合并为一次到访，仅列出正时长到访
  （相切只产生零时长区间，浮点噪声以 1 毫秒容差过滤）；
- 到访起止落在采样点上（该点在圆内）时直接使用该连续记录的端点原始时间，
  不做外推。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .geo import MaskCircle, segment_circle_crossings, to_local_xy

MAX_CHECKPOINTS = 10      # 检查点数量上限
MIN_RADIUS_M = 1.0        # 检查点半径下限（米）
MAX_RADIUS_M = 1000.0     # 检查点半径上限（米）
DEFAULT_RADIUS_M = 50.0   # 默认检查点半径（米）
MAX_GAP_S = 600.0         # 断档阈值（秒）：相邻采样间隔超过 10 分钟不插值、不合并

_MERGE_EPS_S = 1e-3       # 区间相接判定的浮点容差（秒）
_MIN_DURATION_S = 1e-3    # 正时长判定的浮点容差（秒）：相切只产生零时长（浮点噪声级）区间


@dataclass
class Checkpoint:
    """一个圆形检查点。"""

    lat: float
    lon: float
    radius_m: float = DEFAULT_RADIUS_M
    name: str = ""


@dataclass
class Visit:
    """一次到访：同一 trkseg 内连续圆内区间合并后的结果。"""

    seg_index: int        # 原始 trkseg 序号（从 1 开始）
    start_time: datetime  # 进入时间（穿越圆边界时为按线段比例插值的时刻）
    end_time: datetime    # 离开时间
    duration_s: float     # 停留秒数（end_time - start_time，恒为正）


@dataclass
class CheckpointResult:
    """一个检查点的到访分析结果。"""

    index: int            # 检查点序号（从 1 开始，与请求顺序一致）
    name: str
    lat: float
    lon: float
    radius_m: float
    visits: list = field(default_factory=list)  # list[Visit]，按时间先后排列
    total_duration_s: float = 0.0               # 各次到访停留秒数之和


def _check_times(segments: list) -> None:
    """校验每个 trkseg 内相邻时间严格递增；零时差或倒序时抛出 ValueError。"""
    for seg_idx, seg in enumerate(segments, start=1):
        for i, (a, b) in enumerate(zip(seg, seg[1:]), start=1):
            if b.time <= a.time:
                raise ValueError(
                    f"第 {seg_idx} 个 trkseg 中第 {i}→{i + 1} 个轨迹点的时间未递增"
                    "（零时差或倒序），无法进行检查点到访分析"
                )


def _inside_intervals(seg: list, circle: MaskCircle) -> list:
    """一个 trkseg 内轨迹落在圆内的连续时间区间（epoch 秒），按时间先后排列。

    每条采样线段在圆心局部投影中求与圆周的交点，把线段切成若干子区间，
    以子区间中点判定圆内/圆外；圆内子区间的起止时刻按线段比例在线段
    起止时间之间线性插值（子区间端点为采样点时即为该点原始时间）。
    相邻采样间隔超过 600 秒的断档处不构造线段，区间在断档处天然断开。
    """
    intervals = []
    r2 = circle.radius_m * circle.radius_m
    for a, b in zip(seg, seg[1:]):
        dt = (b.time - a.time).total_seconds()
        if dt > MAX_GAP_S:
            continue  # 断档：不跨断档插值
        ax, ay = to_local_xy(a.lat, a.lon, circle.lat, circle.lon)
        bx, by = to_local_xy(b.lat, b.lon, circle.lat, circle.lon)
        ts = segment_circle_crossings(a, b, circle)
        bounds = [0.0] + sorted(ts) + [1.0]
        start_epoch = a.time.timestamp()
        for u0, u1 in zip(bounds, bounds[1:]):
            um = (u0 + u1) * 0.5  # 投影对线段参数是线性的，直接插值投影坐标
            mx = ax + (bx - ax) * um
            my = ay + (by - ay) * um
            if mx * mx + my * my < r2:
                intervals.append((start_epoch + u0 * dt, start_epoch + u1 * dt))
    return intervals


def _merge_intervals(intervals: list) -> list:
    """合并相接（或重叠）的连续圆内区间；输入已按时间先后排列。

    只在同一 trkseg 内合并：断档（> 600 秒）两侧的区间时间间隔远大于
    相接容差，天然不会被合并。
    """
    merged: list = []
    for s, e in intervals:
        if merged and s <= merged[-1][1] + _MERGE_EPS_S:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return merged


def find_checkpoint_visits(segments: list, checkpoints: list) -> list:
    """分析各检查点的到访记录。

    segments 为原始轨迹 list[list[TrackPoint]]，checkpoints 为 list[Checkpoint]；
    返回 list[CheckpointResult]，与检查点一一对应。每个检查点独立分析，
    到访只在同一 trkseg 内合并；时间不递增时抛出 ValueError，由接口层转为提示。
    """
    _check_times(segments)
    results = []
    for idx, cp in enumerate(checkpoints, start=1):
        circle = MaskCircle(lat=cp.lat, lon=cp.lon, radius_m=cp.radius_m)
        visits = []
        for seg_idx, seg in enumerate(segments, start=1):
            for s, e in _merge_intervals(_inside_intervals(seg, circle)):
                if e - s <= _MIN_DURATION_S:
                    continue  # 仅列出正时长到访（相切等零时长区间不计）
                visits.append(Visit(
                    seg_index=seg_idx,
                    start_time=datetime.fromtimestamp(s, tz=timezone.utc),
                    end_time=datetime.fromtimestamp(e, tz=timezone.utc),
                    duration_s=e - s,
                ))
        visits.sort(key=lambda v: v.start_time)
        results.append(CheckpointResult(
            index=idx,
            name=cp.name,
            lat=cp.lat,
            lon=cp.lon,
            radius_m=cp.radius_m,
            visits=visits,
            total_duration_s=sum(v.duration_s for v in visits),
        ))
    return results
