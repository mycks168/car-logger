from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from gps_relay import main as relay_main

client = TestClient(relay_main.app)

PUSH_HEADERS = {"Authorization": "Bearer test-push-token"}
PULL_HEADERS = {"Authorization": "Bearer test-pull-token"}


@pytest.fixture(autouse=True)
def reset_state():
    """各テスト前にRelayのグローバル状態を初期化する。"""
    relay_main._state.lat = None
    relay_main._state.lon = None
    relay_main._state.alt = None
    relay_main._state.speed_kmh = None
    relay_main._state.has_fix = False
    relay_main._state.gps_serial_active = False
    relay_main._state.last_fix_at = None
    relay_main._state.last_push_at = None
    relay_main._state.wifi_aps = []
    relay_main._state.wifi_scanned_at = None
    yield


def _push_payload(**overrides):
    payload = {
        "lat": 35.681236,
        "lon": 139.767125,
        "alt": 12.3,
        "speed_kmh": 4.5,
        "has_fix": True,
        "gps_serial_active": True,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "wifi_aps": [{"macAddress": "AA:BB:CC:DD:EE:FF", "signalStrength": -55}],
        "wifi_scanned_at": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(overrides)
    return payload


def test_health_no_auth_required():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_push_requires_auth():
    resp = client.post("/push/location", json=_push_payload())
    assert resp.status_code == 401


def test_push_rejects_wrong_token():
    resp = client.post(
        "/push/location",
        json=_push_payload(),
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401


def test_pull_requires_auth():
    resp = client.get("/gps")
    assert resp.status_code == 401


def test_gps_before_any_push_returns_all_null():
    resp = client.get("/gps", headers=PULL_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_fix"] is False
    assert body["gpsd_connected"] is False
    assert body["lat"] is None
    assert body["lon"] is None
    assert body["last_push_at"] is None


def test_push_then_pull_reflects_latest_state():
    push_resp = client.post("/push/location", json=_push_payload(), headers=PUSH_HEADERS)
    assert push_resp.status_code == 200
    assert push_resp.json() == {"ok": True}

    resp = client.get("/gps", headers=PULL_HEADERS)
    body = resp.json()
    assert body["has_fix"] is True
    assert body["gpsd_connected"] is True
    assert body["lat"] == 35.681236
    assert body["lon"] == 139.767125
    assert body["wifi_aps"] == [{"macAddress": "AA:BB:CC:DD:EE:FF", "signalStrength": -55}]
    assert body["last_push_at"] is not None


def test_push_without_fix_keeps_last_known_coordinates():
    client.post("/push/location", json=_push_payload(has_fix=True), headers=PUSH_HEADERS)
    client.post("/push/location", json=_push_payload(has_fix=False), headers=PUSH_HEADERS)

    resp = client.get("/gps", headers=PULL_HEADERS)
    body = resp.json()
    # 直近のPushはfix無しなのでhas_fixはfalseになるが、座標はキャッシュとして残る
    assert body["has_fix"] is False
    assert body["lat"] == 35.681236
    assert body["lon"] == 139.767125


def test_stale_push_marks_gpsd_disconnected():
    client.post("/push/location", json=_push_payload(), headers=PUSH_HEADERS)
    with relay_main._state_lock:
        relay_main._state.last_push_at = datetime.now(timezone.utc) - timedelta(
            seconds=relay_main.PUSH_TIMEOUT_SECONDS + 1
        )

    resp = client.get("/gps", headers=PULL_HEADERS)
    body = resp.json()
    assert body["gpsd_connected"] is False
    assert body["has_fix"] is False


def test_heartbeat_push_without_coordinates_keeps_last_known_position():
    """GPS fix無しでもESP32はハートビートとしてPushし続ける（座標欄はnull）。
    座標を含まないPushで、キャッシュ済みの最終既知座標が消えないことを確認する。"""
    client.post("/push/location", json=_push_payload(), headers=PUSH_HEADERS)
    heartbeat = _push_payload(
        lat=None, lon=None, alt=None, speed_kmh=None,
        has_fix=False, gps_serial_active=False,
    )
    resp = client.post("/push/location", json=heartbeat, headers=PUSH_HEADERS)
    assert resp.status_code == 200

    body = client.get("/gps", headers=PULL_HEADERS).json()
    assert body["gpsd_connected"] is True  # ハートビート自体は届いている
    assert body["gps_serial_active"] is False  # GPSモジュールは無応答
    assert body["has_fix"] is False
    assert body["lat"] == 35.681236  # 最終既知座標はキャッシュされたまま
    assert body["lon"] == 139.767125


def test_gps_serial_active_reflects_latest_push():
    client.post(
        "/push/location",
        json=_push_payload(has_fix=False, gps_serial_active=True),
        headers=PUSH_HEADERS,
    )
    body = client.get("/gps", headers=PULL_HEADERS).json()
    assert body["gps_serial_active"] is True
    assert body["has_fix"] is False


def test_gps_serial_active_is_false_when_push_stale():
    """Push自体が古い（機器がオフラインかもしれない）場合は、キャッシュされた
    gps_serial_activeの値をそのまま信用せず、falseとして返す。"""
    client.post(
        "/push/location",
        json=_push_payload(gps_serial_active=True),
        headers=PUSH_HEADERS,
    )
    with relay_main._state_lock:
        relay_main._state.last_push_at = datetime.now(timezone.utc) - timedelta(
            seconds=relay_main.PUSH_TIMEOUT_SECONDS + 1
        )

    body = client.get("/gps", headers=PULL_HEADERS).json()
    assert body["gpsd_connected"] is False
    assert body["gps_serial_active"] is False


def test_expired_cache_invalidates_coordinates():
    client.post("/push/location", json=_push_payload(), headers=PUSH_HEADERS)
    with relay_main._state_lock:
        relay_main._state.last_fix_at = datetime.now(timezone.utc) - timedelta(
            seconds=relay_main.CACHE_MAX_AGE_SECONDS + 1
        )

    resp = client.get("/gps", headers=PULL_HEADERS)
    body = resp.json()
    assert body["lat"] is None
    assert body["lon"] is None
