from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from collections.abc import Iterator

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse

log = logging.getLogger("pornscan")

MODEL = os.getenv("PORNSCAN_MODEL", "nudenet")   # nudenet | yolo
DEVICE = os.getenv("PORNSCAN_DEVICE", "cpu")
MAX_BYTES = int(os.getenv("PORNSCAN_MAX_BYTES", str(20 * 1024 * 1024)))
VIDEO_SAMPLE_FRAMES = int(os.getenv("PORNSCAN_VIDEO_FRAMES", "6"))

# classes considered sexually explicit by NudeNet
EXPLICIT_CLASSES = {
    "ANUS_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_BREAST_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "FEMALE_GENITALIA_COVERED",
    "EXPOSED_ANUS",
    "EXPOSED_BREAST_FEMALE",
    "EXPOSED_BREAST_MALE",
    "EXPOSED_BUTTOCKS",
    "EXPOSED_GENITALIA_FEMALE",
    "EXPOSED_GENITALIA_MALE",
    "COVERED_GENITALIA_FEMALE",
    "COVERED_GENITALIA_MALE",
    "MALE_BUTTOCKS",
}

app = FastAPI(title="ABG Guard - local nudity detector", version="1.0.0")

_detector = None


def get_detector():
    global _detector
    if _detector is not None:
        return _detector

    if MODEL == "yolo":
        from ultralytics import YOLO  # type: ignore

        weights = os.getenv("PORNSCAN_YOLO_WEIGHTS", "yolov8n-nsfw.pt")
        _detector = ("yolo", YOLO(weights))
    else:
        from nudenet import NudeDetector  # type: ignore

        _detector = ("nudenet", NudeDetector())
    log.info("model loaded: %s (device=%s)", MODEL, DEVICE)
    return _detector


def _score_from_detections(dets: list[dict]) -> tuple[float, list[str]]:
    labels: list[str] = []
    best = 0.0
    for det in dets:
        cls = str(det.get("class", "")).upper()
        if cls not in EXPLICIT_CLASSES:
            continue
        score = float(det.get("score", 0.0))
        labels.append(cls)
        best = max(best, score)
    return best, sorted(set(labels))


def _suffix(payload: bytes) -> str:
    """sniff the container format - NudeNet/YOLO both need a real file path."""
    if payload[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if payload[:2] == b"\xff\xd8":
        return ".jpg"
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return ".webp"
    if payload[:4] == b"GIF8":
        return ".gif"
    if payload[:4] == b"\x1aE\xdf\xa3":
        return ".webm"
    if len(payload) > 12 and payload[4:12] == b"ftyp":
        return ".mp4"
    return ".img"


@contextlib.contextmanager
def _temp_media(payload: bytes) -> Iterator[str]:
    tmp = tempfile.NamedTemporaryFile(suffix=_suffix(payload), delete=False)
    try:
        tmp.write(payload)
        tmp.close()
        yield tmp.name
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _detect_image(payload: bytes) -> tuple[float, list[str]]:
    kind, model = get_detector()
    # both backends read from a path (`cv2.imread` / ultralytics predictor)
    with _temp_media(payload) as path:
        if kind == "nudenet":
            dets = model.detect(path) or []
            return _score_from_detections(dets)
        results = model.predict(path, verbose=False, device=DEVICE)[0]
    names = results.names
    boxes = []
    for box, cls, conf in zip(
        results.boxes.xyxy, results.boxes.cls, results.boxes.conf, strict=False
    ):
        boxes.append({"class": names[int(cls)], "score": float(conf), "box": box.tolist()})
    return _score_from_detections(boxes)


def _detect_video(payload: bytes, threshold: float) -> tuple[float, list[str]]:
    """Sample a few frames - a single hit is enough to flag the whole clip."""
    try:
        import cv2  # type: ignore
    except ImportError:
        log.warning("opencv not installed - video scanning disabled (fail-open)")
        return 0.0, []

    suffix = _suffix(payload)
    if suffix not in {".mp4", ".webm", ".gif"}:
        suffix = ".mp4"
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tmp.write(payload)
        tmp.close()
        cap = cv2.VideoCapture(tmp.name)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total <= 0:
            return 0.0, []

        step = max(1, total // max(1, VIDEO_SAMPLE_FRAMES))
        best, labels = 0.0, []
        for idx in range(0, total, step)[:VIDEO_SAMPLE_FRAMES]:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, frame = cap.read()
            if not ok:
                continue
            ok2, buf = cv2.imencode(".jpg", frame)
            if not ok2:
                continue
            score, found = _detect_image(buf.tobytes())
            if score > best:
                best = score
            labels.extend(found)
            if best >= threshold:
                break
        cap.release()
        return best, sorted(set(labels))
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


@app.get("/health")
async def health() -> dict:
    try:
        get_detector()
        ready = True
    except Exception as exc:
        log.error("detector failed to load: %s", exc)
        ready = False
    return {"status": "ok" if ready else "degraded", "model": MODEL, "ready": ready}


@app.post("/detect")
async def detect(
    file: UploadFile = File(...),
    threshold: float = Form(0.55),
) -> JSONResponse:
    payload = await file.read()
    if not payload:
        return JSONResponse(
            {"is_porn": False, "score": 0.0, "labels": [], "reason": "empty"}
        )
    if len(payload) > MAX_BYTES:
        return JSONResponse(
            {"is_porn": False, "score": 0.0, "labels": [], "reason": "too_large"}
        )

    content_type = (file.content_type or "").lower()
    try:
        if content_type.startswith("video/") or (file.filename or "").endswith(
            (".mp4", ".mov", ".mkv", ".webm")
        ):
            score, labels = _detect_video(payload, threshold)
        else:
            score, labels = _detect_image(payload)
    except Exception as exc:
        log.exception("scan failed")
        # fail-open so a detector crash never freezes the worker bot
        return JSONResponse(
            {"is_porn": False, "score": 0.0, "labels": [], "reason": str(exc)},
            status_code=200,
        )

    return JSONResponse(
        {
            "is_porn": score >= threshold or bool(set(labels) & EXPLICIT_CLASSES),
            "score": round(float(score), 4),
            "labels": labels,
        }
    )


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        "main:app",
        host=os.getenv("PORNSCAN_HOST", "127.0.0.1"),
        port=int(os.getenv("PORNSCAN_PORT", "8010")),
        log_level="info",
        workers=1,
    )
