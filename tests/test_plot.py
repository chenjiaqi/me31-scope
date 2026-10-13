"""曲线坐标轴行为测试（无显示器，offscreen 平台）。

覆盖两件事：
1. X 轴：数据未填满窗口时锚定首个采样点、自左向右生长；填满后整体左移。
2. Y 轴：按可见窗口内的数据自适应，且有最小跨度兜底。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

# 必须在创建 QApplication 之前设置，否则没有显示器会直接崩
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from me31_scope import device as dev  # noqa: E402
from me31_scope.gui import Y_MIN_SPAN, Y_PADDING, MainWindow  # noqa: E402

T0 = 1_700_000_000.0
WINDOW = 300.0  # 与 DEFAULT_WINDOW_MINUTES = 5 对应
NORMAL = (25.0, 25.1, 25.2, 25.3)


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app: QApplication) -> Iterator[MainWindow]:
    win = MainWindow()
    assert win.window_spin.value() * 60 == WINDOW
    yield win
    win.close()


def feed(window: MainWindow, samples: list[tuple]) -> None:
    """从 T0 起按 1 秒间隔喂入采样，每个元素是一组 4 通道温度。

    每次调用都从 T0 重新计时，所以一个用例里只应调用一次。
    """
    for index, temps in enumerate(samples):
        window._on_sample(dev.Sample(T0 + index * 1.0, temps))


def x_range(window: MainWindow) -> tuple[float, float]:
    return window.plot.viewRange()[0]


def y_range(window: MainWindow) -> tuple[float, float]:
    return window.plot.viewRange()[1]


def test_curve_grows_rightwards_before_window_is_full(window: MainWindow) -> None:
    feed(window, [NORMAL] * 3)

    # 只有 T0..T0+2 三个点，远未填满 300 秒，X 轴仍锚定在 T0
    assert x_range(window) == [T0, T0 + WINDOW]


def test_x_range_starts_anchored_with_a_single_sample(window: MainWindow) -> None:
    feed(window, [NORMAL])

    assert x_range(window) == [T0, T0 + WINDOW]


def test_window_scrolls_after_becoming_full(window: MainWindow) -> None:
    count = int(WINDOW) + 2  # 最末点距 T0 有 301 秒，超过窗口宽度
    feed(window, [NORMAL] * count)

    last = T0 + (count - 1)
    assert last - T0 > WINDOW
    assert x_range(window) == [last - WINDOW, last]


def test_x_range_hands_over_without_a_jump(window: MainWindow) -> None:
    """填满与滚动两段在交界处必须首尾相接，不能跳变。"""
    feed(window, [NORMAL] * int(WINDOW))
    assert x_range(window) == [T0, T0 + WINDOW]  # 生长阶段：刚好填满

    # 再进一个点，时间跨度首次达到窗口宽度，起点仍是 T0 —— 无跳变
    feed(window, [NORMAL] * (int(WINDOW) + 1))
    assert x_range(window) == [T0, T0 + WINDOW]

    # 再进一个点，超过窗口宽度，起点开始按步长推进
    feed(window, [NORMAL] * (int(WINDOW) + 2))
    assert x_range(window) == [T0 + 1, T0 + WINDOW + 1]


def test_y_range_adapts_to_visible_data(window: MainWindow) -> None:
    feed(window, [(20.0, None, None, None), (30.0, None, None, None)])

    low, high = y_range(window)
    # 数据 20..30，上下各留白 8% -> 19.2 .. 30.8
    assert low == pytest.approx(20.0 - 10.0 * Y_PADDING, abs=0.05)
    assert high == pytest.approx(30.0 + 10.0 * Y_PADDING, abs=0.05)


def test_y_range_ignores_values_that_scrolled_out(window: MainWindow) -> None:
    """早期的高温点滚出窗口后，不应再撑大量程。"""
    feed(window, [(80.0, None, None, None)] + [(25.0, None, None, None)] * (int(WINDOW) + 1))

    low, high = y_range(window)
    assert low > 20.0 and high < 30.0, (low, high)


def test_y_range_keeps_minimum_span_when_all_channels_agree(window: MainWindow) -> None:
    """四个通道读数完全相同时，量程不能塌缩成一条线。"""
    feed(window, [(25.0, 25.0, 25.0, 25.0)] * 2)

    low, high = y_range(window)
    assert high - low >= Y_MIN_SPAN
    assert (low + high) / 2 == pytest.approx(25.0, abs=0.01)  # 读数居中


def test_y_range_is_kept_when_every_visible_point_is_disconnected(window: MainWindow) -> None:
    """窗口内全是断线时不应把量程重置，否则曲线会突然缩放。"""
    feed(window, [(20.0, 30.0, 25.0, 28.0)])
    before = y_range(window)

    # 之后 301 个点全部断线，最早那个正常点被挤出窗口
    feed(window, [(20.0, 30.0, 25.0, 28.0)] + [(None, None, None, None)] * (int(WINDOW) + 1))
    assert y_range(window) == before
