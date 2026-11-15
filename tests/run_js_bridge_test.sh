#!/usr/bin/env bash
# Прогон теста JS-моста чтения (app/max_dom.py) без браузера:
# извлекаем константу _JS_BRIDGE из Python-модуля и гоняем на мини-DOM в node.
# Требуется nodejs >= 18. Запуск из корня проекта: bash tests/run_js_bridge_test.sh
set -euo pipefail
cd "$(dirname "$0")/.."

python3 - <<'PY'
import ast
tree = ast.parse(open('app/max_dom.py').read())
src = next(
    ast.literal_eval(n.value)
    for n in tree.body
    if isinstance(n, ast.Assign)
    and getattr(n.targets[0], 'id', '') == '_JS_BRIDGE'
)
open('.test_js_bridge.js', 'w').write(src)
PY

trap 'rm -f .test_js_bridge.js' EXIT
node tests/test_max_dom_bridge.js
