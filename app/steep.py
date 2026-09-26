"""持续陡坡分析。

- 复用原始轨迹的平滑海拔剖面：与海拔剖面完全相同的连续有效海拔区间切分、
  20 米重采样与三点中位数滤波（直接消费 elevation_profile 的结果）；
- 每个连续有效海拔区间内，以完整 100 米窗口的首末高差计算坡度
  （坡度 = 高差 / 100 米 × 100%），窗口从区间起点每 20 米前移一次，
  只使用首尾均落在 20 米重采样网格上的完整窗口，末端不足 100 米不补；
- 上坡坡度 ≥ 8%（下坡 ≤ −8%）时，标记该窗口中心对应的 20 米里程区间
  （窗口中心 ±10 米，边界恰在重采样网格上）；
- 相邻且同向的标记合并为一个陡坡区间，合并后总长不足 100 米的不列出；
- 原始 trkseg 边界与缺失海拔处在海拔区间切分时即断开，绝不跨边界或缺口合并；
- 全程无任何有效海拔时 has_ele=False、sections 为空（页面显示"无海拔数据"）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .elevation import RESAMPLE_STEP_M, elevation_profile

WINDOW_M = 100.0            # 坡度窗口长度（水平里程，米）
WINDOW_STEP_M = RESAMPLE_STEP_M  # 窗口前移步长 = 重采样间隔（20 米）
GRADE_THRESHOLD = 0.08      # 陡坡坡度阈值：上坡 ≥ 8%，下坡 ≤ −8%
MARK_LEN_M = 20.0           # 单个标记对应的里程区间长度（窗口中心 ±10 米）
MIN_SECTION_M = 100.0       # 合并后陡坡区间的最小总长，恰为 100 米也列出
_EPS_M = 1e-6               # 里程判定的浮点噪声阈值（米）
_EPS_GRADE = 1e-9           # 坡度比值判定的浮点噪声阈值（约百万分之一米高差）
_SPAN = int(round(WINDOW_M / WINDOW_STEP_M))  # 完整窗口跨越的采样间隔数（100/20=5）


@dataclass
class SteepSection:
    """一个合并后的持续陡坡区间（不跨原始 trkseg 边界或缺失海拔缺口）。"""

    seg_index: int            # 原始 trkseg 序号（从 1 开始）
    start_m: float            # 起点全程里程（米）
    end_m: float              # 止点全程里程（米）
    direction: str            # "up" 上坡 / "down" 下坡
    max_grade_pct: float      # 区间内有符号的最大绝对坡度（%，下坡为负）
    window_count: int         # 合并的窗口标记数（每个对应 20 米）


@dataclass
class SteepResult:
    """持续陡坡分析结果；无任何有效海拔时 has_ele=False、sections 为空。"""

    has_ele: bool
    distance_m: float
    sections: list = field(default_factory=list)  # list[SteepSection]


def find_steep_sections(segments: list) -> SteepResult:
    """对整条轨迹（若干原始 trkseg）计算持续陡坡区间。

    直接复用 elevation_profile 的平滑海拔剖面：剖面的每个 run 即一个连续有效
    海拔区间，run.points 为 20 米重采样 + 三点中位数后的 (全程里程, 海拔)。
    """
    profile = elevation_profile(segments)
    if not profile.has_ele:
        return SteepResult(has_ele=False, distance_m=profile.distance_m, sections=[])

    sections: list = []
    for run in profile.runs:
        pts = run.points
        n = len(pts)

        # 逐个完整 100 米窗口判定：第 s 与 s+_SPAN 个采样点相距恰为 5×20=100 米。
        # 末端若非整 20 米采样点，最后一个点参与的窗口跨度不足 100 米，跳过。
        marks = []  # [(标记起点里程, 止点里程, 坡度比值, 方向), ...]
        for s in range(0, n - _SPAN):
            d0, e0 = pts[s]
            d1, e1 = pts[s + _SPAN]
            if abs((d1 - d0) - WINDOW_M) > _EPS_M:
                continue  # 非完整 100 米网格窗口
            grade = (e1 - e0) / WINDOW_M
            if grade >= GRADE_THRESHOLD - _EPS_GRADE:
                direction = "up"
            elif grade <= -GRADE_THRESHOLD + _EPS_GRADE:
                direction = "down"
            else:
                continue
            center = (d0 + d1) / 2.0
            marks.append((center - MARK_LEN_M / 2.0, center + MARK_LEN_M / 2.0,
                          grade, direction))

        # 相邻且同向的标记合并（标记首尾相接即相邻；run 之间天然不合并）
        current = None
        for mstart, mend, grade, direction in marks:
            if (current is not None and current["direction"] == direction
                    and abs(mstart - current["end_m"]) <= _EPS_M):
                current["end_m"] = mend
                current["window_count"] += 1
                if abs(grade) > abs(current["max_grade"]):
                    current["max_grade"] = grade
            else:
                if current is not None:
                    _append_if_long_enough(sections, run.seg_index, current)
                current = {
                    "start_m": mstart, "end_m": mend,
                    "max_grade": grade, "window_count": 1,
                    "direction": direction,
                }
        if current is not None:
            _append_if_long_enough(sections, run.seg_index, current)

    return SteepResult(
        has_ele=True, distance_m=profile.distance_m, sections=sections,
    )


def _append_if_long_enough(sections: list, seg_index: int, cur: dict) -> None:
    """合并后总长至少 100 米（恰好 100 米列出）才输出起止里程、方向与最大坡度。"""
    length = cur["end_m"] - cur["start_m"]
    if length >= MIN_SECTION_M - _EPS_M:
        sections.append(SteepSection(
            seg_index=seg_index,
            start_m=cur["start_m"],
            end_m=cur["end_m"],
            direction=cur["direction"],
            max_grade_pct=cur["max_grade"] * 100.0,
            window_count=cur["window_count"],
        ))
