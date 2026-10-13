"""串口客户端集成测试。

用 pty 伪终端充当设备，这样不用接真机也能覆盖完整链路：
组帧 → 串口收发 → 半双工回显 → 解帧 → 换算 → 信号。

Windows 上没有 pty，整个模块跳过。
"""

from __future__ import annotations

import os
import pty
import time

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="需要 pty 伪终端")

# 必须在创建 QApplication 之前设置，否则没有显示器会直接崩
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from temperature_acq import rtu  # noqa: E402
from temperature_acq.client import DeviceClient  # noqa: E402

TEMP_RAWS = [0x012C, 0x00C8, 0xFC19, 0x00FA]
OFFSET_RAWS = [0x0000, 0x0055, 0xFFAB, 0xFFFF]

REQUEST_LEN = 8  # FC03/04/06 请求都是 8 字节


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


class FakeDevice:
    """pty 伪终端充当设备。

    echo=True 模拟会把请求回显出来的半双工适配器（部分 USB-RS485 如此）；
    echo=False 模拟只回设备响应的适配器（更常见，也是 FC06 曾经踩坑的场景）。
    """

    def __init__(self, echo: bool = True) -> None:
        self.master, slave = pty.openpty()
        os.set_blocking(self.master, False)
        self.port_name = os.ttyname(slave)
        self.echo = echo
        self.silent = False
        self.requests: list[bytes] = []

    def pump(self, app: QApplication, seconds: float) -> None:
        """在指定时间内驱动设备与 Qt 事件循环。"""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._serve()
            app.processEvents()
            time.sleep(0.005)

    def _serve(self) -> None:
        try:
            data = os.read(self.master, 4096)
        except (BlockingIOError, OSError):
            return
        if not data or self.silent:
            return
        if self.echo:
            os.write(self.master, data)  # 回显
        for start in range(0, len(data), REQUEST_LEN):
            request = data[start : start + REQUEST_LEN]
            if len(request) < 6:
                continue
            self.requests.append(request)
            response = self._respond(request)
            if response is not None:
                os.write(self.master, response)

    @staticmethod
    def _respond(request: bytes) -> bytes | None:
        slave, fc = request[0], request[1]
        count = int.from_bytes(request[4:6], "big")
        if fc == rtu.FC_READ_INPUT:
            raws = TEMP_RAWS[:count]
        elif fc == rtu.FC_READ_HOLDING:
            raws = OFFSET_RAWS[:count]
        elif fc == rtu.FC_WRITE_SINGLE:
            return request  # FC06 回显请求即为成功
        else:
            return None
        body = bytes([count * 2]) + rtu.registers_to_bytes(raws)
        return rtu.append_crc(bytes([slave, fc]) + body)

    def close(self) -> None:
        os.close(self.master)


@pytest.fixture
def device() -> FakeDevice:
    fake = FakeDevice()
    yield fake
    fake.close()


@pytest.fixture
def client(app: QApplication, device: FakeDevice) -> DeviceClient:
    client = DeviceClient()
    yield client
    client.close()


def _collect(client: DeviceClient) -> dict[str, list]:
    sink: dict[str, list] = {
        "samples": [],
        "offsets": [],
        "errors": [],
        "writes": [],
        "reasons": [],
    }
    client.sample.connect(sink["samples"].append)
    client.offsetsRead.connect(sink["offsets"].append)
    client.errorOccurred.connect(sink["errors"].append)

    def on_write(channel: int, value: float, ok: bool, reason: str) -> None:
        sink["writes"].append((channel, value, ok))
        sink["reasons"].append(reason)

    client.writeFinished.connect(on_write)
    return sink


def test_open_starts_polling(app: QApplication, device: FakeDevice, client: DeviceClient) -> None:
    assert client.open(device.port_name, 9600, "N", 1) is True
    assert client.is_open
    assert client.is_polling


def test_reads_temperatures_and_offsets(
    app: QApplication, device: FakeDevice, client: DeviceClient
) -> None:
    sink = _collect(client)
    client.open(device.port_name, 9600, "N", 1)
    device.pump(app, 1.3)

    assert sink["samples"], "应至少收到一个采样"
    # 0xFC19 是传感器未接入，应换算为 None
    assert sink["samples"][-1].temps == (30.0, 20.0, None, 25.0)
    # 偏移值：0x0055=8.5, 0xFFAB=-8.5, 0xFFFF=-0.1
    assert sink["offsets"][-1] == (0.0, 8.5, -8.5, -0.1)
    assert sink["errors"] == []


def test_poll_interval_is_one_second(
    app: QApplication, device: FakeDevice, client: DeviceClient
) -> None:
    sink = _collect(client)
    client.open(device.port_name, 9600, "N", 1)
    device.pump(app, 2.4)
    # 每秒一拍，2.4 秒内应有 2~3 个采样
    assert 2 <= len(sink["samples"]) <= 3, len(sink["samples"])


def test_write_positive_offset(
    app: QApplication, device: FakeDevice, client: DeviceClient
) -> None:
    sink = _collect(client)
    client.open(device.port_name, 9600, "N", 1)
    device.pump(app, 1.2)

    client.write_offset(1, 8.5)
    device.pump(app, 0.6)

    assert sink["writes"] == [(1, 8.5, True)]
    # 下发帧应写到 CH2 对应的偏移寄存器
    assert any(r[:2] == bytes([0x01, 0x06]) and r[2:4] == (0x2328 + 1).to_bytes(2, "big")
               for r in device.requests)


def test_write_negative_offset_is_reported_as_success(
    app: QApplication, device: FakeDevice, client: DeviceClient
) -> None:
    """回归测试：FC06 响应里的寄存器值是无符号的，负偏移不能误判为写入失败。"""
    sink = _collect(client)
    client.open(device.port_name, 9600, "N", 1)
    device.pump(app, 1.2)

    client.write_offset(2, -3.2)
    device.pump(app, 0.6)

    assert sink["writes"] == [(2, -3.2, True)]
    assert any(r[2:4] == (0x2328 + 2).to_bytes(2, "big") and r[4:6] == b"\xff\xe0"
               for r in device.requests)


def test_write_all_offsets_sends_every_channel(
    app: QApplication, device: FakeDevice, client: DeviceClient
) -> None:
    sink = _collect(client)
    client.open(device.port_name, 9600, "N", 1)
    device.pump(app, 1.2)

    client.write_offsets([0.5, -0.5, 10.0, -10.0])
    device.pump(app, 1.5)

    assert sorted(sink["writes"]) == [
        (0, 0.5, True),
        (1, -0.5, True),
        (2, 10.0, True),
        (3, -10.0, True),
    ]


def test_timeout_reports_failure_and_error(
    app: QApplication, device: FakeDevice, client: DeviceClient
) -> None:
    sink = _collect(client)
    client.open(device.port_name, 9600, "N", 1)
    device.pump(app, 1.2)

    device.silent = True
    client.write_offset(3, 1.0)
    device.pump(app, 1.0)

    assert sink["writes"] == [(3, 1.0, False)]
    assert sink["errors"], "超时后应上报错误"
    # 失败原因要具体，便于现场排查
    assert "没有响应" in sink["reasons"][-1], sink["reasons"]


def test_write_offset_without_adapter_echo(app: QApplication) -> None:
    """回归测试：FC06 的成功响应本身就等于请求，不能被回显剥离逻辑吃掉。

    只回响应、不回显请求的适配器（真实 USB-RS485 常见）下，写入必须成功。
    修复前这里会判定写入失败。
    """
    device = FakeDevice(echo=False)
    client = DeviceClient()
    try:
        sink = _collect(client)
        client.open(device.port_name, 9600, "N", 1)
        device.pump(app, 1.2)

        client.write_offset(0, 0.0)
        client.write_offset(2, -3.2)
        device.pump(app, 1.6)

        assert sink["writes"] == [(0, 0.0, True), (2, -3.2, True)], sink["writes"]
        assert sink["reasons"] == ["", ""], sink["reasons"]
        assert sink["errors"] == []
        # 回读校验也应在不回显的情况下拿到偏移值
        assert sink["offsets"][-1] == (0.0, 8.5, -8.5, -0.1)
    finally:
        client.close()
        device.close()


def test_close_stops_polling_and_clears_state(
    app: QApplication, device: FakeDevice, client: DeviceClient
) -> None:
    changes = []
    client.connectionChanged.connect(changes.append)
    client.open(device.port_name, 9600, "N", 1)
    device.pump(app, 1.2)
    client.close()

    assert changes == [True, False]
    assert not client.is_open
    assert not client.is_polling


def test_commands_are_ignored_after_close(
    app: QApplication, device: FakeDevice, client: DeviceClient
) -> None:
    sink = _collect(client)
    client.close()
    client.write_offset(0, 1.0)
    device.pump(app, 0.6)
    assert sink["writes"] == []
    assert sink["errors"] == []
