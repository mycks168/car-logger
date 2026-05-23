"""プラットフォーム非依存のシステム情報読み取りユーティリティ。"""
import fcntl
import os
import re
import socket
import struct
import subprocess

POWER_SUPPLY_SYS = "/sys/class/power_supply"
PISUGAR_SOCKET = "/tmp/pisugar-server.sock"


def _wifi_connected() -> bool:
    try:
        with open("/sys/class/net/wlan0/operstate") as f:
            return f.read().strip() == "up"
    except OSError:
        return False


def _read_pisugar_battery() -> tuple[int | None, str | None]:
    if not os.path.exists(PISUGAR_SOCKET):
        return (None, None)
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect(PISUGAR_SOCKET)
        sock.sendall(b"get battery\n")
        data = sock.recv(64).decode("utf-8", errors="ignore").strip()
        sock.close()
        m = re.search(r"(\d+)", data)
        if not m:
            return (None, None)
        pct = max(0, min(100, int(m.group(1))))
        status = None
        try:
            s2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s2.settimeout(0.5)
            s2.connect(PISUGAR_SOCKET)
            s2.sendall(b"get battery_charging\n")
            ch = s2.recv(64).decode("utf-8", errors="ignore").strip().lower()
            s2.close()
            if "true" in ch:
                status = "Charging"
            elif "false" in ch:
                status = "Discharging"
        except (OSError, socket.error):
            pass
        return (pct, status)
    except (OSError, socket.error, ValueError):
        return (None, None)


def _read_battery() -> tuple[int | None, str | None]:
    result = _read_pisugar_battery()
    if result[0] is not None:
        return result
    if not os.path.isdir(POWER_SUPPLY_SYS):
        return (None, None)

    def is_battery_dir(base: str) -> bool:
        type_path = os.path.join(base, "type")
        if os.path.isfile(type_path):
            try:
                with open(type_path) as f:
                    return f.read().strip().upper() == "BATTERY"
            except OSError:
                pass
        return False

    for name in sorted(os.listdir(POWER_SUPPLY_SYS)):
        base = os.path.join(POWER_SUPPLY_SYS, name)
        if not os.path.isdir(base):
            continue
        if not (name.upper().startswith("BAT") or name.lower() == "battery" or is_battery_dir(base)):
            continue
        cap_path = os.path.join(base, "capacity")
        status_path = os.path.join(base, "status")
        pct = None
        if os.path.isfile(cap_path):
            try:
                with open(cap_path) as f:
                    pct = int(f.read().strip())
            except (ValueError, OSError):
                pass
        status = None
        if os.path.isfile(status_path):
            try:
                with open(status_path) as f:
                    status = f.read().strip()
            except OSError:
                pass
        if pct is not None:
            return (pct, status)
    return (None, None)


def _read_cpu_temp() -> float | None:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000.0
    except OSError:
        return None


def _wifi_mode() -> str:
    """wlan0 の動作モードを返す: 'client' / 'ap' / 'none'"""
    try:
        result = subprocess.run(
            ["iw", "dev", "wlan0", "info"],
            capture_output=True, text=True, timeout=2,
        )
        if "type AP" in result.stdout:
            return "ap"
        if "type managed" in result.stdout:
            with open("/sys/class/net/wlan0/operstate") as f:
                return "client" if f.read().strip() == "up" else "none"
    except Exception:
        pass
    return "none"


def _eth1_connected() -> bool:
    """eth1 (USBテザリング) が up かどうかを返す。"""
    try:
        with open("/sys/class/net/eth1/operstate") as f:
            return f.read().strip() == "up"
    except OSError:
        return False


def _get_ip_address(iface: str = "wlan0") -> str | None:
    SIOCGIFADDR = 0x8915
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        raw = fcntl.ioctl(s.fileno(), SIOCGIFADDR, struct.pack("256s", iface[:15].encode()))
        return socket.inet_ntoa(raw[20:24])
    except OSError:
        return None
