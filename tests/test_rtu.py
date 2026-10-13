"""Modbus RTU 组帧与解析测试，CRC 向量取自 ME31-XDXX0400 用户手册 V1.2。"""

from __future__ import annotations

import pytest

from temperature_acq import rtu


def _response(raws: list[int]) -> bytes:
    """构造一条读寄存器响应帧（从机地址 1，功能码 0x04）。"""
    body = bytes([len(raws) * 2]) + rtu.registers_to_bytes(raws)
    return rtu.append_crc(bytes([0x01, rtu.FC_READ_INPUT]) + body)


# ------------------------------------------------------------------ 组帧


def test_crc_matches_manual_vectors() -> None:
    # 手册帧尾的校验码按低字节在前（小端）传输，故整数值为低位在前
    assert rtu.append_crc(bytes.fromhex("010401900004")) == bytes.fromhex("010401900004F018")
    assert rtu.append_crc(bytes.fromhex("010401900001")) == bytes.fromhex("010401900001301B")
    assert rtu.append_crc(bytes.fromhex("010402012C")) == bytes.fromhex("010402012CB97D")

    assert rtu.crc16_modbus(bytes.fromhex("010401900004")) == 0x18F0
    assert rtu.crc16_modbus(bytes.fromhex("010401900001")) == 0x1B30
    assert rtu.crc16_modbus(bytes.fromhex("010402012C")) == 0x7DB9


def test_build_read_input_all_channels() -> None:
    assert rtu.build_read_input(1, 0x0190, 4) == bytes.fromhex("010401900004F018")


def test_build_read_input_single_channel() -> None:
    assert rtu.build_read_input(1, 0x0190, 1) == bytes.fromhex("010401900001301B")


def test_build_read_holding() -> None:
    assert rtu.build_read_holding(1, 0x0190, 4) == bytes.fromhex("01030190000445D8")


def test_build_write_single() -> None:
    assert rtu.build_write_single(1, 0x2328, 85) == bytes.fromhex("010623280055C279")
    # 负值按补码下发：-8.5℃ -> -85 -> 0xFFAB
    assert rtu.build_write_single(1, 0x2328, -85) == bytes.fromhex("01062328FFAB0209")


def test_build_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        rtu.build_read_input(0, 0x0190, 4)
    with pytest.raises(ValueError):
        rtu.build_read_input(1, 0x0190, 0)
    with pytest.raises(ValueError):
        rtu.build_write_single(1, 0x2328, 0x8000)


def test_expected_len() -> None:
    assert rtu.expected_len(rtu.FC_READ_INPUT, 4) == 13
    assert rtu.expected_len(rtu.FC_READ_HOLDING, 4) == 13
    assert rtu.expected_len(rtu.FC_WRITE_SINGLE) == 8
    assert rtu.expected_len(rtu.FC_READ_INPUT | 0x80) == 5


def test_check_crc() -> None:
    assert rtu.check_crc(bytes.fromhex("010402012CB97D"))
    assert not rtu.check_crc(bytes.fromhex("010402012CB97E"))
    assert not rtu.check_crc(b"\x01\x04")


# ------------------------------------------------------------------ 解帧


def test_parse_registers_round_trip() -> None:
    frame = rtu.append_crc(
        b"\x01\x04\x08" + rtu.registers_to_bytes([0x012C, 0xFFCE, 0xFC19, 0x0000])
    )
    assert rtu.parse_registers(frame) == [0x012C, 0xFFCE, 0xFC19, 0x0000]


def test_parse_registers_matches_manual_response() -> None:
    assert rtu.parse_registers(bytes.fromhex("010402012CB97D")) == [0x012C]


def test_parse_registers_rejects_length_mismatch() -> None:
    with pytest.raises(ValueError):
        rtu.parse_registers(rtu.append_crc(b"\x01\x04\x09" + b"\x00" * 8))


def test_to_signed() -> None:
    assert rtu.to_signed(0x012C) == 300
    assert rtu.to_signed(0xFFCE) == -50
    assert rtu.to_signed(0xFC19) == -999


# ------------------------------------------------------------------ 解析状态机


def test_parser_handles_split_frames() -> None:
    parser = rtu.RtuParser()
    parser.expect_echo(rtu.build_read_input(1, 0x0190, 4))
    frame = _response([0x012C, 0x012D, 0x012E, 0x012F])

    for byte in frame[:-1]:
        parser.feed(bytes([byte]))
        assert parser.pop_response(1, rtu.FC_READ_INPUT, 4) is None

    parser.feed(frame[-1:])
    assert rtu.parse_registers(parser.pop_response(1, rtu.FC_READ_INPUT, 4)) == [
        0x012C,
        0x012D,
        0x012E,
        0x012F,
    ]


def test_parser_keeps_fc06_response_identical_to_request() -> None:
    """回归测试：FC06 的成功响应逐字节等于请求，不能被当成回显剥掉。

    只回响应、不回显的适配器下，缓冲区里就只有这一帧——剥掉就等于把响应丢了，
    表现为写偏移永远超时失败。
    """
    parser = rtu.RtuParser()
    request = rtu.build_write_single(1, 0x2328, 0)
    parser.expect_echo(request)

    parser.feed(request)  # 设备响应 == 请求本身

    frame = parser.pop_response(1, rtu.FC_WRITE_SINGLE)
    assert frame == request


def test_parser_handles_fc06_echo_and_response() -> None:
    """回显型适配器下 FC06 会收到两帧完全相同的字节。"""
    parser = rtu.RtuParser()
    request = rtu.build_write_single(1, 0x2328, 0x0055)
    parser.expect_echo(request)

    parser.feed(request + request)  # 回显 + 响应

    assert parser.pop_response(1, rtu.FC_WRITE_SINGLE) == request


def test_parser_raises_fc06_exception_frame() -> None:
    """FC06 被设备拒绝时应抛出异常，而不是当成成功。"""
    parser = rtu.RtuParser()
    parser.expect_echo(rtu.build_write_single(1, 0x2328, 1))

    parser.feed(rtu.append_crc(bytes([0x01, 0x86, 0x02])))

    with pytest.raises(rtu.ModbusException) as info:
        parser.pop_response(1, rtu.FC_WRITE_SINGLE)
    assert info.value.code == 0x02


def test_parser_strips_half_duplex_echo() -> None:
    parser = rtu.RtuParser()
    request = rtu.build_read_input(1, 0x0190, 4)
    parser.expect_echo(request)

    parser.feed(request + _response([0x012C] * 4))

    frame = parser.pop_response(1, rtu.FC_READ_INPUT, 4)
    assert frame is not None
    assert rtu.parse_registers(frame)[0] == 0x012C


def test_parser_waits_for_incomplete_echo() -> None:
    parser = rtu.RtuParser()
    request = rtu.build_read_input(1, 0x0190, 4)
    parser.expect_echo(request)

    parser.feed(request[:3])
    assert parser.pop_response(1, rtu.FC_READ_INPUT, 4) is None
    # 回显剩余部分应被剥掉，不能让后面的数据被误当成帧
    parser.feed(request[3:] + _response([0x012C] * 4))
    frame = parser.pop_response(1, rtu.FC_READ_INPUT, 4)
    assert frame is not None
    assert rtu.parse_registers(frame)[0] == 0x012C


def test_parser_ignores_leading_garbage() -> None:
    parser = rtu.RtuParser()
    parser.expect_echo(rtu.build_read_input(1, 0x0190, 4))

    parser.feed(b"\xff\x00\x7a" + _response([0x012C] * 4))

    frame = parser.pop_response(1, rtu.FC_READ_INPUT, 4)
    assert frame is not None
    assert rtu.parse_registers(frame)[0] == 0x012C


def test_parser_resyncs_after_crc_failure() -> None:
    """数据字节被破坏时，只前进一个字节重新同步，随后的好帧仍能解析出来。"""
    parser = rtu.RtuParser()
    parser.expect_echo(rtu.build_read_input(1, 0x0190, 4))
    good = _response([0x012C] * 4)
    corrupted = good[:3] + bytes([good[3] ^ 0xFF]) + good[4:]

    parser.feed(corrupted + good)

    frame = parser.pop_response(1, rtu.FC_READ_INPUT, 4)
    assert frame is not None
    assert rtu.parse_registers(frame)[0] == 0x012C


def test_parser_resyncs_after_byte_count_mismatch() -> None:
    parser = rtu.RtuParser()
    parser.expect_echo(rtu.build_read_input(1, 0x0190, 4))
    good = _response([0x012C] * 4)
    corrupted = good[:2] + b"\x09" + good[3:]

    parser.feed(corrupted + good)

    frame = parser.pop_response(1, rtu.FC_READ_INPUT, 4)
    assert frame is not None
    assert rtu.parse_registers(frame)[0] == 0x012C


def test_parser_raises_on_exception_frame() -> None:
    parser = rtu.RtuParser()
    parser.expect_echo(rtu.build_read_input(1, 0x0190, 4))

    parser.feed(rtu.append_crc(bytes([0x01, rtu.FC_READ_INPUT | 0x80, 0x02])))

    with pytest.raises(rtu.ModbusException) as excinfo:
        parser.pop_response(1, rtu.FC_READ_INPUT, 4)
    assert excinfo.value.code == 0x02


def test_parser_drops_wrong_slave_address() -> None:
    parser = rtu.RtuParser()
    parser.expect_echo(rtu.build_read_input(2, 0x0190, 4))

    parser.feed(_response([0x012C] * 4))

    assert parser.pop_response(2, rtu.FC_READ_INPUT, 4) is None


def test_parser_reset_clears_buffer() -> None:
    parser = rtu.RtuParser()
    parser.feed(_response([0x012C] * 4))
    assert parser.pending > 0
    parser.reset()
    assert parser.pending == 0


def test_buffer_is_bounded() -> None:
    parser = rtu.RtuParser()
    parser.feed(b"\xff" * (rtu.MAX_BUFFER * 2))
    assert parser.pending <= rtu.MAX_BUFFER
