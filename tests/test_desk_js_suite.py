"""Тесты фронта настольной админки — внутри общего прогона pytest.

У десктопа есть свой набор JS-тестов (app/webui/desk/tests/run.mjs, `npm run test:desk`,
26 тестов, ~100 мс), но до сих пор он не входил ни в один автоматический прогон: запускать
руками все забывали, и красный фронт мог доехать до ветки незамеченным. Этот файл — тонкая
обёртка: дёргает тот же run.mjs и падает вместе с ним, показывая вывод раннера.

Node нужен ТОЛЬКО разработчику: у лицензиата ни node, ни node_modules нет (собранный desk.js
коммитится). Поэтому без них — skip, а не падение.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "app" / "webui" / "desk" / "tests" / "run.mjs"


def _node_or_skip() -> str:
    """Путь к node или skip: на машине без сборочного окружения тестам фронта неоткуда взяться."""
    node = shutil.which("node")
    if not node:
        pytest.skip("нет node — тесты фронта настольной админки прогоняются только на машине разработчика")
    # run.mjs собирает каждый .test.js тем же esbuild из node_modules; без пакета он падает
    # с ENOENT, и это была бы поломка окружения, а не фронта.
    if not (ROOT / "node_modules" / "@esbuild").is_dir():
        pytest.skip("нет node_modules/@esbuild — нечем собрать тесты фронта (npm install)")
    return node


def test_desk_js_suite_passes():
    """`node app/webui/desk/tests/run.mjs` завершается нулём.

    Раннер сам собирает тесты esbuild-ом и отдаёт их встроенному `node --test`; ненулевой код
    возврата означает либо провалившийся тест, либо ошибку сборки — в обоих случаях фронт
    сломан. Вывод раннера целиком уезжает в текст ошибки, иначе по одному коду не понять что.
    """
    node = _node_or_skip()
    assert RUNNER.is_file(), f"пропал раннер тестов фронта: {RUNNER}"

    proc = subprocess.run(
        [node, str(RUNNER)],
        cwd=ROOT,
        capture_output=True,
        timeout=600,
    )
    # Не text=True: node пишет UTF-8, а locale-кодировка Windows (cp1251) спотыкается и о
    # кириллицу в названиях тестов, и о галочки раннера.
    out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    assert proc.returncode == 0, f"тесты фронта настольной админки упали (код {proc.returncode}):\n{out}"

    # Страховка от «зелёного молчания»: раннер, не нашедший ни одного теста, выходит с 1, но
    # сводку читаем и сами — вдруг маска перестанет раскрываться, а код останется нулевым.
    # Формат сводки node --test версионный, поэтому отсутствие строки не считаем провалом.
    m = re.search(r"^\D*\bpass (\d+)", out, re.MULTILINE)
    if m:
        assert int(m.group(1)) > 0, f"раннер отработал вхолостую — ноль прошедших тестов:\n{out}"
