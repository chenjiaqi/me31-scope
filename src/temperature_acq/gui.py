"""主界面：左侧参数与控制，右侧实时温度曲线。"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import device as dev
from .client import DeviceClient, available_ports
from .recorder import Recorder

CHANNEL_COLORS = ("#e6194b", "#3cb44b", "#4363d8", "#f58231")

# 曲线历史长度上限，按最大显示时长（120 分钟）留出容量
HISTORY_LIMIT = 120 * 60
DEFAULT_WINDOW_MINUTES = 5

# Y 轴自适应：上下留白比例，以及最小跨度（℃）
# 最小跨度用于四个通道读数完全相同时避免量程塌缩成一条线
Y_PADDING = 0.08
Y_MIN_SPAN = 1.0

LOG_MAX_BLOCKS = 500


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ME31-XDXX0400 四通道温度采集")
        self.resize(1200, 720)

        self._recorder: Recorder | None = None
        self._offsets: tuple[float, ...] | None = None
        self._verifying: dict[int, float] = {}

        self._times: deque[float] = deque(maxlen=HISTORY_LIMIT)
        self._values: list[deque[float]] = [
            deque(maxlen=HISTORY_LIMIT) for _ in range(dev.CHANNELS)
        ]

        self.client = DeviceClient(self)
        self.client.sample.connect(self._on_sample)
        self.client.offsetsRead.connect(self._on_offsets_read)
        self.client.writeFinished.connect(self._on_write_finished)
        self.client.connectionChanged.connect(self._on_connection_changed)
        self.client.errorOccurred.connect(self._on_error)
        self.client.frameLogged.connect(self._log_frame)

        self._build_ui()
        self._refresh_ports()
        self._set_connected(False)

    # ------------------------------------------------------------------ 界面

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.addWidget(self._build_left_panel(), 0)
        layout.addWidget(self._build_right_panel(), 1)

        self.status = QLabel("未连接")
        self.status.setMinimumWidth(220)
        self.status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.statusBar().addPermanentWidget(self.status)

    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(360)
        layout = QVBoxLayout(panel)
        layout.addWidget(self._build_serial_group())
        layout.addWidget(self._build_offset_group())
        layout.addWidget(self._build_record_group())
        layout.addStretch(1)
        return panel

    def _build_serial_group(self) -> QGroupBox:
        group = QGroupBox("串口设置")
        form = QFormLayout(group)

        port_row = QHBoxLayout()
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(150)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self._refresh_ports)
        port_row.addWidget(self.port_combo, 1)
        port_row.addWidget(refresh)
        form.addRow("串口", port_row)

        self.baud_combo = QComboBox()
        for baud in dev.BAUDRATE_CHOICES:
            self.baud_combo.addItem(str(baud), baud)
        self.baud_combo.setCurrentText(str(dev.DEFAULT_BAUDRATE))
        form.addRow("波特率", self.baud_combo)

        self.parity_combo = QComboBox()
        for code, label in (("N", "无校验"), ("O", "奇校验"), ("E", "偶校验")):
            self.parity_combo.addItem(label, code)
        form.addRow("校验位", self.parity_combo)

        frame = QLabel("数据位 8 / 停止位 1（固定）")
        frame.setStyleSheet("color: gray;")
        form.addRow("帧格式", frame)

        self.slave_spin = QSpinBox()
        self.slave_spin.setRange(1, 247)
        self.slave_spin.setValue(dev.DEFAULT_SLAVE)
        form.addRow("从机地址", self.slave_spin)

        self.connect_button = QPushButton("连接")
        self.connect_button.clicked.connect(self._toggle_connection)
        form.addRow(self.connect_button)
        return group

    def _build_offset_group(self) -> QGroupBox:
        group = QGroupBox("温度偏移值（设备内部自动叠加到温度上）")
        self.offset_group = group
        grid = QGridLayout(group)
        grid.addWidget(QLabel("通道"), 0, 0)
        grid.addWidget(QLabel("偏移(℃)"), 0, 1)
        grid.addWidget(QLabel("操作"), 0, 2)

        self.offset_spins: list[QDoubleSpinBox] = []
        for channel in range(dev.CHANNELS):
            spin = QDoubleSpinBox()
            spin.setRange(dev.OFFSET_CELSIUS_MIN, dev.OFFSET_CELSIUS_MAX)
            spin.setSingleStep(0.1)
            spin.setDecimals(1)
            spin.setSuffix(" ℃")
            self.offset_spins.append(spin)

            row = channel + 1
            grid.addWidget(QLabel(dev.CHANNEL_NAMES[channel]), row, 0)
            grid.addWidget(spin, row, 1)
            button = QPushButton("写入")
            button.clicked.connect(lambda _=False, ch=channel: self._write_offset(ch))
            grid.addWidget(button, row, 2)

        buttons = QHBoxLayout()
        self.read_offsets_button = QPushButton("读取全部")
        self.read_offsets_button.clicked.connect(self.client.request_offsets)
        self.write_offsets_button = QPushButton("写入全部")
        self.write_offsets_button.clicked.connect(self._write_all_offsets)
        self.reboot_button = QPushButton("重启设备")
        self.reboot_button.clicked.connect(self._reboot)
        buttons.addWidget(self.read_offsets_button)
        buttons.addWidget(self.write_offsets_button)
        buttons.addWidget(self.reboot_button)
        grid.addLayout(buttons, dev.CHANNELS + 1, 0, 1, 3)
        return group

    def _build_record_group(self) -> QGroupBox:
        group = QGroupBox("数据记录")
        self.record_group = group
        layout = QVBoxLayout(group)

        buttons = QHBoxLayout()
        self.start_record_button = QPushButton("开始记录")
        self.start_record_button.clicked.connect(self._start_recording)
        self.stop_record_button = QPushButton("停止记录")
        self.stop_record_button.clicked.connect(self._stop_recording)
        self.stop_record_button.setEnabled(False)
        buttons.addWidget(self.start_record_button)
        buttons.addWidget(self.stop_record_button)
        layout.addLayout(buttons)

        self.record_path_label = QLabel("未在记录")
        self.record_path_label.setWordWrap(True)
        self.record_path_label.setStyleSheet("color: gray;")
        layout.addWidget(self.record_path_label)
        return group

    def _build_right_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)

        values = QGroupBox("当前温度")
        values_layout = QHBoxLayout(values)
        self.value_labels: list[QLabel] = []
        for channel in range(dev.CHANNELS):
            box = QVBoxLayout()
            name = QLabel(dev.CHANNEL_NAMES[channel])
            name.setAlignment(Qt.AlignmentFlag.AlignCenter)
            value = QLabel("--")
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            value.setStyleSheet(
                f"font-size: 26px; font-weight: bold; color: {CHANNEL_COLORS[channel]};"
            )
            box.addWidget(name)
            box.addWidget(value)
            values_layout.addLayout(box)
            self.value_labels.append(value)
        layout.addWidget(values)

        window_row = QHBoxLayout()
        window_row.addWidget(QLabel("显示最近"))
        self.window_spin = QSpinBox()
        self.window_spin.setRange(1, 120)
        self.window_spin.setValue(DEFAULT_WINDOW_MINUTES)
        self.window_spin.setSuffix(" 分钟")
        self.window_spin.valueChanged.connect(lambda _=0: self._update_plot())
        window_row.addWidget(self.window_spin)
        window_row.addStretch(1)
        layout.addLayout(window_row)

        pg.setConfigOptions(antialias=True)
        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        self.plot.setBackground("w")
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.plot.setLabel("left", "温度", units="℃")
        self.plot.addLegend()
        self.curves = [
            self.plot.plot(
                [],
                [],
                pen=pg.mkPen(color, width=2),
                name=dev.CHANNEL_NAMES[channel],
                connect="finite",
            )
            for channel, color in enumerate(CHANNEL_COLORS)
        ]
        for curve in self.curves:
            curve.setClipToView(True)
        layout.addWidget(self.plot, 1)

        log_group = QGroupBox("通信日志")
        log_layout = QVBoxLayout(log_group)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(LOG_MAX_BLOCKS)
        self.log_view.setFixedHeight(110)
        log_layout.addWidget(self.log_view)
        layout.addWidget(log_group)
        return panel

    # ------------------------------------------------------------------ 交互

    def _refresh_ports(self) -> None:
        selected = self.port_combo.currentData()
        self.port_combo.clear()
        for name, description in available_ports():
            self.port_combo.addItem(f"{name}  {description}".strip(), name)
        if selected is not None:
            index = self.port_combo.findData(selected)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)

    def _toggle_connection(self) -> None:
        if self.client.is_open:
            self.client.close()
            return
        port_name = self.port_combo.currentData()
        if not port_name:
            self._warn("没有可用的串口，请插入 USB 转 RS485 设备后点击「刷新」")
            return
        self.client.open(
            port_name,
            self.baud_combo.currentData(),
            self.parity_combo.currentData(),
            self.slave_spin.value(),
        )

    def _set_connected(self, connected: bool) -> None:
        self.connect_button.setText("断开" if connected else "连接")
        self.port_combo.setEnabled(not connected)
        self.baud_combo.setEnabled(not connected)
        self.parity_combo.setEnabled(not connected)
        self.slave_spin.setEnabled(not connected)

        # 整个分组一起启用/禁用，子控件跟随
        self.offset_group.setEnabled(connected)

        self.start_record_button.setEnabled(connected and self._recorder is None)
        if not connected and self._recorder is not None:
            self._stop_recording()

        self.status.setText("已连接" if connected else "未连接")
        self.status.setStyleSheet(
            "color: #2a7; font-weight: bold;" if connected else "color: gray;"
        )

    # -------------------------------------------------------------- 设备事件

    def _on_sample(self, sample: dev.Sample) -> None:
        self._times.append(sample.time)
        for channel, value in enumerate(sample.temps):
            self._values[channel].append(np.nan if value is None else value)
            self.value_labels[channel].setText(dev.format_temp(value))
        self._update_plot()

        if self._recorder is not None and self._recorder.is_open:
            self._recorder.write_row(
                datetime.fromtimestamp(sample.time), sample.temps, self._offsets
            )
            self.record_path_label.setText(
                f"{self._recorder.path}\n已记录 {self._recorder.rows_written} 行"
            )

    def _on_offsets_read(self, offsets: tuple) -> None:
        self._offsets = tuple(offsets)
        for channel, spin in enumerate(self.offset_spins):
            spin.blockSignals(True)
            spin.setValue(offsets[channel])
            spin.blockSignals(False)

        for channel, expected in self._verifying.items():
            actual = offsets[channel]
            if abs(actual - expected) > 0.05:
                self._warn(
                    f"{dev.CHANNEL_NAMES[channel]} 回读为 {actual:.1f}℃，与写入值 "
                    f"{expected:.1f}℃ 不一致，请点击「重启设备」后再试"
                )
            else:
                self._log(f"{dev.CHANNEL_NAMES[channel]} 回读校验通过：{actual:.1f}℃")
        self._verifying.clear()

    def _on_write_finished(
        self, channel: int, celsius: float, matched: bool, reason: str
    ) -> None:
        if not matched:
            self._warn(f"{dev.CHANNEL_NAMES[channel]} 偏移值写入失败：{reason}")
            return
        self._log(f"{dev.CHANNEL_NAMES[channel]} 偏移值已写入 {celsius:.1f}℃，正在回读校验…")
        self._verifying[channel] = celsius
        self.client.request_offsets()

    def _on_connection_changed(self, connected: bool) -> None:
        self._set_connected(connected)
        if not connected:
            # 断开时丢弃未完成的回读校验，避免重连后误报不一致
            self._verifying.clear()
            self._offsets = None
            self._times.clear()
            for values in self._values:
                values.clear()
            for label in self.value_labels:
                label.setText("--")
            self._update_plot()

    def _on_error(self, message: str) -> None:
        self._log(f"[错误] {message}")

    def _write_offset(self, channel: int) -> None:
        self.client.write_offset(channel, self.offset_spins[channel].value())

    def _write_all_offsets(self) -> None:
        self.client.write_offsets([spin.value() for spin in self.offset_spins])

    def _reboot(self) -> None:
        answer = QMessageBox.question(
            self,
            "重启设备",
            "确定要重启设备吗？设备会短暂离线，采集和记录将中断几秒。",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.client.reboot()

    # ------------------------------------------------------------------ 记录

    def _start_recording(self) -> None:
        default = Path.home() / f"temp_{datetime.now():%Y%m%d_%H%M%S}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "保存温度记录", str(default), "CSV 文件 (*.csv)")
        if not path:
            return
        recorder = Recorder(path)
        try:
            recorder.open()
        except OSError as exc:
            self._warn(f"无法创建文件：{exc}")
            return
        self._recorder = recorder
        self.start_record_button.setEnabled(False)
        self.stop_record_button.setEnabled(True)
        self.record_path_label.setText(f"{recorder.path}\n已记录 0 行")
        self.record_path_label.setStyleSheet("color: black;")
        self._log(f"开始记录到 {recorder.path}")

    def _stop_recording(self) -> None:
        if self._recorder is None:
            return
        rows = self._recorder.rows_written
        path = self._recorder.path
        self._recorder.close()
        self._recorder = None
        self.start_record_button.setEnabled(self.client.is_open)
        self.stop_record_button.setEnabled(False)
        self.record_path_label.setText(f"未在记录（上次 {path}，共 {rows} 行）")
        self.record_path_label.setStyleSheet("color: gray;")
        self._log(f"停止记录，共写入 {rows} 行")

    # ------------------------------------------------------------------ 曲线

    def _update_plot(self) -> None:
        if not self._times:
            return
        times = np.fromiter(self._times, dtype=float, count=len(self._times))
        series = [
            np.fromiter(self._values[channel], dtype=float, count=len(self._values[channel]))
            for channel in range(dev.CHANNELS)
        ]
        for curve, values in zip(self.curves, series):
            curve.setData(times, values, connect="finite")

        window = self.window_spin.value() * 60
        # 数据还没填满整个窗口时锚定在首个采样点上，曲线自左向右生长；
        # 填满后窗口整体左移，最新点始终贴右边缘。两段在交界处连续。
        start = times[0] if times[-1] - times[0] < window else times[-1] - window
        self.plot.setXRange(start, start + window, padding=0)
        self._fit_y_range(times, series, start)

    def _fit_y_range(self, times: np.ndarray, series: list[np.ndarray], start: float) -> None:
        """Y 轴按当前可见窗口内的数据自适应；窗口内全是断线时保持原量程。"""
        low: float | None = None
        high: float | None = None
        for values in series:
            chunk = values[times >= start]
            chunk = chunk[np.isfinite(chunk)]
            if chunk.size == 0:
                continue
            low = float(chunk.min()) if low is None else min(low, float(chunk.min()))
            high = float(chunk.max()) if high is None else max(high, float(chunk.max()))

        if low is None or high is None:
            return
        if high - low < Y_MIN_SPAN:
            center = (low + high) / 2
            low, high = center - Y_MIN_SPAN / 2, center + Y_MIN_SPAN / 2
        self.plot.setYRange(low, high, padding=Y_PADDING)

    # ------------------------------------------------------------------ 日志

    def _log(self, message: str) -> None:
        self.log_view.appendPlainText(f"{datetime.now():%H:%M:%S}  {message}")

    def _log_frame(self, direction: str, payload: str) -> None:
        self._log(f"{direction}  {payload}")

    def _warn(self, message: str) -> None:
        self._log(f"[警告] {message}")
        QMessageBox.warning(self, "提示", message)

    # ------------------------------------------------------------------ 收尾

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._stop_recording()
        self.client.close()
        super().closeEvent(event)
