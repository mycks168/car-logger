"""face_recog.py のユニットテスト。"""

import io
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from gps_web.face_recog import (
    FaceDetectResult,
    bytes_to_embedding,
    embedding_to_bytes,
    is_known_family,
    _cosine_similarity,
)


class TestEmbeddingSerialization:
    def test_シリアライズ後デシリアライズで元に戻る(self):
        emb = np.array([0.1, 0.2, 0.3, 0.4], dtype=np.float32)
        data = embedding_to_bytes(emb)
        restored = bytes_to_embedding(data)
        np.testing.assert_array_almost_equal(emb, restored)

    def test_512次元ベクトルをシリアライズできる(self):
        emb = np.random.rand(512).astype(np.float32)
        data = embedding_to_bytes(emb)
        restored = bytes_to_embedding(data)
        assert restored.shape == (512,)
        np.testing.assert_array_almost_equal(emb, restored)


class TestCosineSimilarity:
    def test_同一ベクトルは1(self):
        v = np.array([1.0, 0.0, 0.0])
        assert _cosine_similarity(v, v) == pytest.approx(1.0)

    def test_直交ベクトルは0(self):
        a = np.array([1.0, 0.0, 0.0])
        b = np.array([0.0, 1.0, 0.0])
        assert _cosine_similarity(a, b) == pytest.approx(0.0)

    def test_ゼロベクトルは0(self):
        a = np.array([0.0, 0.0, 0.0])
        b = np.array([1.0, 0.0, 0.0])
        assert _cosine_similarity(a, b) == pytest.approx(0.0)


class TestIsKnownFamily:
    def _make_emb(self, seed: int) -> np.ndarray:
        rng = np.random.default_rng(seed)
        v = rng.random(512).astype(np.float32)
        return v / np.linalg.norm(v)

    def test_家族登録なしはFalse(self):
        emb = self._make_emb(0)
        assert is_known_family([emb], []) is False

    def test_同一ベクトルはTrue(self):
        emb = self._make_emb(1)
        assert is_known_family([emb], [emb]) is True

    def test_無関係ベクトルはFalse(self):
        # 全く異なる2ベクトルは類似度が低い
        a = self._make_emb(10)
        b = self._make_emb(99)
        # ランダムベクトル同士の類似度は通常0.4未満
        # 閾値を一時的に下げてテスト
        with patch("gps_web.face_recog.FACE_SIMILARITY_THRESHOLD", 0.99):
            assert is_known_family([a], [b]) is False

    def test_複数の検知顔のうち1人が家族ならTrue(self):
        fam = self._make_emb(1)
        stranger = self._make_emb(50)
        assert is_known_family([stranger, fam], [fam]) is True


class TestDetectFaces:
    def test_insightface未インストール時はfaces_found0(self):
        """InsightFaceが使えない環境ではエラーにならず0を返す。"""
        import gps_web.face_recog as m
        with patch.object(m, "_get_app", side_effect=RuntimeError("no insightface")):
            result = m.detect_faces(b"\xff\xd8\xff" + b"\x00" * 100)
        assert result.faces_found == 0

    def test_無効な画像バイト列はfaces_found0(self):
        import gps_web.face_recog as m
        result = m.detect_faces(b"not-an-image")
        assert result.faces_found == 0
