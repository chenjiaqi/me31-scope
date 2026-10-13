"""把采样写成 CSV。不依赖 Qt。"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from types import TracebackType

from .device import CHANNEL_NAMES


def timestamp_text(when: datetime) -> str:
    """精确到毫秒的时间文本。"""
    return when.strftime("%Y-%m-%d %H:%M:%S.") + f"{when.microsecond // 1000:03d}"


class Recorder:
    """逐行追加写入的 CSV 记录器，每行都 flush，中途崩溃不丢已写数据。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._file = None
        self._writer: csv.writer | None = None
        self.rows_written = 0

    @property
    def is_open(self) -> bool:
        return self._file is not None

    def open(self) -> None:
        if self.is_open:
            raise RuntimeError("记录器已经打开")
        self._file = self.path.open("w", encoding="utf-8-sig", newline="")
        self._writer = csv.writer(self._file)
        self._writer.writerow(self._header())
        self._file.flush()
        self.rows_written = 0

    def _header(self) -> list[str]:
        temps = [f"{name}(℃)" for name in CHANNEL_NAMES]
        offsets = [f"{name}偏移(℃)" for name in CHANNEL_NAMES]
        return ["时间", *temps, *offsets]

    def write_row(
        self,
        when: datetime,
        temps: Sequence[float | None],
        offsets: Sequence[float | None] | None = None,
    ) -> None:
        """写入一行。温度为 None 的通道留空单元格。"""
        if not self.is_open or self._writer is None or self._file is None:
            raise RuntimeError("记录器尚未打开")

        row: list[str] = [timestamp_text(when)]
        row.extend("" if t is None else f"{t:.1f}" for t in temps)
        if offsets is None:
            row.extend([""] * len(CHANNEL_NAMES))
        else:
            row.extend("" if o is None else f"{o:.1f}" for o in offsets)

        self._writer.writerow(row)
        self._file.flush()
        self.rows_written += 1

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
        self._file = None
        self._writer = None

    def __enter__(self) -> "Recorder":
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
