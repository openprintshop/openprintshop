"""Графики для админ-дашборда (matplotlib, headless). Импорт matplotlib ленивый —
чтобы не замедлять старт бота; шрифт по умолчанию DejaVu Sans содержит кириллицу.

ВНИМАНИЕ: модуль НЕ ИСПОЛЬЗУЕТСЯ — импортёров нет во всём проекте (графики админки
рисует фронтенд на JS), а matplotlib с 28.08.2026 не входит в зависимости продукта.
Значит вызов чего угодно отсюда упадёт ImportError у покупателя. Прежде чем возвращать
модуль в дело, верните matplotlib в requirements.txt — и взвесьте 45 МБ, ради которых
его как раз и выносили.
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import config

log = logging.getLogger(__name__)


def revenue_chart(rows: list[tuple[str, int, float]]) -> Path | None:
    """rows: [(дата, заказов, выручка), ...]. Рисует столбцы выручки + линию заказов. PNG-путь."""
    if not rows:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        dates = [r[0][5:] for r in rows]        # MM-DD
        counts = [r[1] for r in rows]
        revs = [r[2] for r in rows]
        fig, ax1 = plt.subplots(figsize=(9, 4))
        ax1.bar(dates, revs, color="#3390ec", alpha=0.85, label="Выручка")
        ax1.set_ylabel(f"Выручка, {config.CURRENCY}")
        ax1.tick_params(axis="x", labelrotation=60, labelsize=7)
        ax2 = ax1.twinx()
        ax2.plot(dates, counts, color="#e8590c", marker="o", linewidth=2, label="Заказов")
        ax2.set_ylabel("Заказов")
        ax2.set_ylim(bottom=0)
        plt.title("Выручка и заказы по дням")
        fig.tight_layout()
        out = config.QUOTE_DIR / "revenue_chart.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(out), dpi=90)
        plt.close(fig)
        return out if out.exists() else None
    except Exception:  # noqa: BLE001
        log.warning("revenue_chart failed", exc_info=True)
        return None
