"""覆盖离线重排、祖先后继定位、订正单回放与逐行裁决。

运行：PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
"""
from __future__ import annotations

import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from pqscan.errors import RearrangeError
from pqscan.expr import parse_queries, validate
from pqscan.locate import locate
from pqscan.patch import replay_patch
from pqscan.planner import plan_file
from pqscan.rearrange import rearrange_from_plan
from pqscan.scanner import InstrumentedParquetFile, scan_query
from pqscan.stats import read_footer_stats
from pqscan.versions import Ledger, register_genesis

SCHEMA = pa.schema(
    [
        pa.field("amount", pa.int64()),
        pa.field("label", pa.string()),
        pa.field("note", pa.string()),
    ]
)


def _fresh_ledger(tmp_path, rows=None):
    rows = rows if rows is not None else [
        {"amount": 5, "label": "A", "note": None},
        {"amount": 5, "label": "A", "note": None},
    ]
    src = str(tmp_path / "initial.parquet")
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), src, write_statistics=True)
    register_genesis(str(tmp_path / "v"), src, "v0")
    return Ledger(str(tmp_path / "v"))


def _two_step(tmp_path):
    ledger = _fresh_ledger(tmp_path)
    rearrange_from_plan(
        {
            "base": "v0", "new_version": "v1", "row_group_sizes": [1],
            "moves": [
                {"old": [0, 0], "new": [0, 0], "edits": {"label": "B"}},
                {"old": [0, 1], "retire": True},
            ],
        },
        ledger,
    )
    rearrange_from_plan(
        {"base": "v1", "new_version": "v2", "row_group_sizes": [1],
         "moves": [{"old": [0, 0], "new": [0, 0]}]},
        Ledger(str(tmp_path / "v")),
    )
    return Ledger(str(tmp_path / "v"))


def _scan_all(path):
    """用现有 plan/scan 入口读回整表。"""
    q = parse_queries([{"id": "all", "where": {"or": [{"is_null": "label"}, {"ne": ["label", ""]}]},
                        "columns": ["amount", "label", "note"]}])[0]
    schema = pq.ParquetFile(path).schema_arrow
    validate(q.where, schema)
    groups = read_footer_stats(path, schema)
    decisions = {path: plan_file(path, q.where, groups)}
    out = path + ".scan.parquet"
    scan_query(q, decisions, InstrumentedParquetFile, out)
    return [(r["amount"], r["label"], r["note"], r["_source_row_group"], r["_source_row_in_group"])
            for r in pq.read_table(out).to_pylist()]


# ---------------------------------------------------------------------------
# 重排校验：遗漏 / 重复消费 / 重复占用 / 越界，均整次拒绝
# ---------------------------------------------------------------------------

def test_rearrange_rejects_missing_old_row(tmp_path):
    ledger = _fresh_ledger(tmp_path)
    with pytest.raises(RearrangeError, match="遗漏旧行"):
        rearrange_from_plan(
            {"base": "v0", "new_version": "bad", "row_group_sizes": [1],
             "moves": [{"old": [0, 0], "new": [0, 0]}]}, ledger)
    assert "bad" not in ledger.records
    assert not os.path.exists(ledger.path_for("bad"))


def test_rearrange_rejects_duplicate_old_consumption(tmp_path):
    ledger = _fresh_ledger(tmp_path)
    with pytest.raises(RearrangeError, match="重复消费旧行"):
        rearrange_from_plan(
            {"base": "v0", "new_version": "bad", "row_group_sizes": [2],
             "moves": [{"old": [0, 0], "new": [0, 0]},
                       {"old": [0, 0], "new": [0, 1]}]}, ledger)


def test_rearrange_rejects_duplicate_new_position(tmp_path):
    ledger = _fresh_ledger(tmp_path)
    with pytest.raises(RearrangeError, match="重复占用新位置"):
        rearrange_from_plan(
            {"base": "v0", "new_version": "bad", "row_group_sizes": [2],
             "moves": [{"old": [0, 0], "new": [0, 0]},
                       {"old": [0, 1], "new": [0, 0]}]}, ledger)


def test_rearrange_rejects_layout_mismatch(tmp_path):
    ledger = _fresh_ledger(tmp_path)
    with pytest.raises(RearrangeError, match="容量"):
        rearrange_from_plan(
            {"base": "v0", "new_version": "bad", "row_group_sizes": [5],
             "moves": [{"old": [0, 0], "new": [0, 0]},
                       {"old": [0, 1], "new": [0, 1]}]}, ledger)


def test_rearrange_rejects_zero_size_group(tmp_path):
    ledger = _fresh_ledger(tmp_path)
    with pytest.raises(RearrangeError, match="正整数"):
        rearrange_from_plan(
            {"base": "v0", "new_version": "bad", "row_group_sizes": [0, 2],
             "moves": [{"old": [0, 0], "new": [1, 0]},
                       {"old": [0, 1], "new": [1, 1]}]}, ledger)


def test_rearrange_rejects_edits_on_retired(tmp_path):
    ledger = _fresh_ledger(tmp_path)
    with pytest.raises(RearrangeError, match="退役"):
        rearrange_from_plan(
            {"base": "v0", "new_version": "bad", "row_group_sizes": [1],
             "moves": [{"old": [0, 0], "new": [0, 0]},
                       {"old": [0, 1], "retire": True, "edits": {"label": "Z"}}]}, ledger)


def test_rearrange_rejects_existing_version_name(tmp_path):
    ledger = _fresh_ledger(tmp_path)
    with pytest.raises(RearrangeError, match="已存在"):
        rearrange_from_plan(
            {"base": "v0", "new_version": "v0", "row_group_sizes": [2],
             "moves": [{"old": [0, 0], "new": [0, 0]},
                       {"old": [0, 1], "new": [0, 1]}]}, ledger)


# ---------------------------------------------------------------------------
# 连续重排、旧版可读、新快照可再扫描
# ---------------------------------------------------------------------------

def test_two_rearranges_keep_old_versions_and_new_scan(tmp_path):
    ledger = _two_step(tmp_path)
    # 旧版仍按原坐标读取
    assert _scan_all(ledger.path_for("v0")) == [
        (5, "A", None, 0, 0), (5, "A", None, 0, 1)
    ]
    # 新快照可再扫描：先 B（第一次重排改的），第二行退役
    assert _scan_all(ledger.path_for("v2")) == [(5, "B", None, 0, 0)]
    # v1 也仍可读
    assert _scan_all(ledger.path_for("v1")) == [(5, "B", None, 0, 0)]


def test_snapshot_is_true_parquet_rescannable(tmp_path):
    ledger = _two_step(tmp_path)
    pf = pq.ParquetFile(ledger.path_for("v2"))
    assert pf.num_row_groups == 1
    assert pf.metadata.num_rows == 1
    assert pf.schema_arrow.names == ["amount", "label", "note"]


# ---------------------------------------------------------------------------
# 定位入口：现行 / 退役 / 不同链 / 向下回溯
# ---------------------------------------------------------------------------

def test_locate_forward_live_and_retired(tmp_path):
    ledger = _two_step(tmp_path)
    live = locate(ledger, "v0", "v2", 0, 0)
    assert live.status == "live" and live.to_coord == [0, 0]
    retired = locate(ledger, "v0", "v2", 0, 1)
    assert retired.status == "retired" and retired.to_coord is None


def test_locate_different_lineage_is_incomparable(tmp_path):
    ledger = _two_step(tmp_path)
    rearrange_from_plan(
        {"base": "v0", "new_version": "va", "row_group_sizes": [2],
         "moves": [{"old": [0, 0], "new": [0, 0]}, {"old": [0, 1], "new": [0, 1]}]},
        ledger)
    ledger = Ledger(str(tmp_path / "v"))
    assert locate(ledger, "v2", "va", 0, 0).status == "incomparable"


def test_locate_backward_to_ancestor(tmp_path):
    ledger = _two_step(tmp_path)
    back = locate(ledger, "v2", "v0", 0, 0)
    assert back.status == "live" and back.to_coord == [0, 0]
    # v2 的坐标来自 v0 的 (0,0)；v0 的第二行没有后代
    assert locate(ledger, "v2", "v0", 0, 0).to_coord == [0, 0]


def test_swap_identical_rows_tracks_identity_not_position(tmp_path):
    """两行内容完全相同、仅物理位置互换：定位按逐格映射走，而非按坐标取值。"""
    ledger = _fresh_ledger(tmp_path)
    rearrange_from_plan(
        {"base": "v0", "new_version": "vs", "row_group_sizes": [2],
         "moves": [{"old": [0, 0], "new": [0, 1]}, {"old": [0, 1], "new": [0, 0]}]},
        ledger)
    ledger = Ledger(str(tmp_path / "v"))
    assert locate(ledger, "v0", "vs", 0, 0).to_coord == [0, 1]
    assert locate(ledger, "v0", "vs", 0, 1).to_coord == [0, 0]
    # 从 v0(0,0) 订正，应写到 vs 的 (0,1)
    res = replay_patch(
        {"base": "v0", "rows": [
            {"coord": [0, 0], "fields": [{"column": "amount", "old": 5, "new": 9}]}]},
        "vs", ledger, "ps")
    rows = _scan_all(ledger.path_for(res.new_version))
    by_pos = {(r[3], r[4]): r[0] for r in rows}
    assert by_pos == {(0, 0): 5, (0, 1): 9}


# ---------------------------------------------------------------------------
# 订正单回放：成功 / 退役 / 整行冲突 / NULL 相等 / 跨链
# ---------------------------------------------------------------------------

def _patch(coord, fields):
    return {"base": "v0", "rows": [{"coord": coord, "fields": fields}]}


def test_patch_applies_and_reports_retired(tmp_path):
    ledger = _two_step(tmp_path)
    res = replay_patch(
        {"base": "v0", "rows": [
            {"coord": [0, 0], "fields": [{"column": "amount", "old": 5, "new": 7}]},
            {"coord": [0, 1], "fields": [{"column": "amount", "old": 5, "new": 7}]},
        ]},
        "v2", ledger, "p1")
    assert res.rows[0].status == "applied"
    assert res.rows[1].status == "retired"
    assert res.new_version == "p1"
    assert _scan_all(ledger.path_for("p1")) == [(7, "B", None, 0, 0)]
    # 老版不受影响
    assert _scan_all(ledger.path_for("v2")) == [(5, "B", None, 0, 0)]
    # 订正版仍能定位祖先
    assert locate(Ledger(str(tmp_path / "v")), "v0", "p1", 0, 0).status == "live"


def test_patch_conflict_blocks_whole_row(tmp_path):
    ledger = _two_step(tmp_path)
    res = replay_patch(
        _patch([0, 0], [
            {"column": "amount", "old": 5, "new": 7},
            {"column": "label", "old": "A", "new": "C"},   # 现值 B -> 冲突
        ]),
        "v2", ledger, "p2")
    assert res.rows[0].status == "conflict"
    assert res.applied_fields == 0
    assert res.new_version is None
    amount_field = next(f for f in res.rows[0].fields if f.column == "amount")
    assert amount_field.status == "skipped_on_conflict"
    assert _scan_all(ledger.path_for("v2")) == [(5, "B", None, 0, 0)]


def test_patch_success_after_label_restored(tmp_path):
    ledger = _two_step(tmp_path)
    # 另起与 v2 同形但 label 保持 A 的链
    rearrange_from_plan(
        {"base": "v0", "new_version": "v2a", "row_group_sizes": [1],
         "moves": [{"old": [0, 0], "new": [0, 0]},
                   {"old": [0, 1], "retire": True}]},
        ledger)
    ledger = Ledger(str(tmp_path / "v"))
    res = replay_patch(
        _patch([0, 0], [
            {"column": "amount", "old": 5, "new": 7},
            {"column": "label", "old": "A", "new": "C"},
        ]),
        "v2a", ledger, "p3")
    assert res.rows[0].status == "applied"
    assert res.applied_fields == 2
    assert _scan_all(ledger.path_for("p3")) == [(7, "C", None, 0, 0)]


def test_patch_already_satisfied_and_null_equality(tmp_path):
    ledger = _two_step(tmp_path)
    res = replay_patch(
        _patch([0, 0], [
            {"column": "amount", "old": 5, "new": 5},     # 已达
            {"column": "note", "old": None, "new": "n1"},  # NULL == NULL -> 可改
        ]),
        "v2", ledger, "p4")
    assert res.rows[0].status == "applied"
    assert _scan_all(ledger.path_for("p4")) == [(5, "B", "n1", 0, 0)]


def test_patch_other_rows_succeed_despite_one_conflict(tmp_path):
    ledger = _fresh_ledger(tmp_path)
    res = replay_patch(
        {"base": "v0", "rows": [
            {"coord": [0, 0], "fields": [{"column": "label", "old": "A", "new": "Z"}]},
            {"coord": [0, 1], "fields": [{"column": "label", "old": "WRONG", "new": "Z"}]},
        ]},
        "v0", ledger, "p5")
    assert res.rows[0].status == "applied"
    assert res.rows[1].status == "conflict"
    assert res.applied_fields == 1
    assert _scan_all(ledger.path_for("p5")) == [
        (5, "Z", None, 0, 0), (5, "A", None, 0, 1)
    ]


def test_patch_incomparable_lineage_is_flagged(tmp_path):
    ledger = _two_step(tmp_path)
    rearrange_from_plan(
        {"base": "v0", "new_version": "va", "row_group_sizes": [2],
         "moves": [{"old": [0, 0], "new": [0, 0]}, {"old": [0, 1], "new": [0, 1]}]},
        ledger)
    ledger = Ledger(str(tmp_path / "v"))
    res = replay_patch(
        _patch([0, 0], [{"column": "amount", "old": 5, "new": 7}]),
        "va", ledger, "p6")
    # base=v0 是 va 的祖先，因此这里其实可达；改成从 v2 出发不可达的情况：
    res2 = replay_patch(
        {"base": "v2", "rows": [
            {"coord": [0, 0], "fields": [{"column": "amount", "old": 5, "new": 7}]}]},
        "va", ledger, "p7")
    assert res.new_version == "p6"
    assert res2.rows[0].status == "incomparable"
    assert res2.new_version is None
