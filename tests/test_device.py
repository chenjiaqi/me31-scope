"""寄存器换算测试。"""

from __future__ import annotations

import pytest

from me31_scope import device as dev


def test_decode_positive_temperature() -> None:
    assert dev.decode_temp(0x012C) == 30.0


def test_decode_negative_temperature_uses_twos_complement() -> None:
    # 手册 4.1.2 的例子：0xFFCE = -50 -> -5.0℃
    assert dev.decode_temp(0xFFCE) == -5.0


def test_decode_sensor_fault_returns_none() -> None:
    # 手册 4.7：传感器未接入时寄存器为 0xFC19，即有符号 -999
    assert dev.decode_temp(dev.SENSOR_FAULT_RAW) is None
    assert dev.decode_temp(0xFC19) is None


def test_decode_zero() -> None:
    assert dev.decode_temp(0x0000) == 0.0


def test_decode_temps_tuple() -> None:
    assert dev.decode_temps([0x012C, 0xFFCE, 0xFC19, 0x000A]) == (30.0, -5.0, None, 1.0)


@pytest.mark.parametrize(
    ("celsius", "raw"),
    [(0.0, 0), (8.5, 85), (-8.5, -85), (10.0, 100), (-10.0, -100), (0.1, 1)],
)
def test_encode_offset_round_trip(celsius: float, raw: int) -> None:
    assert dev.encode_offset(celsius) == raw
    assert dev.decode_offset(raw & 0xFFFF) == celsius


def test_encode_offset_clamps_to_range() -> None:
    assert dev.encode_offset(99.9) == dev.OFFSET_RAW_MAX
    assert dev.encode_offset(-99.9) == dev.OFFSET_RAW_MIN


def test_decode_offsets() -> None:
    assert dev.decode_offsets([0x0055, 0xFFAB, 0x0000, 0x0064]) == (8.5, -8.5, 0.0, 10.0)


def test_format_temp() -> None:
    assert dev.format_temp(25.34) == "25.3"
    assert dev.format_temp(-5.0) == "-5.0"
    assert dev.format_temp(None) == "--"


def test_register_map_matches_manual() -> None:
    assert dev.TEMP_START == 0x0190
    assert dev.TEMP_COUNT == 4
    assert dev.OFFSET_START == 0x2328
    assert dev.OFFSET_COUNT == 4
    assert dev.REBOOT_REG == 0x07EA
    assert dev.SENSOR_FAULT_RAW == 0xFC19


def test_defaults_match_manual() -> None:
    assert dev.DEFAULT_BAUDRATE == 9600
    assert dev.DEFAULT_PARITY == "N"
    assert dev.DEFAULT_SLAVE == 1
