"""
カメラ画像解析モジュール。
サーバGPU（CUDA）でYOLO11m推論を行い、前フレームから新規出現した人物・車両のみSlack通知する。

フロー:
  1. upload_photo エンドポイントから enqueue() で画像パスと撮影時刻を受け取る
  2. バックグラウンドスレッドがキューを消費し、YOLO推論
  3. 前フレームの検知結果と IoU 比較し、新規出現物体のみ通知
     （常駐している隣家の車など、毎回同じ位置にある物体は除外される）
"""

import logging
import os
import queue
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# 設定
DETECT_COOLDOWN_SECONDS = int(os.getenv("DETECT_COOLDOWN_SECONDS", "300"))
YOLO_MODEL = os.getenv("YOLO_MODEL", "yolo11m.pt")
YOLO_CONF = float(os.getenv("YOLO_CONF", "0.5"))
# 前フレームと同じ物体とみなすIoU閾値（この値以上なら「常駐物体」として除外）
NEW_OBJECT_IOU_THRESHOLD = float(os.getenv("NEW_OBJECT_IOU_THRESHOLD", "0.5"))

# COCOクラスID → 日本語ラベル（通知対象のみ）
_DETECT_CLASSES = {0: "人物", 2: "車両", 3: "バイク", 5: "バス", 7: "トラック"}

# キューの要素は (画像パス, 撮影時刻ISO文字列) のタプル
_analysis_queue: queue.Queue[tuple[Path, str]] = queue.Queue()

# 前フレームの検知結果: [(x1, y1, x2, y2, cls_id), ...]
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


def _filter_new_objects(
    current: list[_PrevBox],
    prev: list[_PrevBox],
) -> list[_PrevBox]:
    """
    前フレームに同クラス・同位置（IoU >= 閾値）の物体がなければ新規とみなす。
    新規出現物体のリストを返す。
    """
    new_objects = []
    for box in current:
        cls_id = box[4]
        is_new = not any(
            prev_box[4] == cls_id and _iou(box, prev_box) >= NEW_OBJECT_IOU_THRESHOLD
            for prev_box in prev
        )
        if is_new:
            new_objects.append(box)
    return new_objects


def _analyze_worker(webhook_url: str, bot_token: str, channel_id: str) -> None:
    """解析ワーカースレッド本体。"""
    from datetime import datetime, timezone

    from gps_monitor.notify import send_detection

    model = _load_model()
    prev_boxes: list[_PrevBox] = []
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

            # YOLO物体検知（パスを直接渡す）
            results = model(str(photo_path), conf=YOLO_CONF, verbose=False)[0]
            current_boxes: list[_PrevBox] = []
            for box in results.boxes:
                cls_id = int(box.cls[0])
                if cls_id in _DETECT_CLASSES:
                    x1, y1, x2, y2 = box.xyxy[0].tolist()
                    current_boxes.append((x1, y1, x2, y2, cls_id))

            if not in_cooldown:
                new_objects = _filter_new_objects(current_boxes, prev_boxes)
                if new_objects:
                    detected: dict[str, int] = {}
                    for box in new_objects:
                        label = _DETECT_CLASSES[box[4]]
                        detected[label] = detected.get(label, 0) + 1
                    items = "、".join(f"{label} {count}件" for label, count in detected.items())
                    logger.info("新規物体検知: %s", items)
                    send_detection(
                        webhook_url=webhook_url,
                        bot_token=bot_token,
                        channel_id=channel_id,
                        detected_items=items,
                        captured_at=captured_at,
                        photo_path=photo_path,
                    )
                    last_notified_at = now

            prev_boxes = current_boxes

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
