"""ハードウェアバックエンドファクトリ。"""
from hardware.base import DisplayBackend


def create_display(backlight: int) -> DisplayBackend:
    from hardware.pi3.display import Display
    return Display(backlight=backlight)
