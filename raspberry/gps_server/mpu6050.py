"""
MPU-6050 加速度センサードライバ（I2C）。

接続: ラズパイのI2Cバス（デフォルト bus=1）、アドレス 0x68（AD0=GND）または 0x69（AD0=3.3V）。
"""

import math

_PWR_MGMT_1 = 0x6B
_ACCEL_XOUT_H = 0x3B
_SENSITIVITY_2G = 16384.0  # LSB/g（±2gレンジ）
_GRAVITY = 9.80665          # m/s²


def _to_signed16(val: int) -> int:
    return val - 65536 if val >= 32768 else val


class MPU6050:
    """MPU-6050 加速度センサー。smbus2 経由でI2C読み取りを行う。"""

    def __init__(self, bus: int = 1, addr: int = 0x68) -> None:
        import smbus2
        self._bus = smbus2.SMBus(bus)
        self._addr = addr
        # スリープモードを解除
        self._bus.write_byte_data(addr, _PWR_MGMT_1, 0x00)

    def read_accel_ms2(self) -> tuple[float, float, float]:
        """加速度を m/s² で返す (X, Y, Z)。"""
        data = self._bus.read_i2c_block_data(self._addr, _ACCEL_XOUT_H, 6)
        x = _to_signed16(data[0] << 8 | data[1]) / _SENSITIVITY_2G * _GRAVITY
        y = _to_signed16(data[2] << 8 | data[3]) / _SENSITIVITY_2G * _GRAVITY
        z = _to_signed16(data[4] << 8 | data[5]) / _SENSITIVITY_2G * _GRAVITY
        return x, y, z

    def dynamic_accel_magnitude(self) -> float:
        """
        重力成分を除いた動的加速度の大きさ（m/s²）を返す。
        静止状態では ~0、加速・振動があると値が増える。
        """
        x, y, z = self.read_accel_ms2()
        magnitude = math.sqrt(x ** 2 + y ** 2 + z ** 2)
        return abs(magnitude - _GRAVITY)
