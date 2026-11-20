"""订正单回放：把旧版坐标上的“旧值→目标值”订正裁决到现行版本。

订正单
------
- ``base``：订正单写明的起始版本；
- ``rows``：每行给出起始版当时坐标 ``(行组, 组内行号)``，以及若干
  ``{column, old, new}`` 字段订正项（旧值/目标值可为 NULL）。

裁决（按行）
------------
1. 先用定位入口把起始版坐标换算到目标版本的**现行坐标**；若该行已退役，
   整行记为 ``retired``，不写任何字段。
2. 否则逐字段裁决：
   - 现值 == 旧值：可改，记为 ``applied``；
   - 现值 == 目标值：视为已达成，记为 ``already``；
   - 其他值：``conflict``。
   NULL 参与相等判断（``None == None``）。若某行存在任一 ``conflict``，
   该行**所有**字段都不写（包括其中本可应用的字段），整行记为冲突。
3. 同一张单的其他行仍独立裁决、独立成功。

结果
----
返回逐行解释；只要存在可成功写入的字段，就写出一个**新的**可扫描快照
（内容为“现值 + 已应用字段”），并在台账登记，具备指向祖先的映射。
老版快照不受影响。若无任何可写字段，则不产生新版本。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import PatchError
from .locate import INCOMPARABLE, RETIRED, locate
from .snapshot import read_snapshot, write_snapshot
from .versions import Ledger, VersionRecord, global_to_phys, phys_to_global

STATUS_APPLIED = "applied"
STATUS_ALREADY = "already"
STATUS_CONFLICT = "conflict"
STATUS_RETIRED = "retired"
STATUS_INCOMPARABLE = "incomparable"


@dataclass
class FieldVerdict:
    column: str
    old: Any
    new: Any
    current: Any
    status: str            # applied | already | conflict | (skipped_on_conflict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "column": self.column,
            "old": self.old,
            "new": self.new,
            "current": self.current,
            "status": self.status,
        }


@dataclass
class RowVerdict:
    base_coord: list[int]
    status: str
    target_coord: list[int] | None
    fields: list[FieldVerdict] = field(default_factory=list)
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_coord": list(self.base_coord),
            "status": self.status,
            "target_coord": None if self.target_coord is None else list(self.target_coord),
            "fields": [f.to_dict() for f in self.fields],
            "detail": self.detail,
        }


@dataclass
class PatchResult:
    base_version: str
    target_version: str
    new_version: str | None          # 无任何可写字段时为 None
    rows: list[RowVerdict]
    applied_fields: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_version": self.base_version,
            "target_version": self.target_version,
            "new_version": self.new_version,
            "applied_fields": self.applied_fields,
            "rows": [r.to_dict() for r in self.rows],
        }


def _equal(a: Any, b: Any) -> bool:
    """NULL 参与相等判断：None == None 为真。"""
    return a == b


def parse_patch(raw: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(raw, dict):
        raise PatchError("订正单必须是对象")
    for key in ("base", "rows"):
        if key not in raw:
            raise PatchError(f"订正单缺少字段 {key!r}")
    if not isinstance(raw["rows"], list):
        raise PatchError("订正单 rows 必须是列表")
    return str(raw["base"]), raw["rows"]


def replay_patch(
    raw_patch: dict[str, Any],
    target_version: str,
    ledger: Ledger,
    new_version: str,
    note: str = "",
) -> PatchResult:
    base_version, raw_rows = parse_patch(raw_patch)
    ledger.get(base_version)
    target_rec = ledger.get(target_version)
    if new_version in ledger.records:
        raise PatchError(f"目标版本 {new_version!r} 已存在，拒绝覆盖（快照不可变）")

    # 目标版的行内容，作为“现值”的唯一来源（不读起始版快照）。
    target = read_snapshot(ledger.path_for(target_version))
    target_flat = target.flatten()

    # 逐行裁决，同时汇总每行的“待写字段”。
    verdicts: list[RowVerdict] = []
    planned: list[tuple[int, dict[str, Any]]] = []   # (目标全局行, {列: 新值})
    applied_fields = 0
    for i, raw_row in enumerate(raw_rows):
        if not isinstance(raw_row, dict) or "coord" not in raw_row:
            raise PatchError(f"第 {i} 行订正缺少 coord: {raw_row!r}")
        coord = raw_row["coord"]
        if not (isinstance(coord, list) and len(coord) == 2):
            raise PatchError(f"第 {i} 行 coord 必须是 [行组, 行号]: {raw_row!r}")
        rg, row = int(coord[0]), int(coord[1])
        fields_raw = raw_row.get("fields")
        if not isinstance(fields_raw, list) or not fields_raw:
            raise PatchError(f"第 {i} 行 fields 必须是非空列表: {raw_row!r}")

        loc = locate(ledger, base_version, target_version, rg, row)
        if loc.status == RETIRED:
            verdicts.append(RowVerdict([rg, row], STATUS_RETIRED, None, [], loc.detail))
            continue
        if loc.status == INCOMPARABLE:
            verdicts.append(RowVerdict([rg, row], STATUS_INCOMPARABLE, None, [], loc.detail))
            continue

        t_rg, t_row = loc.to_coord
        t_global = phys_to_global(target_rec.row_group_sizes, t_rg, t_row)
        current_row = target_flat[t_global]

        field_verdicts: list[FieldVerdict] = []
        row_conflict = False
        for f in fields_raw:
            if not isinstance(f, dict) or "column" not in f:
                raise PatchError(f"第 {i} 行的字段订正项非法: {f!r}")
            col = f["column"]
            if col not in target.schema.names:
                raise PatchError(f"第 {i} 行订正了不存在的列: {col!r}")
            old, new = f.get("old"), f.get("new")
            current = current_row[col]
            if _equal(current, old):
                status = STATUS_APPLIED
            elif _equal(current, new):
                status = STATUS_ALREADY
            else:
                status = STATUS_CONFLICT
                row_conflict = True
            field_verdicts.append(FieldVerdict(col, old, new, current, status))

        if row_conflict:
            # 行内任一冲突 => 该行所有字段都不写。
            for fv in field_verdicts:
                if fv.status == STATUS_APPLIED:
                    fv.status = "skipped_on_conflict"
            verdicts.append(
                RowVerdict([rg, row], STATUS_CONFLICT, [t_rg, t_row], field_verdicts,
                           "存在字段冲突，整行保留现值，不写任何字段")
            )
            continue

        row_status = STATUS_ALREADY if all(
            fv.status == STATUS_ALREADY for fv in field_verdicts
        ) else STATUS_APPLIED
        writes = {fv.column: fv.new for fv in field_verdicts if fv.status == STATUS_APPLIED}
        if writes:
            planned.append((t_global, writes))
            applied_fields += len(writes)
        verdicts.append(
            RowVerdict([rg, row], row_status, [t_rg, t_row], field_verdicts,
                       "全部字段达成" if row_status == STATUS_ALREADY
                       else f"写入 {len(writes)} 个字段")
        )

    if applied_fields == 0:
        return PatchResult(base_version, target_version, None, verdicts, 0)

    # 写出新快照：以目标版内容为底，覆盖计划写入的字段。
    new_groups = [list(group) for group in target.row_groups]
    # 复制成可改的 dict
    new_groups = [[dict(r) for r in group] for group in new_groups]
    for t_global, writes in planned:
        grp, r = global_to_phys(target_rec.row_group_sizes, t_global)
        new_groups[grp][r].update(writes)

    out_path = ledger.path_for(new_version)
    try:
        write_snapshot(out_path, target.schema, new_groups)
    except Exception as exc:
        raise PatchError(f"写出订正版快照失败: {exc}") from exc

    rec = VersionRecord(
        version=new_version,
        parents=[target_version],
        kind="patch",
        num_rows=target_rec.num_rows,
        row_group_sizes=list(target_rec.row_group_sizes),
        map=list(range(target_rec.num_rows)),   # 订正不改变行身份/位置，仅改字段值
        retired_parent_rows=[],
        note=note,
    )
    ledger.add(rec)
    ledger.save()

    return PatchResult(base_version, target_version, new_version, verdicts, applied_fields)
