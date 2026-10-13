"""串口通信层：把 QSerialPort 包成「每秒轮询一次」的 Modbus RTU 主机。

同一时刻只允许一个未完成的事务，因此解析器已知期望的功能码和寄存器个数。
所有串口读写都在主线程的事件循环里完成，不需要额外线程。
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtSerialPort import QSerialPort, QSerialPortInfo

from . import device as dev
from . import rtu

PARITY_MAP = {
    "N": QSerialPort.Parity.NoParity,
    "O": QSerialPort.Parity.OddParity,
    "E": QSerialPort.Parity.EvenParity,
}

# 连续超时达到这个次数才再次报错，避免每秒刷屏
TIMEOUT_REPORT_EVERY = 5


@dataclass(slots=True)
class _Pending:
    kind: str
    fc: int
    count: int
    meta: object = None


def available_ports() -> list[tuple[str, str]]:
    """返回 (端口名, 描述) 列表。"""
    result = []
    for info in QSerialPortInfo.availablePorts():
        description = info.description() or info.manufacturer() or ""
        result.append((info.portName(), description))
    return result


class DeviceClient(QObject):
    """ME31-XDXX0400 的串口客户端。"""

    sample = Signal(object)  # dev.Sample
    offsetsRead = Signal(tuple)  # tuple[float, ...]
    # 通道序号, 写入值, 是否校验一致, 失败原因（成功时为空串）
    writeFinished = Signal(int, float, bool, str)
    connectionChanged = Signal(bool)
    errorOccurred = Signal(str)
    frameLogged = Signal(str, str)  # 方向（TX/RX）, 十六进制文本

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._port = QSerialPort(self)
        self._port.errorOccurred.connect(self._on_port_error)
        self._port.readyRead.connect(self._on_ready_read)

        self._parser = rtu.RtuParser()
        self._pending: _Pending | None = None
        self._deferred: deque[tuple[str, bytes, int, int, object]] = deque()
        self._slave = dev.DEFAULT_SLAVE
        self._offsets: tuple[float, ...] | None = None
        self._consecutive_timeouts = 0

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(dev.POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._on_poll_tick)

        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.setInterval(dev.RESPONSE_TIMEOUT_MS)
        self._timeout_timer.timeout.connect(self._on_response_timeout)

    # ------------------------------------------------------------------ 连接

    @property
    def is_open(self) -> bool:
        return self._port.isOpen()

    @property
    def is_polling(self) -> bool:
        return self._poll_timer.isActive()

    @property
    def offsets(self) -> tuple[float, ...] | None:
        """最近一次读到的偏移值，尚未读取过时为 None。"""
        return self._offsets

    def open(self, port_name: str, baudrate: int, parity: str, slave: int) -> bool:
        """打开串口并启动轮询。"""
        self.close()

        self._slave = slave
        self._port.setPortName(port_name)
        self._port.setBaudRate(baudrate)
        self._port.setDataBits(QSerialPort.DataBits.Data8)
        self._port.setStopBits(QSerialPort.StopBits.OneStop)
        self._port.setParity(PARITY_MAP.get(parity, QSerialPort.Parity.NoParity))
        self._port.setFlowControl(QSerialPort.FlowControl.NoFlowControl)

        if not self._port.open(QSerialPort.OpenModeFlag.ReadWrite):
            self.errorOccurred.emit(f"打开 {port_name} 失败：{self._port.errorString()}")
            self.connectionChanged.emit(False)
            return False

        self._consecutive_timeouts = 0
        self._poll_timer.start()
        self.connectionChanged.emit(True)
        self.request_offsets()
        return True

    def close(self) -> None:
        """停止轮询并关闭串口。"""
        self._poll_timer.stop()
        self._timeout_timer.stop()
        self._pending = None
        self._deferred.clear()
        self._parser.reset()
        self._offsets = None
        if self._port.isOpen():
            self._port.close()
            self.connectionChanged.emit(False)

    # -------------------------------------------------------------- 对外命令

    def request_offsets(self) -> None:
        """读取 4 个通道的偏移值。"""
        request = rtu.build_read_holding(self._slave, dev.OFFSET_START, dev.OFFSET_COUNT)
        self._submit("offsets", request, rtu.FC_READ_HOLDING, dev.OFFSET_COUNT)

    def write_offset(self, channel: int, celsius: float) -> None:
        """写入单个通道的偏移值（通道序号从 0 开始）。"""
        if not 0 <= channel < dev.CHANNELS:
            raise ValueError(f"通道序号超出范围: {channel}")
        raw = dev.encode_offset(celsius)
        request = rtu.build_write_single(self._slave, dev.OFFSET_START + channel, raw)
        self._submit("write", request, rtu.FC_WRITE_SINGLE, 0, meta=(channel, celsius))

    def write_offsets(self, values: Sequence[float]) -> None:
        """逐个通道写入偏移值。"""
        for channel, celsius in enumerate(values[: dev.CHANNELS]):
            self.write_offset(channel, celsius)

    def reboot(self) -> None:
        """重启设备。"""
        request = rtu.build_write_single(self._slave, dev.REBOOT_REG, 1)
        self._submit("reboot", request, rtu.FC_WRITE_SINGLE, 0)

    # ------------------------------------------------------------ 事务调度

    def _on_poll_tick(self) -> None:
        if self._pending is not None or self._deferred:
            return  # 上一次还没回来，跳过这一拍
        request = rtu.build_read_input(self._slave, dev.TEMP_START, dev.TEMP_COUNT)
        # 轮询请求不排队：错过一拍就等下一秒
        self._start("temps", request, rtu.FC_READ_INPUT, dev.TEMP_COUNT)

    def _submit(
        self,
        kind: str,
        request: bytes,
        fc: int,
        count: int,
        meta: object = None,
    ) -> None:
        """提交用户发起的命令，串口忙时排队等待。"""
        if self._pending is not None or self._deferred:
            self._deferred.append((kind, request, fc, count, meta))
            return
        self._start(kind, request, fc, count, meta)

    def _start(self, kind: str, request: bytes, fc: int, count: int, meta: object = None) -> None:
        if not self._port.isOpen():
            # 串口已关闭（例如刚被拔掉），丢弃这条命令
            self._pending = None
            self._deferred.clear()
            return
        self._parser.reset()
        self._parser.expect_echo(request)
        self._pending = _Pending(kind, fc, count, meta)
        self._port.write(request)
        self._timeout_timer.start()
        self.frameLogged.emit("TX", request.hex(" ").upper())

    def _finish(self) -> None:
        self._pending = None
        self._timeout_timer.stop()
        self._parser.reset()
        if self._deferred:
            kind, request, fc, count, meta = self._deferred.popleft()
            self._start(kind, request, fc, count, meta)

    # ------------------------------------------------------------ 串口事件

    def _on_ready_read(self) -> None:
        data = bytes(self._port.readAll().data())
        if not data:
            return
        self.frameLogged.emit("RX", data.hex(" ").upper())
        self._parser.feed(data)
        self._drain()

    def _drain(self) -> None:
        if self._pending is None:
            # 没有在等响应，说明是上一轮的残留数据
            self._parser.reset()
            return
        try:
            frame = self._parser.pop_response(
                self._slave, self._pending.fc, self._pending.count
            )
        except rtu.ModbusException as exc:
            pending = self._pending
            self._finish()
            if pending.kind == "write" and pending.meta is not None:
                channel, celsius = pending.meta
                self.writeFinished.emit(channel, celsius, False, f"设备拒绝写入（{exc}）")
            self.errorOccurred.emit(str(exc))
            return

        if frame is None:
            return

        pending = self._pending
        self._finish()
        self._consecutive_timeouts = 0
        self._handle_frame(pending, frame)

    def _handle_frame(self, pending: _Pending, frame: bytes) -> None:
        if pending.kind == "temps":
            raws = rtu.parse_registers(frame)
            if len(raws) != dev.TEMP_COUNT:
                self.errorOccurred.emit(f"温度响应寄存器个数异常: {len(raws)}")
                return
            self.sample.emit(dev.Sample(time.time(), dev.decode_temps(raws)))

        elif pending.kind == "offsets":
            raws = rtu.parse_registers(frame)
            if len(raws) != dev.OFFSET_COUNT:
                self.errorOccurred.emit(f"偏移值响应寄存器个数异常: {len(raws)}")
                return
            self._offsets = dev.decode_offsets(raws)
            self.offsetsRead.emit(self._offsets)

        elif pending.kind == "write":
            channel, celsius = pending.meta  # type: ignore[misc]
            address = int.from_bytes(frame[2:4], "big")
            raw = int.from_bytes(frame[4:6], "big")
            # 寄存器值是 16 位无符号，encode_offset 返回有符号，比较前统一为无符号
            expected_raw = dev.encode_offset(celsius) & 0xFFFF
            expected_address = dev.OFFSET_START + channel

            if address != expected_address:
                reason = f"响应寄存器地址 0x{address:04X}，与请求的 0x{expected_address:04X} 不符"
            elif raw != expected_raw:
                reason = f"响应值 0x{raw:04X}，与请求的 0x{expected_raw:04X} 不符"
            else:
                reason = ""
            self.writeFinished.emit(channel, celsius, not reason, reason)

        elif pending.kind == "reboot":
            self.errorOccurred.emit("已发送重启命令，设备将短暂离线")

    def _on_response_timeout(self) -> None:
        if self._pending is None:
            return
        kind = self._pending.kind
        meta = self._pending.meta
        self._finish()
        self._consecutive_timeouts += 1

        if kind == "write" and meta is not None:
            channel, celsius = meta
            self.writeFinished.emit(
                channel, celsius, False, f"设备在 {dev.RESPONSE_TIMEOUT_MS} ms 内没有响应"
            )

        if self._consecutive_timeouts == 1 or self._consecutive_timeouts % TIMEOUT_REPORT_EVERY == 0:
            self.errorOccurred.emit(
                f"设备无响应（连续 {self._consecutive_timeouts} 次），请检查端口、"
                f"波特率、校验位和从机地址（当前 {self._slave}）"
            )

    def _on_port_error(self, error: QSerialPort.SerialPortError) -> None:
        if error == QSerialPort.SerialPortError.NoError:
            return
        fatal = {
            QSerialPort.SerialPortError.ResourceError,
            QSerialPort.SerialPortError.DeviceNotFoundError,
            QSerialPort.SerialPortError.PermissionError,
            QSerialPort.SerialPortError.OpenError,
        }
        if error in fatal:
            self.errorOccurred.emit(f"串口错误：{self._port.errorString()}")
            self.close()
