"""端到端样例：两行除物理位置外完全相同，验证重排、定位与订正回放。

场景（与需求一致）：
- 起始版 v0 含两行，除物理位置外**完全相同**：amount=5、label=A；
- v1：让第二行（坐标 (0,1)）退役，第一行进两次搬迁并改 label：A→B；
- 因此 v2 只有一行 amount=5、label=B，但它是起始版第一行的后代；
- 从起始版坐标下发订正单，交给现行版 v2 裁决：
  * 第一行 amount 5→7 成功；
  * 起始版第二行已退役；
  * 同一行若同时要求 label A→C，则第一行整行保留 5/B（label 现值 B 冲突）；
  * 把搬迁中的 B 改回 A（即另做一条只含 label A→A 的重排链），双字段单就写入 7/C。

所有版本都用 pqscan 现有扫描入口（plan/scan）读回，便于核对。

直接运行：
    python examples/demo_rearrange_patch.py
"""
from __future__ import annotations

import json
import os
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq

from pqscan.expr import parse_queries, validate
from pqscan.planner import plan_file
from pqscan.scanner import InstrumentedParquetFile, scan_query
from pqscan.stats import read_footer_stats
from pqscan.rearrange import rearrange_from_plan
from pqscan.patch import replay_patch
from pqscan.locate import locate
from pqscan.versions import Ledger, register_genesis

SCHEMA = pa.schema(
    [
        pa.field("amount", pa.int64()),
        pa.field("label", pa.string()),
    ]
)


def _write_initial(path: str) -> None:
    tbl = pa.Table.from_pylist(
        [{"amount": 5, "label": "A"}, {"amount": 5, "label": "A"}], schema=SCHEMA
    )
    pq.write_table(tbl, path, write_statistics=True)


def _scan(parquet_path: str, where: dict) -> list[dict]:
    """用现有 plan/scan 入口扫描一个版本快照并返回行（含溯源列）。"""
    q = parse_queries([{"id": "dump", "where": where, "columns": ["amount", "label"]}])[0]
    schema = pq.ParquetFile(parquet_path).schema_arrow
    validate(q.where, schema)
    groups = read_footer_stats(parquet_path, schema)
    decisions = {parquet_path: plan_file(parquet_path, q.where, groups)}
    out = parquet_path + f".scan.{q.id}.parquet"
    scan_query(q, decisions, InstrumentedParquetFile, out)
    return pq.read_table(out).to_pylist()


def _scan_all(parquet_path: str) -> list[dict]:
    """恒真谓词（amount 可能为 NULL 时用 is_null OR not）以等价于全表查看。"""
    q = parse_queries([{"id": "all", "where": {"or": [{"is_null": "label"}, {"ne": ["label", ""]}]},
                        "columns": ["amount", "label"]}])[0]
    schema = pq.ParquetFile(parquet_path).schema_arrow
    validate(q.where, schema)
    groups = read_footer_stats(parquet_path, schema)
    decisions = {parquet_path: plan_file(parquet_path, q.where, groups)}
    out = parquet_path + ".all.parquet"
    scan_query(q, decisions, InstrumentedParquetFile, out)
    return pq.read_table(out).to_pylist()


def _rows_without_prov(rows: list[dict]) -> list[tuple]:
    return [(r["amount"], r["label"], r["_source_row_group"], r["_source_row_in_group"])
            for r in rows]


def main() -> None:
    workdir = tempfile.mkdtemp(prefix="pqscan_demo_")
    vdir = os.path.join(workdir, "versions")
    os.makedirs(vdir)
    initial = os.path.join(workdir, "initial.parquet")
    _write_initial(initial)
    ledger = Ledger(vdir)
    register_genesis(vdir, initial, "v0")
    ledger = Ledger(vdir)
    print(f"工作目录: {workdir}")

    # ---- v0：两行完全相同（除位置） --------------------------------------
    v0_rows = _scan_all(ledger.path_for("v0"))
    print("\n[v0] 扫描全部行:", _rows_without_prov(v0_rows))
    assert [r["amount"] for r in v0_rows] == [5, 5]
    assert [r["label"] for r in v0_rows] == ["A", "A"]

    # ---- v1：第二行退役，第一行搬到别处；label A->B ----------------------
    plan_v1 = {
        "base": "v0",
        "new_version": "v1",
        "row_group_sizes": [1],           # 只保留 1 行
        "moves": [
            {"old": [0, 0], "new": [0, 0], "edits": {"label": "B"}},
            {"old": [0, 1], "retire": True},
        ],
        "note": "第二行退役；第一行改 label=B",
    }
    rearrange_from_plan(plan_v1, Ledger(vdir))
    ledger = Ledger(vdir)
    v1_rows = _scan_all(ledger.path_for("v1"))
    print("[v1] 扫描全部行:", _rows_without_prov(v1_rows))
    assert [(r["amount"], r["label"]) for r in v1_rows] == [(5, "B")]

    # ---- v2：第一行再搬一次（连续重排，旧版仍可按原坐标读取） ------------
    plan_v2 = {
        "base": "v1",
        "new_version": "v2",
        "row_group_sizes": [1],
        "moves": [{"old": [0, 0], "new": [0, 0]}],
        "note": "第一行再次搬迁，内容不变",
    }
    rearrange_from_plan(plan_v2, Ledger(vdir))
    ledger = Ledger(vdir)
    v2_rows = _scan_all(ledger.path_for("v2"))
    print("[v2] 扫描全部行:", _rows_without_prov(v2_rows))
    assert [(r["amount"], r["label"]) for r in v2_rows] == [(5, "B")]

    # 旧版 v0 仍可按原坐标读取（未被改写）
    v0_again = _scan_all(ledger.path_for("v0"))
    assert [(r["amount"], r["label"]) for r in v0_again] == [(5, "A"), (5, "A")]
    print("[v0] 重排后仍可读:", _rows_without_prov(v0_again))

    # ---- 定位：v0 的 (0,0) -> v2 现行坐标；v0 的 (0,1) -> 退役 -----------
    loc0 = locate(ledger, "v0", "v2", 0, 0)
    loc1 = locate(ledger, "v0", "v2", 0, 1)
    print("\n[locate] v0(0,0)->v2:", loc0.to_dict())
    print("[locate] v0(0,1)->v2:", loc1.to_dict())
    assert loc0.status == "live" and loc0.to_coord == [0, 0]
    assert loc1.status == "retired"

    # 不同祖先链：另建一条从 v0 出发的新链，与 v2 不可比
    plan_alt = {
        "base": "v0", "new_version": "v_alt", "row_group_sizes": [2],
        "moves": [{"old": [0, 0], "new": [0, 0]}, {"old": [0, 1], "new": [0, 1]}],
    }
    rearrange_from_plan(plan_alt, Ledger(vdir))
    ledger = Ledger(vdir)
    inc = locate(ledger, "v2", "v_alt", 0, 0)
    print("[locate] v2->v_alt（不同链）:", inc.to_dict())
    assert inc.status == "incomparable"

    # ---- 地址改变的核心隐患：交换两条完全相同的记录 ---------------------
    # 旧坐标 (0,0) 在新版指向了原来 (0,1) 的记录；靠逐格映射而非坐标取值。
    plan_swap = {
        "base": "v0", "new_version": "v_swap", "row_group_sizes": [2],
        "moves": [{"old": [0, 0], "new": [0, 1]}, {"old": [0, 1], "new": [0, 0]}],
        "note": "两条相同记录互换物理位置",
    }
    rearrange_from_plan(plan_swap, Ledger(vdir))
    ledger = Ledger(vdir)
    print("\n[locate] v0(0,0)->v_swap:", locate(ledger, "v0", "v_swap", 0, 0).to_dict())
    assert locate(ledger, "v0", "v_swap", 0, 0).to_coord == [0, 1]
    # 从 v0 的 (0,0) 订正：应写到 v_swap 的 (0,1)，而不是物理位置 (0,0)
    res_swap = replay_patch(
        {"base": "v0", "rows": [
            {"coord": [0, 0], "fields": [{"column": "amount", "old": 5, "new": 9}]}]},
        "v_swap", Ledger(vdir), "p_swap")
    assert res_swap.rows[0].status == "applied"
    ledger = Ledger(vdir)
    swap_rows = _scan_all(ledger.path_for("p_swap"))
    print("[p_swap] 扫描全部行:", _rows_without_prov(swap_rows))
    by_pos = {(r["_source_row_group"], r["_source_row_in_group"]): r["amount"] for r in swap_rows}
    assert by_pos == {(0, 0): 5, (0, 1): 9}   # 被改的是搬过去的原 (0,0)，现位于 (0,1)

    # ---- 订正单 A：amount 5->7，应成功；第二行应报退役 -------------------
    patch_a = {
        "base": "v0",
        "rows": [
            {"coord": [0, 0], "fields": [{"column": "amount", "old": 5, "new": 7}]},
            {"coord": [0, 1], "fields": [{"column": "amount", "old": 5, "new": 7}]},
        ],
    }
    res_a = replay_patch(patch_a, "v2", Ledger(vdir), "p1")
    ledger = Ledger(vdir)
    print("\n[patch A] amount 5->7:")
    print(json.dumps(res_a.to_dict(), ensure_ascii=False, indent=2))
    assert res_a.rows[0].status == "applied"
    assert res_a.rows[1].status == "retired"
    p1_rows = _scan_all(ledger.path_for(res_a.new_version))
    print("[p1] 扫描全部行:", _rows_without_prov(p1_rows))
    assert [(r["amount"], r["label"]) for r in p1_rows] == [(7, "B")]

    # ---- 订正单 B：同时要求 amount 5->7 与 label A->C，整行冲突 ---------
    patch_b = {
        "base": "v0",
        "rows": [{"coord": [0, 0], "fields": [
            {"column": "amount", "old": 5, "new": 7},
            {"column": "label", "old": "A", "new": "C"},
        ]}],
    }
    res_b = replay_patch(patch_b, "v2", Ledger(vdir), "p2_conflict")
    ledger = Ledger(vdir)
    print("\n[patch B] amount 5->7 且 label A->C（label 现值 B 冲突）:")
    print(json.dumps(res_b.to_dict(), ensure_ascii=False, indent=2))
    assert res_b.rows[0].status == "conflict"
    assert res_b.new_version is None
    # v2 未被影响
    v2_after_b = _scan_all(ledger.path_for("v2"))
    assert [(r["amount"], r["label"]) for r in v2_after_b] == [(5, "B")]
    print("[v2] 回放冲突后仍为:", _rows_without_prov(v2_after_b))

    # ---- 订正单 C：把搬迁中的 label B 改回 A，再下双字段单 -> 写入 7/C ---
    plan_v2a = {
        "base": "v0", "new_version": "v2a", "row_group_sizes": [1],
        "moves": [
            {"old": [0, 0], "new": [0, 0], "edits": {"label": "A"}},
            {"old": [0, 1], "retire": True},
        ],
        "note": "与 v2 同形，但 label 保持 A",
    }
    rearrange_from_plan(plan_v2a, Ledger(vdir))
    ledger = Ledger(vdir)
    res_c = replay_patch(patch_b, "v2a", Ledger(vdir), "p3")
    ledger = Ledger(vdir)
    print("\n[patch C] label 改回 A 后，双字段单:")
    print(json.dumps(res_c.to_dict(), ensure_ascii=False, indent=2))
    assert res_c.rows[0].status == "applied"
    p3_rows = _scan_all(ledger.path_for(res_c.new_version))
    print("[p3] 扫描全部行:", _rows_without_prov(p3_rows))
    assert [(r["amount"], r["label"]) for r in p3_rows] == [(7, "C")]

    # 订正产生的版本仍能定位祖先
    loc_p3 = locate(ledger, "v0", res_c.new_version, 0, 0)
    print("[locate] v0(0,0)->p3:", loc_p3.to_dict())
    assert loc_p3.status == "live"

    print("\n全部断言通过。各版扫描结果与裁决均符合预期。")
    print(f"版本台账与快照保留在: {vdir}")


if __name__ == "__main__":
    main()
