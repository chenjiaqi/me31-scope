"""CSV 记录测试。"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from me31_scope import device as dev
from me31_scope.recorder import Recorder, timestamp_text


def _read(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.reader(handle))


def test_timestamp_text_has_millisecond_precision() -> None:
    when = datetime(2026, 10, 13, 14, 10, 0, 123456)
    assert timestamp_text(when) == "2026-10-13 14:10:00.123"


def test_write_header_and_rows(tmp_path: Path) -> None:
    path = tmp_path / "record.csv"
    when = datetime(2026, 10, 13, 14, 10, 0, 123000)
    offsets = (0.0, 8.5, -8.5, 1.0)

    with Recorder(path) as recorder:
        recorder.write_row(when, (25.3, 25.4, 24.9, 25.1), offsets)
        recorder.write_row(when, (25.4, 25.5, 25.0, 25.2), offsets)

    rows = _read(path)
    assert rows[0] == [
        "时间",
        "CH1(℃)",
        "CH2(℃)",
        "CH3(℃)",
        "CH4(℃)",
        "CH1偏移(℃)",
        "CH2偏移(℃)",
        "CH3偏移(℃)",
        "CH4偏移(℃)",
    ]
    assert rows[1] == ["2026-10-13 14:10:00.123", "25.3", "25.4", "24.9", "25.1", "0.0", "8.5", "-8.5", "1.0"]
    assert len(rows) == 3


def test_faulted_channel_is_written_as_empty_cell(tmp_path: Path) -> None:
    path = tmp_path / "record.csv"
    with Recorder(path) as recorder:
        recorder.write_row(datetime(2026, 10, 13), (25.3, None, 24.9, None), (0.0, 0.0, 0.0, 0.0))

    row = _read(path)[1]
    assert row[2] == ""
    assert row[4] == ""


def test_missing_offsets_leave_empty_cells(tmp_path: Path) -> None:
    path = tmp_path / "record.csv"
    with Recorder(path) as recorder:
        recorder.write_row(datetime(2026, 10, 13), (25.3, 25.4, 24.9, 25.1))

    row = _read(path)[1]
    assert row[5:] == ["", "", "", ""]


def test_rows_are_flushed_immediately(tmp_path: Path) -> None:
    path = tmp_path / "record.csv"
    recorder = Recorder(path)
    recorder.open()
    recorder.write_row(datetime(2026, 10, 13), (25.3, 25.4, 24.9, 25.1), None)
    # 未调用 close 也应能从磁盘上读到这一行
    assert len(_read(path)) == 2
    recorder.close()


def test_row_counter(tmp_path: Path) -> None:
    path = tmp_path / "record.csv"
    with Recorder(path) as recorder:
        assert recorder.rows_written == 0
        recorder.write_row(datetime(2026, 10, 13), (25.3, 25.4, 24.9, 25.1), None)
        recorder.write_row(datetime(2026, 10, 13), (25.3, 25.4, 24.9, 25.1), None)
        assert recorder.rows_written == 2


def test_closed_recorder_rejects_writes(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path / "record.csv")
    recorder.open()
    recorder.close()
    assert not recorder.is_open
    try:
        recorder.write_row(datetime(2026, 10, 13), (25.3, 25.4, 24.9, 25.1), None)
    except RuntimeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("关闭后的记录器不应接受写入")


def test_header_uses_device_channel_names(tmp_path: Path) -> None:
    path = tmp_path / "record.csv"
    with Recorder(path) as recorder:
        assert recorder._header()[1:5] == [f"{name}(℃)" for name in dev.CHANNEL_NAMES]
