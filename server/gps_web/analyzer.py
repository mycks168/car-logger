"""
カメラ画像解析モジュール。
サーバGPU（CUDA）でYOLO11m推論を行い、前フレームから新規出現した人物・車両のみSlack通知する。

フロー:
  1. upload_photo エンドポイントから enqueue() で画像パスと撮影時刻を受け取る
  2. バックグラウンドスレッドがキューを消費し、YOLO推論
  3. 前フレームの検知結果と IoU 比較し、新規/常駐を分類
  4. 新規物体があればアノテーション済み画像（新規=赤、常駐=グレー）をSlack通知
"""

import logging
import os
import queue
import tempfile
import threading
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# 設定
DETECT_COOLDOWN_SECONDS = int(os.getenv("DETECT_COOLDOWN_SECONDS", "300"))
YOLO_MODEL = os.getenv("YOLO_MODEL", "yolo11m.pt")
YOLO_CONF = float(os.getenv("YOLO_CONF", "0.5"))
NEW_OBJECT_IOU_THRESHOLD = float(os.getenv("NEW_OBJECT_IOU_THRESHOLD", "0.5"))
# 何フレーム連続で0件なら「本当に何もなくなった」とみなすか
EMPTY_TOLERANCE = int(os.getenv("EMPTY_TOLERANCE", "2"))

# COCOクラスID → 日本語ラベル（通知対象のみ）
_DETECT_CLASSES = {0: "人物", 2: "車両", 3: "バイク", 5: "バス", 7: "トラック"}

# 車両系クラス（YOLOの判定ゆれで car↔truck が変わっても同一物体として扱う）
_VEHICLE_CLASSES = {2, 3, 5, 7}

# 新規物体: 赤、常駐物体: グレー（PIL用RGB）
_COLOR_NEW = (255, 0, 0)
_COLOR_RESIDENT = (160, 160, 160)

# 日本語フォント（NotoSansCJK）
_FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
_FONT_SIZE = 18
try:
    _font = ImageFont.truetype(_FONT_PATH, _FONT_SIZE)
except Exception:
    logger.warning("日本語フォントの読み込みに失敗しました。ラベルが文字化けする可能性があります")
    _font = ImageFont.load_default()

# キューの要素は (画像パス, 撮影時刻ISO文字列) のタプル
_analysis_queue: queue.Queue[tuple[Path, str]] = queue.Queue()

_PrevBox = tuple[float, float, float, float, int]


def _load_model():
    """YOLO11mモデルをロードする。CUDAが使えれば自動的にGPUを使用する。"""
    import torch
    from ultralytics import YOLO

    device = "cuda" if torch.cuda.is_available() else "cpu"
    logger.info("YOLOモデルをロード中: %s (device=%s)", YOLO_MODEL, device)
    model = YOLO(YOLO_MODEL)
    model.to(device)
    logger.info("YOLOモデルのロード完了")
    return model


def _iou(a: _PrevBox, b: _PrevBox) -> float:
    """2つのbounding boxのIoU（Intersection over Union）を返す。"""
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter == 0.0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (area_a + area_b - inter)


def _same_group(cls_a: int, cls_b: int) -> bool:
    """同じカテゴリグループか判定する。車両系（car/truck/bus/バイク）は同一グループ扱い。"""
    if cls_a == cls_b:
        return True
    return cls_a in _VEHICLE_CLASSES and cls_b in _VEHICLE_CLASSES


def _filter_new_objects(
    current: list[_PrevBox],
    prev: list[_PrevBox],
) -> list[_PrevBox]:
    """
    直前フレームに同グループ・同位置（IoU >= 閾値）の物体がなければ新規とみなす。
    車両系クラスはYOLOの判定ゆれ（car↔truck等）を吸収するため同一グループとして扱う。
    """
    new_objects = []
    for box in current:
        is_new = not any(
            _same_group(box[4], prev_box[4]) and _iou(box, prev_box) >= NEW_OBJECT_IOU_THRESHOLD
            for prev_box in prev
        )
        if is_new:
            new_objects.append(box)
    return new_objects


def _annotate_image(photo_path: Path, current_boxes: list[_PrevBox], new_objects: list[_PrevBox]) -> Path:
    """
    PILで日本語ラベル付きbounding boxを描画した画像を一時ファイルに保存して返す。
    新規物体は赤、常駐物体はグレーで描画する。
    """
    img_bgr = cv2.imread(str(photo_path))
    img_pil = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(img_pil)
    new_set = set(new_objects)

    for box in current_boxes:
        x1, y1, x2, y2, cls_id = int(box[0]), int(box[1]), int(box[2]), int(box[3]), box[4]
        label = _DETECT_CLASSES[cls_id]
        is_new = box in new_set
        color = _COLOR_NEW if is_new else _COLOR_RESIDENT
        text = f"新規: {label}" if is_new else f"{label}（常駐）"

        # bounding box
        draw.rectangle([x1, y1, x2, y2], outline=color, width=2)

        # テキスト背景付きラベル
        bbox = draw.textbbox((x1, y1), text, font=_font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.rectangle([x1, y1 - th - 4, x1 + tw + 4, y1], fill=color)
        draw.text((x1 + 2, y1 - th - 2), text, fill=(255, 255, 255), font=_font)

    img_bgr = cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    cv2.imwrite(tmp.name, img_bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return Path(tmp.name)


def _summarize(boxes: list[_PrevBox]) -> str:
    """物体リストを「人物 2件、車両 1件」形式の文字列にまとめる。"""
    counts: dict[str, int] = {}
    for box in boxes:
        label = _DETECT_CLASSES[box[4]]
        counts[label] = counts.get(label, 0) + 1
    return "、".join(f"{label} {n}件" for label, n in counts.items())


def _analyze_worker(webhook_url: str, bot_token: str, channel_id: str) -> None:
    """解析ワーカースレッド本体。"""
    from datetime import datetime, timezone

    from gps_monitor.notify import send_detection

    model = _load_model()
    prev_boxes: list[_PrevBox] = []
    consecutive_empty = 0
    last_notified_at: datetime | None = None

    while True:
        try:
            photo_path, captured_at = _analysis_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        try:
            now = datetime.now(timezone.utc)

            # クールダウン中でも prev_boxes は更新するためスキップしない
            in_cooldown = (
                last_notified_at is not None
                and (now - last_notified_at).total_seconds() < DETECT_COOLDOWN_SECONDS
            )

            # YOLO物体検知
            results = model(str(photo_path), conf=YOLO_CONF, verbose=False)[0]
            current_boxes: list[_PrevBox] = []
            for box in results.boxes:
                cls_id = int(box.cls[0])
                if cls_id in _DETECT_CLASSES:
                    x1, y1, x2, y2 = box.xyxy[0].tolist()
                    current_boxes.append((x1, y1, x2, y2, cls_id))

            if current_boxes:
                consecutive_empty = 0
                if not in_cooldown:
                    new_objects = _filter_new_objects(current_boxes, prev_boxes)
                    resident_objects = [b for b in current_boxes if b not in set(new_objects)]

                    if new_objects:
                        annotated_path = _annotate_image(photo_path, current_boxes, new_objects)
                        try:
                            send_detection(
                                webhook_url=webhook_url,
                                bot_token=bot_token,
                                channel_id=channel_id,
                                new_items=_summarize(new_objects),
                                resident_items=_summarize(resident_objects),
                                captured_at=captured_at,
                                photo_path=annotated_path,
                            )
                        finally:
                            annotated_path.unlink(missing_ok=True)
                        logger.info("新規物体検知: %s", _summarize(new_objects))
                        last_notified_at = now
                prev_boxes = current_boxes
            else:
                consecutive_empty += 1
                if consecutive_empty > EMPTY_TOLERANCE:
                    prev_boxes = []
                    logger.debug("連続%d回0件検知のためprev_boxesをリセットしました", consecutive_empty)

        except Exception as e:
            logger.exception("解析中にエラーが発生しました: %s", e)


def start_analyzer(webhook_url: str, bot_token: str = "", channel_id: str = "") -> None:
    """解析ワーカースレッドを起動する。アプリ起動時に1回だけ呼ぶ。"""
    threading.Thread(
        target=_analyze_worker,
        args=(webhook_url, bot_token, channel_id),
        daemon=True,
    ).start()
    logger.info(
        "画像解析ワーカーを起動しました (モデル=%s, クールダウン=%d秒)",
        YOLO_MODEL, DETECT_COOLDOWN_SECONDS,
    )


def enqueue(photo_path: Path, captured_at: str) -> None:
    """解析キューに画像パスと撮影時刻を追加する。upload_photo エンドポイントから呼ぶ。"""
    _analysis_queue.put((photo_path, captured_at))
