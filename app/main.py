"""FastAPI 入口：上传 GPX、遮蔽计算、导出、分段配速、海拔分析、漂移修复、停留识别、轨迹精简、双人轨迹相遇、检查点到访、参考路线偏离、静态页面。"""
from __future__ import annotations

import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .checkpoint import (
    DEFAULT_RADIUS_M as CP_DEFAULT_RADIUS_M,
    MAX_CHECKPOINTS,
    MAX_GAP_S as CP_MAX_GAP_S,
    MAX_RADIUS_M as CP_MAX_RADIUS_M,
    MIN_RADIUS_M as CP_MIN_RADIUS_M,
    Checkpoint,
    find_checkpoint_visits,
)
from .deviation import CORRIDOR_M, MAX_GAP_S as DEV_MAX_GAP_S, find_deviations
from .drift import MAX_SPEED_MPS, MIN_OFFSET_M, fix_drift
from .elevation import RESAMPLE_STEP_M, REVERSAL_THRESHOLD_M, elevation_profile
from .encounter import MAX_GAP_S as ENCOUNTER_MAX_GAP_S
from .encounter import MEET_RADIUS_M, MIN_MEET_S, find_encounters
from .geo import MaskCircle, haversine_m, mask_track
from .gpxutil import build_gpx, parse_gpx
from .pace import DEFAULT_SPLIT_KM, MAX_SPLIT_KM, MIN_SPLIT_KM, pace_splits
from .simplify import (
    DEFAULT_TOLERANCE_M,
    MAX_TOLERANCE_M,
    MIN_TOLERANCE_M,
    simplify_track,
)
from .stay import MAX_GAP_S, MIN_STAY_S, STAY_RADIUS_M, detect_stays
from .steep import (
    GRADE_THRESHOLD,
    MIN_SECTION_M,
    WINDOW_M,
    WINDOW_STEP_M,
    find_steep_sections,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB

app = FastAPI(title="徒步轨迹分享 · 敏感地点遮蔽")

# 内存存储（演示用途，重启即清空）：track_id -> {"segments": ..., "name": ...}
TRACKS: dict = {}


class CircleIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_m: float = Field(gt=0, le=100_000)


class MaskRequest(BaseModel):
    track_id: str
    circles: list[CircleIn] = []


class PaceRequest(BaseModel):
    track_id: str
    split_km: float = Field(default=DEFAULT_SPLIT_KM, ge=MIN_SPLIT_KM, le=MAX_SPLIT_KM)


class ElevationRequest(BaseModel):
    track_id: str


class SteepRequest(BaseModel):
    track_id: str


class DriftRequest(BaseModel):
    track_id: str


class StayRequest(BaseModel):
    track_id: str


class SimplifyRequest(BaseModel):
    track_id: str
    tolerance_m: float = Field(
        default=DEFAULT_TOLERANCE_M, ge=MIN_TOLERANCE_M, le=MAX_TOLERANCE_M
    )


class EncounterRequest(BaseModel):
    track_id: str
    other_track_id: str


class CheckpointIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_m: float = Field(
        default=CP_DEFAULT_RADIUS_M, ge=CP_MIN_RADIUS_M, le=CP_MAX_RADIUS_M
    )
    name: str = Field(default="", max_length=50)


class CheckpointVisitsRequest(BaseModel):
    track_id: str
    checkpoints: list[CheckpointIn] = Field(default=[], max_length=MAX_CHECKPOINTS)


class DeviationRequest(BaseModel):
    track_id: str
    reference_track_id: str


def _point_dict(p):
    return {
        "lat": p.lat,
        "lon": p.lon,
        "ele": p.ele,
        "time": p.time.isoformat() if p.time else None,
    }


def _get_track(track_id: str):
    track = TRACKS.get(track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="轨迹不存在或已过期，请重新上传")
    return track


def _run_mask(track, circles_in):
    circles = [MaskCircle(lat=c.lat, lon=c.lon, radius_m=c.radius_m) for c in circles_in]
    return mask_track(track["segments"], circles)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/upload")
async def upload(file: UploadFile):
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="文件超过 50 MB 限制")
    try:
        segments, name = parse_gpx(content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    track_id = uuid.uuid4().hex[:12]
    TRACKS[track_id] = {"segments": segments, "name": name}

    distance = sum(
        haversine_m(a, b) for seg in segments for a, b in zip(seg, seg[1:])
    )
    times = [p.time for seg in segments for p in seg if p.time]
    return {
        "track_id": track_id,
        "name": name,
        "segments": [[_point_dict(p) for p in seg] for seg in segments],
        "stats": {
            "points": sum(len(s) for s in segments),
            "segments": len(segments),
            "distance_m": distance,
            "start_time": min(times).isoformat() if times else None,
            "end_time": max(times).isoformat() if times else None,
        },
    }


@app.post("/api/mask")
def mask(req: MaskRequest):
    track = _get_track(req.track_id)
    result = _run_mask(track, req.circles)
    return {
        "visible_segments": [[_point_dict(p) for p in seg] for seg in result.visible_segments],
        "hidden_segments": [[_point_dict(p) for p in seg] for seg in result.hidden_segments],
        "hidden_intervals": result.hidden_intervals,
        "distance_before_m": result.distance_before_m,
        "distance_after_m": result.distance_after_m,
        "visible_points": sum(len(s) for s in result.visible_segments),
    }


@app.post("/api/export")
def export(req: MaskRequest):
    track = _get_track(req.track_id)
    result = _run_mask(track, req.circles)
    name = f"{track['name']}-masked"
    gpx_bytes = build_gpx(result.visible_segments, name)
    filename = quote(f"{name}.gpx")
    return Response(
        content=gpx_bytes,
        media_type="application/gpx+xml",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


def _split_dict(s):
    return {
        "seg_index": s.seg_index,
        "split_index": s.split_index,
        "distance_m": s.distance_m,
        "duration_s": s.duration_s,
        "pace_min_per_km": s.pace_min_per_km,
        "start_time": s.start_time.isoformat(),
        "end_time": s.end_time.isoformat(),
    }


@app.post("/api/pace")
def pace(req: PaceRequest):
    """分段配速：按 split_km 公里把每个原始 trkseg 切段，返回每段距离/耗时/配速/起止时间。"""
    track = _get_track(req.track_id)
    try:
        per_segment = pace_splits(track["segments"], req.split_km)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    segments_out = [
        {
            "seg_index": seg_idx,
            "distance_m": sum(s.distance_m for s in splits),
            "splits": [_split_dict(s) for s in splits],
        }
        for seg_idx, splits in enumerate(per_segment, start=1)
    ]
    total_distance = sum(g["distance_m"] for g in segments_out)
    total_duration = sum(s.duration_s for splits in per_segment for s in splits)
    return {
        "split_km": req.split_km,
        "segments": segments_out,
        "totals": {
            "distance_m": total_distance,
            "duration_s": total_duration,
            "pace_min_per_km": (
                total_duration / 60.0 / (total_distance / 1000.0)
                if total_distance > 0 else None
            ),
        },
    }


@app.post("/api/elevation")
def elevation(req: ElevationRequest):
    """海拔剖面与累计爬升：基于上传时保存的原始轨迹计算，与遮蔽裁切无关。"""
    track = _get_track(req.track_id)
    result = elevation_profile(track["segments"])
    return {
        "has_ele": result.has_ele,
        "sample_step_m": RESAMPLE_STEP_M,
        "reversal_threshold_m": REVERSAL_THRESHOLD_M,
        "distance_m": result.distance_m,
        "ascent_m": result.ascent_m,
        "descent_m": result.descent_m,
        "ele_min": result.ele_min,
        "ele_max": result.ele_max,
        "runs": [
            {
                "seg_index": r.seg_index,
                "start_m": r.start_m,
                "points": [{"d": d, "ele": e} for d, e in r.points],
            }
            for r in result.runs
        ],
    }


@app.post("/api/steep-sections")
def steep_sections(req: SteepRequest):
    """持续陡坡分析：复用海拔剖面的平滑重采样序列，100 米窗口、20 米前移，
    上坡 ≥ 8% 或下坡 ≤ −8% 标记窗口中心 20 米区间，相邻同向合并后
    总长 ≥ 100 米才列出；不跨原始 trkseg 边界或缺失海拔缺口。"""
    track = _get_track(req.track_id)
    result = find_steep_sections(track["segments"])
    return {
        "has_ele": result.has_ele,
        "distance_m": result.distance_m,
        "sections": [
            {
                "seg_index": s.seg_index,
                "start_m": s.start_m,
                "end_m": s.end_m,
                "length_m": s.end_m - s.start_m,
                "direction": s.direction,
                "max_grade_pct": s.max_grade_pct,
                "window_count": s.window_count,
            }
            for s in result.sections
        ],
        "thresholds": {
            "window_m": WINDOW_M,
            "window_step_m": WINDOW_STEP_M,
            "grade_threshold_pct": GRADE_THRESHOLD * 100.0,
            "min_section_m": MIN_SECTION_M,
        },
    }


def _hit_dict(h):
    return {
        "seg_index": h.seg_index,
        "point_index": h.point_index,
        "lat": h.lat,
        "lon": h.lon,
        "time": h.time.isoformat() if h.time else None,
        "speed_in_mps": h.speed_in_mps,
        "speed_out_mps": h.speed_out_mps,
        "speed_bypass_mps": h.speed_bypass_mps,
        "offset_m": h.offset_m,
    }


def _run_drift(track):
    """对原始轨迹执行漂移修复；时间不递增时转为 400 明确提示。"""
    try:
        return fix_drift(track["segments"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/drift")
def drift(req: DriftRequest):
    """单点漂移检测与修复：基于上传时保存的原始轨迹计算，不影响遮蔽与配速。"""
    track = _get_track(req.track_id)
    result = _run_drift(track)
    return {
        "hit_count": len(result.hits),
        "hits": [_hit_dict(h) for h in result.hits],
        "distance_before_m": result.distance_before_m,
        "distance_after_m": result.distance_after_m,
        "points_before": sum(len(s) for s in track["segments"]),
        "points_after": sum(len(s) for s in result.fixed_segments),
        "thresholds": {
            "max_speed_mps": MAX_SPEED_MPS,
            "min_offset_m": MIN_OFFSET_M,
        },
    }


@app.post("/api/drift/export")
def drift_export(req: DriftRequest):
    """导出修复后的 GPX：删除全部命中跳点，保留原始 trkseg 结构与首尾点。"""
    track = _get_track(req.track_id)
    result = _run_drift(track)
    name = f"{track['name']}-fixed"
    gpx_bytes = build_gpx(result.fixed_segments, name)
    filename = quote(f"{name}.gpx")
    return Response(
        content=gpx_bytes,
        media_type="application/gpx+xml",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


def _stay_dict(s):
    return {
        "seg_index": s.seg_index,
        "start_index": s.start_index,
        "end_index": s.end_index,
        "lat": s.lat,
        "lon": s.lon,
        "start_time": s.start_time.isoformat(),
        "end_time": s.end_time.isoformat(),
        "duration_s": s.duration_s,
        "points": s.points,
    }


@app.post("/api/stays")
def stays(req: StayRequest):
    """停留识别与记录/停留/移动时长：基于上传时保存的原始轨迹计算，与遮蔽裁切无关。"""
    track = _get_track(req.track_id)
    try:
        result = detect_stays(track["segments"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "stays": [_stay_dict(s) for s in result.stays],
        "stay_total_s": result.stay_total_s,
        "recording_s": result.recording_s,
        "moving_s": result.moving_s,
        "thresholds": {
            "radius_m": STAY_RADIUS_M,
            "min_stay_s": MIN_STAY_S,
            "max_gap_s": MAX_GAP_S,
        },
    }


def _run_simplify(track, tolerance_m):
    """对原始轨迹执行 Douglas-Peucker 精简；阈值越界时转为 400 明确提示。"""
    try:
        return simplify_track(track["segments"], tolerance_m)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/simplify")
def simplify(req: SimplifyRequest):
    """轨迹精简：基于上传时保存的原始轨迹计算，不修改原轨迹，与其余统计互不影响。"""
    track = _get_track(req.track_id)
    result = _run_simplify(track, req.tolerance_m)
    return {
        "tolerance_m": req.tolerance_m,
        "segments": [[_point_dict(p) for p in seg] for seg in result.simplified_segments],
        "points_before": result.points_before,
        "points_after": result.points_after,
        "distance_before_m": result.distance_before_m,
        "distance_after_m": result.distance_after_m,
    }


@app.post("/api/simplify/export")
def simplify_export(req: SimplifyRequest):
    """导出精简后的 GPX：与页面预览使用同一算法与参数，结果一致；保留原始 trkseg 结构。"""
    track = _get_track(req.track_id)
    result = _run_simplify(track, req.tolerance_m)
    name = f"{track['name']}-simplified"
    gpx_bytes = build_gpx(result.simplified_segments, name)
    filename = quote(f"{name}.gpx")
    return Response(
        content=gpx_bytes,
        media_type="application/gpx+xml",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


def _encounter_dict(e):
    return {
        "start_time": e.start_time.isoformat(),
        "end_time": e.end_time.isoformat(),
        "duration_s": e.duration_s,
        "min_distance_m": e.min_distance_m,
        "closest_time": e.closest_time.isoformat(),
        "lat": e.lat,
        "lon": e.lon,
    }


@app.post("/api/encounters")
def encounters(req: EncounterRequest):
    """双人轨迹相遇分析：当前轨迹与对比轨迹（均来自 /api/upload）时间重叠、
    距离 ≤ 50 米且合并后持续 ≥ 60 秒的区间；基于上传时保存的原始轨迹计算。"""
    track = _get_track(req.track_id)
    other = _get_track(req.other_track_id)
    try:
        result = find_encounters(track["segments"], other["segments"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "encounters": [_encounter_dict(e) for e in result.encounters],
        "thresholds": {
            "radius_m": MEET_RADIUS_M,
            "min_meet_s": MIN_MEET_S,
            "max_gap_s": ENCOUNTER_MAX_GAP_S,
        },
    }


def _visit_dict(v):
    return {
        "seg_index": v.seg_index,
        "start_time": v.start_time.isoformat(),
        "end_time": v.end_time.isoformat(),
        "duration_s": v.duration_s,
    }


@app.post("/api/checkpoint-visits")
def checkpoint_visits(req: CheckpointVisitsRequest):
    """检查点到访分析：基于上传时保存的原始轨迹计算（不修改原轨迹），
    每个检查点独立合并连续圆内区间，仅列出正时长到访；不跨 trkseg 或
    超过 600 秒的采样间隔插值、合并。"""
    track = _get_track(req.track_id)
    checkpoints = [
        Checkpoint(lat=c.lat, lon=c.lon, radius_m=c.radius_m, name=c.name.strip())
        for c in req.checkpoints
    ]
    try:
        results = find_checkpoint_visits(track["segments"], checkpoints)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "checkpoints": [
            {
                "index": r.index,
                "name": r.name,
                "lat": r.lat,
                "lon": r.lon,
                "radius_m": r.radius_m,
                "visit_count": len(r.visits),
                "total_duration_s": r.total_duration_s,
                "visits": [_visit_dict(v) for v in r.visits],
            }
            for r in results
        ],
        "thresholds": {
            "max_checkpoints": MAX_CHECKPOINTS,
            "min_radius_m": CP_MIN_RADIUS_M,
            "max_radius_m": CP_MAX_RADIUS_M,
            "default_radius_m": CP_DEFAULT_RADIUS_M,
            "max_gap_s": CP_MAX_GAP_S,
        },
    }


def _deviation_dict(d):
    return {
        "seg_index": d.seg_index,
        "start_time": d.start_time.isoformat(),
        "end_time": d.end_time.isoformat(),
        "duration_s": d.duration_s,
        "start_lat": d.start_lat,
        "start_lon": d.start_lon,
        "end_lat": d.end_lat,
        "end_lon": d.end_lon,
        "path": [{"lat": p.lat, "lon": p.lon} for p in d.path],
    }


@app.post("/api/deviations")
def deviations(req: DeviationRequest):
    """参考路线偏离分析：参考路线复用第二条上传的 GPX（只取其原始 trkseg 折线，
    忽略时间，不同 trkseg 不相连），主轨迹使用上传时保存的原始数据；主轨迹到任一
    有限参考路段的最短距离 ≤ 50 米算在走廊内，逐条采样线段求走廊内参数区间的并集，
    补集即偏离区间；进出时刻与位置沿主轨迹线性插值；相邻采样超过 600 秒或跨
    trkseg 断开，仅合并同段相接的正时长偏离区间。"""
    track = _get_track(req.track_id)
    ref = _get_track(req.reference_track_id)
    try:
        result = find_deviations(track["segments"], ref["segments"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "deviations": [_deviation_dict(d) for d in result.deviations],
        "deviation_total_s": result.deviation_total_s,
        "thresholds": {
            "corridor_m": CORRIDOR_M,
            "max_gap_s": DEV_MAX_GAP_S,
        },
    }


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
