"""
USBカメラ静止画キャプチャモジュール。

OpenCV (opencv-python-headless) を使用して USB カメラから JPEG 画像を取得する。
"""

import logging

logger = logging.getLogger(__name__)


class UsbCamera:
    """USB カメラから JPEG 静止画を取得するクラス。"""

    def __init__(
        self,
        device: int = 0,
        width: int = 640,
        height: int = 480,
        jpeg_quality: int = 75,
        warm_up_frames: int = 3,
    ) -> None:
        import cv2
        self._cv2 = cv2
        self._cap = cv2.VideoCapture(device)
        # YUYV→BGR変換の色ズレを回避するため MJPEG を優先要求する
        self._cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self._quality = jpeg_quality

        if not self._cap.isOpened():
            raise RuntimeError(f"カメラデバイス {device} を開けませんでした")

        # 最初の数フレームは不安定なので読み捨てる
        for _ in range(warm_up_frames):
            self._cap.read()

        logger.info(
            "カメラ初期化完了: device=%d, %dx%d, quality=%d",
            device, width, height, jpeg_quality,
        )

    def capture_jpeg(self) -> bytes | None:
        """静止画を JPEG バイト列で返す。取得失敗時は None。"""
        ret, frame = self._cap.read()
        if not ret:
            logger.warning("カメラからのフレーム取得に失敗しました")
            return None
        ok, buf = self._cv2.imencode(
            ".jpg", frame, [self._cv2.IMWRITE_JPEG_QUALITY, self._quality]
        )
        if not ok:
            logger.warning("JPEG エンコードに失敗しました")
            return None
        return buf.tobytes()

    def release(self) -> None:
        self._cap.release()
