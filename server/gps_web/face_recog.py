"""
InsightFace (CUDA) を使った顔検知・埋め込み抽出・家族照合モジュール。

初回起動時に InsightFace のモデル（buffalo_l, 約500MB）が自動ダウンロードされる。
"""

import io
import logging
import os
from dataclasses import dataclass, field

import numpy as np

logger = logging.getLogger(__name__)

FACE_SIMILARITY_THRESHOLD = float(os.getenv("FACE_SIMILARITY_THRESHOLD", "0.4"))

# InsightFaceアプリのシングルトン（初期化コストを避けるため）
_app = None


def _get_app():
    global _app
    if _app is not None:
        return _app
    try:
        from insightface.app import FaceAnalysis
        app = FaceAnalysis(providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
        app.prepare(ctx_id=0, det_size=(640, 640))
        _app = app
        logger.info("InsightFace 初期化完了 (CUDA優先)")
        return _app
    except Exception as e:
        logger.error("InsightFace 初期化失敗: %s", e)
        raise


@dataclass
class FaceDetectResult:
    faces_found: int
    embeddings: list[np.ndarray] = field(default_factory=list)
    is_family: bool = False


def embedding_to_bytes(emb: np.ndarray) -> bytes:
    """numpy配列をバイト列にシリアライズする。"""
    buf = io.BytesIO()
    np.save(buf, emb)
    return buf.getvalue()


def bytes_to_embedding(data: bytes) -> np.ndarray:
    """バイト列からnumpy配列にデシリアライズする。"""
    return np.load(io.BytesIO(data))


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def detect_faces(image_bytes: bytes) -> FaceDetectResult:
    """
    画像から顔を検知し埋め込みベクトルを抽出する。

    Args:
        image_bytes: JPEG画像のバイト列

    Returns:
        FaceDetectResult（顔数・埋め込みリスト）
    """
    import cv2
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        logger.warning("画像のデコードに失敗しました")
        return FaceDetectResult(faces_found=0)

    try:
        app = _get_app()
        faces = app.get(img)
    except Exception as e:
        logger.error("InsightFace 顔検知エラー: %s", e)
        return FaceDetectResult(faces_found=0)

    if not faces:
        return FaceDetectResult(faces_found=0)

    embeddings = [f.embedding for f in faces if f.embedding is not None]
    return FaceDetectResult(faces_found=len(faces), embeddings=embeddings)


def is_known_family(embeddings: list[np.ndarray], family_embeddings: list[np.ndarray]) -> bool:
    """
    検知した顔の埋め込みが家族の埋め込みのいずれかと類似しているか判定する。

    Args:
        embeddings: 検知した顔の埋め込みリスト
        family_embeddings: 登録済み家族埋め込みリスト

    Returns:
        1人でも家族と一致すれば True
    """
    if not family_embeddings:
        return False
    for emb in embeddings:
        for fam_emb in family_embeddings:
            sim = _cosine_similarity(emb, fam_emb)
            if sim >= FACE_SIMILARITY_THRESHOLD:
                logger.debug("家族と一致 (類似度=%.3f, 閾値=%.3f)", sim, FACE_SIMILARITY_THRESHOLD)
                return True
    return False
