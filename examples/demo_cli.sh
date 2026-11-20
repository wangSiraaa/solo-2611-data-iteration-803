#!/usr/bin/env bash
# 命令行复跑样例：两行除物理位置外完全相同（amount=5,label=A），
# 第二行退役、第一行经两次搬迁并把 label 改为 B；再用订正单回放。
#
# 用法（仓库根目录）：
#   PYTHONPATH=src .venv/bin/python -m pqscan.cli init ...   # 由本脚本自动完成
#   bash examples/demo_cli.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-python3}"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

WORK="$(mktemp -d)"
VDIR="$WORK/versions"
echo "工作目录: $WORK"

# 1) 生成起始快照：两行除物理位置外完全相同
"$PY" - "$WORK/initial.parquet" <<'PY'
import sys, pyarrow as pa, pyarrow.parquet as pq
schema = pa.schema([pa.field("amount", pa.int64()), pa.field("label", pa.string())])
rows = [{"amount": 5, "label": "A"}, {"amount": 5, "label": "A"}]
pq.write_table(pa.Table.from_pylist(rows, schema=schema), sys.argv[1], write_statistics=True)
PY

# 2) 登记起点版本 v0，并做两次搬迁（v0->v1->v2）
"$PY" -m pqscan.cli init --dir "$VDIR" --source "$WORK/initial.parquet" --version v0

cat > "$WORK/re1.json" <<'JSON'
{"base":"v0","new_version":"v1","row_group_sizes":[1],
 "moves":[{"old":[0,0],"new":[0,0],"edits":{"label":"B"}},
          {"old":[0,1],"retire":true}],
 "note":"第二行退役；第一行改 label=B"}
JSON
cat > "$WORK/re2.json" <<'JSON'
{"base":"v1","new_version":"v2","row_group_sizes":[1],
 "moves":[{"old":[0,0],"new":[0,0]}],
 "note":"第一行再搬一次，内容不变"}
JSON
"$PY" -m pqscan.cli rearrange --dir "$VDIR" --plan "$WORK/re1.json"
"$PY" -m pqscan.cli rearrange --dir "$VDIR" --plan "$WORK/re2.json"

# 3) 定位：v0 第一行 -> v2 现行；v0 第二行 -> 退役
echo "--- locate v0(0,0) -> v2 ---"
"$PY" -m pqscan.cli locate --dir "$VDIR" --from v0 --to v2 --row-group 0 --row 0
echo "--- locate v0(0,1) -> v2 ---"
"$PY" -m pqscan.cli locate --dir "$VDIR" --from v0 --to v2 --row-group 0 --row 1

# 4) 订正单 A：第一行 amount 5->7 成功；第二行退役
cat > "$WORK/patch_a.json" <<'JSON'
{"base":"v0","rows":[
 {"coord":[0,0],"fields":[{"column":"amount","old":5,"new":7}]},
 {"coord":[0,1],"fields":[{"column":"amount","old":5,"new":7}]}]}
JSON
echo "--- patch A: amount 5->7 ---"
"$PY" -m pqscan.cli patch --dir "$VDIR" --patch "$WORK/patch_a.json" --target v2 --new-version p1

# 5) 订正单 B：同时要求 amount 5->7 与 label A->C；label 现值 B 冲突 -> 整行不写
cat > "$WORK/patch_b.json" <<'JSON'
{"base":"v0","rows":[
 {"coord":[0,0],"fields":[
   {"column":"amount","old":5,"new":7},
   {"column":"label","old":"A","new":"C"}]}]}
JSON
echo "--- patch B: amount 5->7 且 label A->C（应冲突）---"
"$PY" -m pqscan.cli patch --dir "$VDIR" --patch "$WORK/patch_b.json" --target v2 --new-version p2_conflict || true

# 6) 把搬迁中的 label B 改回 A，再下同一张双字段单 -> 写入 7/C
cat > "$WORK/re_alt.json" <<'JSON'
{"base":"v0","new_version":"v2a","row_group_sizes":[1],
 "moves":[{"old":[0,0],"new":[0,0],"edits":{"label":"A"}},
          {"old":[0,1],"retire":true}]}
JSON
"$PY" -m pqscan.cli rearrange --dir "$VDIR" --plan "$WORK/re_alt.json"
echo "--- patch B 交给 v2a（label 已为 A）---"
"$PY" -m pqscan.cli patch --dir "$VDIR" --patch "$WORK/patch_b.json" --target v2a --new-version p3

# 7) 用现有扫描入口（pqscan run）读回各版结果
"$PY" - "$WORK" <<'PY'
import json, os, sys
work = sys.argv[1]
for ver in ["v0", "v1", "v2", "p1", "p3"]:
    d = os.path.join(work, "scan_" + ver); os.makedirs(d, exist_ok=True)
    files = os.path.join(d, "files.json")
    with open(files, "w") as fh:
        json.dump([os.path.join(work, "versions", ver + ".parquet")], fh)
    queries = os.path.join(d, "queries.json")
    with open(queries, "w") as fh:
        json.dump([{"id": "all", "columns": ["amount", "label"],
                    "where": {"or": [{"is_null": "label"}, {"ne": ["label", ""]}]}}], fh)
    print(f"=== 版本 {ver} ===")
    os.system(f'"{sys.executable}" -m pqscan.cli run --files "{files}" --queries "{queries}" --out "{d}/out" >/dev/null')
    import pyarrow.parquet as pq
    rows = pq.read_table(os.path.join(d, "out", "results", "all.parquet")).to_pylist()
    print([(r["amount"], r["label"], r["_source_row_group"], r["_source_row_in_group"]) for r in rows])
PY

echo "完成。版本台账与快照保留在: $VDIR"
