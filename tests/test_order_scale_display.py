"""Масштаб модели в карточке заказа мастера (деск и мобильная админка).

Печатать надо в масштабе, который выбрал клиент, а не в 100% файла. Правило источника —
`orderdesk._order_scale_pct`: снимок сметы → тег в ярлыке → 100 у обычного заказа с файлом;
None там, где масштаба нет или он не записан («100%» было бы враньём мастеру).
"""
from types import SimpleNamespace

from app.web.orderdesk import _order_scale_pct


def _req(**kw):
    base = dict(breakdown=None, model_label="Годзилла", file_path="data/models/g.3mf",
                is_custom=0, product_id=0, unsliced=0)
    base.update(kw)
    return SimpleNamespace(**base)


def test_snapshot_wins():
    assert _order_scale_pct(_req(breakdown={"scale_pct": 150, "rows": []})) == 150


def test_snapshot_beats_label_tag():
    """Снимок — то, с чем слайсят пересчёт и допечатка; ярлык мог устареть."""
    r = _req(breakdown={"scale_pct": 80}, model_label="Годзилла · масштаб 150%")
    assert _order_scale_pct(r) == 80


def test_label_tag_when_snapshot_missing():
    assert _order_scale_pct(_req(model_label="Годзилла · столы 1,3 · масштаб 200%")) == 200


def test_plain_order_with_file_is_100():
    assert _order_scale_pct(_req()) == 100
    assert _order_scale_pct(_req(breakdown={"rows": []})) == 100   # снимок до фичи масштаба


def test_word_in_model_name_is_not_a_tag():
    assert _order_scale_pct(_req(model_label="Линейка масштаб 1:50")) == 100


def test_unknown_or_meaningless_is_none():
    assert _order_scale_pct(_req(is_custom=1, file_path="")) is None
    assert _order_scale_pct(_req(product_id=7)) is None
    assert _order_scale_pct(_req(file_path="")) is None          # заказ от мастера без файла
    assert _order_scale_pct(_req(unsliced=1)) is None             # слайс упал, выбор не записан


def test_unsliced_with_tag_still_shows():
    assert _order_scale_pct(_req(unsliced=1, model_label="Годзилла · масштаб 120%")) == 120
