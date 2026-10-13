"""Modbus RTU 帧的组帧与解析。

本模块不依赖 Qt，可脱离显示器直接单元测试。
"""

from __future__ import annotations

from collections.abc import Iterable

# 功能码
FC_READ_HOLDING = 0x03
FC_READ_INPUT = 0x04
FC_WRITE_SINGLE = 0x06

EXCEPTION_FLAG = 0x80

# 单条 RTU 帧最长 256 字节，这里再宽一些防止脏数据无限堆积
MAX_BUFFER = 512


class ModbusException(Exception):
    """设备返回了异常响应帧（功能码最高位置 1）。"""

    MESSAGES = {
        0x01: "非法功能码",
        0x02: "非法数据地址",
        0x03: "非法数据值",
        0x04: "从机设备故障",
        0x05: "确认（需较长时间处理）",
        0x06: "从机设备忙",
        0x08: "存储奇偶校验错误",
        0x0A: "网关路径不可用",
        0x0B: "网关目标设备无响应",
    }

    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(f"Modbus 异常 0x{code:02X}：{self.MESSAGES.get(code, '未知错误')}")


def crc16_modbus(data: bytes) -> int:
    """CRC-16/MODBUS（多项式 0xA001 反射，初值 0xFFFF）。"""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def append_crc(pdu: bytes) -> bytes:
    """把校验码以小端序追加到帧尾。"""
    return pdu + crc16_modbus(pdu).to_bytes(2, "little")


def check_crc(frame: bytes) -> bool:
    """CRC 校验，frame 含帧尾两字节校验码。"""
    if len(frame) < 4:
        return False
    return crc16_modbus(frame[:-2]) == int.from_bytes(frame[-2:], "little")


def build_read_input(slave: int, start: int, count: int) -> bytes:
    """组 FC 0x04 读输入寄存器请求。"""
    return _build_read(FC_READ_INPUT, slave, start, count)


def build_read_holding(slave: int, start: int, count: int) -> bytes:
    """组 FC 0x03 读保持寄存器请求。"""
    return _build_read(FC_READ_HOLDING, slave, start, count)


def _build_read(fc: int, slave: int, start: int, count: int) -> bytes:
    if not 1 <= slave <= 247:
        raise ValueError(f"从机地址超出范围: {slave}")
    if not 0 <= start <= 0xFFFF:
        raise ValueError(f"寄存器地址超出范围: {start}")
    if not 1 <= count <= 125:
        raise ValueError(f"寄存器数量超出范围: {count}")
    return append_crc(bytes([slave, fc]) + start.to_bytes(2, "big") + count.to_bytes(2, "big"))


def build_write_single(slave: int, address: int, value: int) -> bytes:
    """组 FC 0x06 写单个保持寄存器请求，value 为 16 位有符号。"""
    if not 1 <= slave <= 247:
        raise ValueError(f"从机地址超出范围: {slave}")
    if not 0 <= address <= 0xFFFF:
        raise ValueError(f"寄存器地址超出范围: {address}")
    if not -0x8000 <= value <= 0x7FFF:
        raise ValueError(f"寄存器值超出范围: {value}")
    payload = address.to_bytes(2, "big") + (value & 0xFFFF).to_bytes(2, "big")
    return append_crc(bytes([slave, FC_WRITE_SINGLE]) + payload)


def expected_len(fc: int, count: int = 0) -> int:
    """响应帧的完整长度（含 2 字节 CRC）。"""
    if fc & EXCEPTION_FLAG:
        return 5
    if fc in (FC_READ_HOLDING, FC_READ_INPUT):
        # 从机地址 + 功能码 + 字节数 + 数据 + CRC
        return 3 + 2 * count + 2
    if fc == FC_WRITE_SINGLE:
        # 回显请求：地址 + 功能码 + 寄存器地址 + 值 + CRC
        return 8
    raise ValueError(f"不支持的功能码: 0x{fc:02X}")


def parse_registers(frame: bytes) -> list[int]:
    """从读寄存器响应帧中取出 16 位无符号值列表。"""
    byte_count = frame[2]
    if byte_count % 2 != 0 or len(frame) != byte_count + 5:
        raise ValueError(f"响应帧长度与字节数不一致: {frame.hex(' ').upper()}")
    body = frame[3:-2]
    return [int.from_bytes(body[i : i + 2], "big") for i in range(0, byte_count, 2)]


def to_signed(value: int) -> int:
    """16 位无符号 → 有符号。"""
    return value - 0x10000 if value & 0x8000 else value


class RtuParser:
    """把串口字节流切割成一条条合法的 Modbus 响应帧。

    一次只允许一个未完成的事务，因此解析时已知期望的功能码与寄存器个数。
    能处理半双工适配器回显、分片到达、脏数据失步等情况。
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._echo = b""

    def reset(self) -> None:
        """丢弃缓冲与回显记录（超时或重新连接时调用）。"""
        self._buffer.clear()
        self._echo = b""

    def expect_echo(self, request: bytes) -> None:
        """登记刚发出的请求，用于剥离半双工回显。"""
        self._echo = request

    def feed(self, data: bytes) -> None:
        self._buffer.extend(data)
        if len(self._buffer) > MAX_BUFFER:
            del self._buffer[: len(self._buffer) - MAX_BUFFER]

    def pop_response(self, slave: int, fc: int, count: int = 0) -> bytes | None:
        """返回一条校验通过的响应帧，数据不足时返回 None。

        遇到脏数据只丢弃一个字节后重新同步，不清空整个缓冲。
        """
        while True:
            if self._strip_echo():
                return None  # 回显还没收全，等下一次 readyRead
            if not self._buffer:
                return None

            if self._buffer[0] != slave:
                del self._buffer[0]
                continue

            if len(self._buffer) < 2:
                return None

            frame_fc = self._buffer[1]
            if frame_fc not in (fc, fc | EXCEPTION_FLAG):
                del self._buffer[0]
                continue

            if len(self._buffer) < 3:
                return None

            if frame_fc & EXCEPTION_FLAG:
                length = 5
            elif frame_fc in (FC_READ_HOLDING, FC_READ_INPUT):
                # 字节数必须与请求的寄存器个数吻合，否则是残帧
                if self._buffer[2] != 2 * count:
                    del self._buffer[0]
                    continue
                length = expected_len(frame_fc, count)
            else:
                length = expected_len(frame_fc)

            if len(self._buffer) < length:
                return None

            frame = bytes(self._buffer[:length])
            del self._buffer[:length]

            if not check_crc(frame):
                # 校验失败：把这帧去掉首字节后放回队首，等价于只前进一个字节重新同步
                self._buffer[:0] = frame[1:]
                continue

            if frame_fc & EXCEPTION_FLAG:
                raise ModbusException(frame[2])
            return frame

    def _strip_echo(self) -> bool:
        """剥离半双工适配器回显。返回 True 表示回显尚未收全，应继续等待。"""
        if not self._echo:
            return False
        # FC06 的成功响应按标准就是「原样回显请求」，与适配器回显逐字节相同，
        # 无从区分。此时绝不能剥离，否则会把真正的响应也一起丢掉，
        # 表现为写入永远超时失败（温度轮询不受影响，因为 FC03/04 的响应与请求不同）。
        if self._echo[1] == FC_WRITE_SINGLE:
            self._echo = b""
            return False
        if self._buffer.startswith(self._echo):
            del self._buffer[: len(self._echo)]
            self._echo = b""
            return False
        if self._echo.startswith(bytes(self._buffer)):
            return True
        # 回显没有出现（适配器是全双工的或已经消费掉），不再等待
        self._echo = b""
        return False

    @property
    def pending(self) -> int:
        """当前缓冲中待解析的字节数。"""
        return len(self._buffer)


def registers_to_bytes(values: Iterable[int]) -> bytes:
    """把寄存器值列表拼成响应数据段（用于测试与构造帧）。"""
    return b"".join((v & 0xFFFF).to_bytes(2, "big") for v in values)
