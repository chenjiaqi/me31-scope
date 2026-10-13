"""ME31-XDXX0400 的寄存器表与数值换算。

依据《ME31-XDXX0400 用户手册 V1.2》第 5.1 章。本模块不依赖 Qt。
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable, Sequence

CHANNELS = 4
CHANNEL_NAMES = ("CH1", "CH2", "CH3", "CH4")

# 温度整型值：输入寄存器，FC 0x04，每寄存器一个通道，值/10 = ℃
TEMP_START = 0x0190
TEMP_COUNT = CHANNELS

# 温度偏移值：保持寄存器，FC 0x03 读 / FC 0x06 写，值/10 = ℃
OFFSET_START = 0x2328
OFFSET_COUNT = CHANNELS

# 设备维护寄存器
REBOOT_REG = 0x07EA
FW_VERSION_REG = 0x07DC
MODEL_REG = 0x07D0

# 手册 4.7：传感器未接入时寄存器值为 0xFC19，即有符号 -999
SENSOR_FAULT_RAW = 0xFC19

# 偏移值原始范围 -100~100，对应 -10.0~10.0℃
OFFSET_RAW_MIN = -100
OFFSET_RAW_MAX = 100
OFFSET_CELSIUS_MIN = OFFSET_RAW_MIN / 10.0
OFFSET_CELSIUS_MAX = OFFSET_RAW_MAX / 10.0

SCALE = 10.0

# 串口默认参数（手册 3.2）
DEFAULT_BAUDRATE = 9600
DEFAULT_PARITY = "N"
DEFAULT_SLAVE = 1
BAUDRATE_CHOICES = (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200)
PARITY_CHOICES = ("N", "O", "E")

# 轮询周期：设备采集频率为 1 Hz，所以每秒读一次
POLL_INTERVAL_MS = 1000
# 响应超时：9600bps 下 13 字节约 14 ms，留足余量
RESPONSE_TIMEOUT_MS = 400


@dataclass(frozen=True, slots=True)
class Sample:
    """一次轮询得到的一组采样。温度为 None 表示该通道传感器未接入。"""

    time: float
    temps: tuple[float | None, ...]


def decode_temp(raw: int) -> float | None:
    """温度原始值 → 摄氏度。传感器未接入返回 None。

    raw 为 16 位无符号，负数以补码形式上传。
    """
    if raw == SENSOR_FAULT_RAW:
        return None
    signed = raw - 0x10000 if raw & 0x8000 else raw
    return signed / SCALE


def decode_temps(raws: Iterable[int]) -> tuple[float | None, ...]:
    """一组温度原始值 → 一组摄氏度。"""
    return tuple(decode_temp(raw) for raw in raws)


def encode_offset(celsius: float) -> int:
    """偏移摄氏度 → 寄存器原始值，超出范围时钳位到 ±10.0℃。"""
    raw = int(round(celsius * SCALE))
    return max(OFFSET_RAW_MIN, min(OFFSET_RAW_MAX, raw))


def decode_offset(raw: int) -> float:
    """偏移寄存器原始值 → 摄氏度。"""
    signed = raw - 0x10000 if raw & 0x8000 else raw
    return signed / SCALE


def decode_offsets(raws: Sequence[int]) -> tuple[float, ...]:
    """一组偏移原始值 → 一组摄氏度。"""
    return tuple(decode_offset(raw) for raw in raws)


def format_temp(celsius: float | None) -> str:
    """界面/CSV 用的温度文本，未接入显示为 --。"""
    return "--" if celsius is None else f"{celsius:.1f}"
