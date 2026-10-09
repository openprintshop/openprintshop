"""Волна 1 разреза db.py: модели строк и статусы заказов живут в app/store/models.

Контракты: фасад db реэкспортирует ТОТ ЖЕ объект (isinstance/identity у потребителей —
pricing пишет `from .db import Plastic`, keyboards/texts — `REQ_STATUS_LABELS`), в db.py
не осталось повторных определений, models ни от чего не зависит (ни db, ни core), а
поведение моделей (свойства Plastic, сроки ServiceItem) не изменилось.
"""
import ast
import pathlib
import subprocess
import sys

import pytest

from app import db, pricing, texts
from app.store import models

REPO = pathlib.Path(__file__).resolve().parents[1]

CLASSES = ("Plastic", "Printer", "PrinterNozzle", "QualityPreset", "StrengthPreset", "DiscountTier",
           "PostprocOption", "SavedModel", "CollectionItem", "Request", "ServiceItem", "Product")
CONSTS = ("REQ_STATUS_LABELS", "REQ_CANCELLABLE")


@pytest.mark.parametrize("name", CLASSES + CONSTS)
def test_models_facade_reexports(name):
    assert hasattr(db, name), f"имя {name} потеряно в фасаде app/db.py"
    assert getattr(db, name) is getattr(models, name)


def test_consumers_see_the_same_objects():
    """Прямые импорты `from .db import …` у потребителей — тот же класс/словарь."""
    assert pricing.Plastic is models.Plastic
    assert texts.REQ_STATUS_LABELS is models.REQ_STATUS_LABELS


def test_no_leftover_definitions_in_dbpy():
    tree = ast.parse((REPO / "app" / "db.py").read_text(encoding="utf-8"))
    classes = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
    assert not classes, f"в db.py остались классы: {sorted(classes)}"
    assigns = {t.id for n in tree.body if isinstance(n, ast.Assign)
               for t in n.targets if isinstance(t, ast.Name)}
    assert not (assigns & set(CONSTS)), "статусы заказов определены в db.py повторно"


def test_models_import_nothing_from_the_app():
    """models — лист зависимостей: ни db, ни core, ни config. Иначе домены store/, типизируясь
    от него, замкнут цикл."""
    code = ("import app.store.models, sys; "
            "print(sorted(m for m in sys.modules if m.startswith('app.') and m != 'app.store.models'))")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == "['app.store']", f"models подтянул лишнее: {out}"


def test_model_behaviour_unchanged():
    p = models.Plastic(id=1, name="x", price_per_kg=100.0, density=1.24, is_active=1,
                       colors="Белый, Синий,, ", price_factor=1.5)
    assert p.color_list == ["Белый", "Синий"]
    assert p.price_per_gram == pytest.approx(0.15)
    assert p.eff_price_per_kg == 150.0
    s = models.ServiceItem(id=1, key=None, label="смазка", trigger_type="days", threshold=30,
                           hours_since=0, prints_since=0, last_service_at=0, is_active=1, notified=0)
    assert s.is_due(now=31 * 86400) and not s.is_due(now=10 * 86400)
    assert "deferred" in models.REQ_CANCELLABLE and models.REQ_STATUS_LABELS["new"].endswith("Новый")
