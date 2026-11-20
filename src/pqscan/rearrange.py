"""离线重排：按逐行搬迁清单把旧快照的每一行搬到新位置或退役，并写出新快照。

搬迁清单语义
------------
- 每个旧物理坐标 ``(行组, 组内行号)`` **恰好出现一次**；
- 每一项要么给出新坐标（搬入新文件的指定位置），要么标记退役（``retire: true``）；
- 搬迁时可对搬过去的行显式点名改动的业务字段（``edits``，值可为 NULL）。

整次重排要么作为新的可见版本落盘，要么完全不产生版本。下列任一情况整体拒绝：
- 遗漏某个旧行（未出现在清单里）；
- 重复消费某个旧行（同一旧坐标出现多次）；
- 重复占用同一新位置，或新位置与 ``row_group_sizes`` 容量不符（多/少、越界）。

新快照写出后登记到版本台账，旧快照原样保留，仍可按旧坐标读取。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import RearrangeError
from .snapshot import read_snapshot, write_snapshot
from .versions import (
    Ledger,
    VersionRecord,
    phys_to_global,
)


@dataclass
class Move:
    old_rg: int
    old_row: int
    new_rg: int | None = None       # None 表示退役
    new_row: int | None = None
    edits: dict[str, Any] = field(default_factory=dict)


@dataclass
class RearrangeResult:
    version: str
    path: str
    num_rows: int
    retired: int
    edits_applied: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "path": self.path,
            "num_rows": self.num_rows,
            "retired": self.retired,
            "edits_applied": self.edits_applied,
        }


def parse_moves(raw: list[dict[str, Any]]) -> list[Move]:
    moves: list[Move] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise RearrangeError(f"第 {i} 条搬迁项必须是对象: {item!r}")
        old = item.get("old")
        if not (isinstance(old, list) and len(old) == 2):
            raise RearrangeError(f"第 {i} 条缺少合法的 old=[行组, 行号]: {item!r}")
        edits = item.get("edits", {})
        if edits is None:
            edits = {}
        if not isinstance(edits, dict):
            raise RearrangeError(f"第 {i} 条 edits 必须是对象: {edits!r}")
        retire = bool(item.get("retire", False))
        new = item.get("new")
        if retire and new is not None:
            raise RearrangeError(f"第 {i} 条不能既退役又给出新位置: {item!r}")
        if retire:
            if edits:
                raise RearrangeError(
                    f"第 {i} 条已退役，不能同时点名改字段: edits={edits!r}"
                )
            moves.append(Move(int(old[0]), int(old[1]), edits=dict(edits)))
            continue
        if not (isinstance(new, list) and len(new) == 2):
            raise RearrangeError(f"第 {i} 条未退役但没有合法的 new=[行组, 行号]: {item!r}")
        moves.append(Move(int(old[0]), int(old[1]), int(new[0]), int(new[1]), dict(edits)))
    return moves


def apply_rearrange(
    base_version: str,
    moves: list[Move],
    new_row_group_sizes: list[int],
    new_version: str,
    ledger: Ledger,
    note: str = "",
) -> RearrangeResult:
    ledger.get(base_version)  # 版本不存在时抛 LocateError
    if new_version in ledger.records:
        raise RearrangeError(f"目标版本 {new_version!r} 已存在，拒绝覆盖（快照不可变）")
    base = read_snapshot(ledger.path_for(base_version))

    old_sizes = base.row_group_sizes
    old_num_rows = base.num_rows

    # --- 校验：旧行被恰好消费一次 ---------------------------------------
    seen_old: dict[tuple[int, int], int] = {}
    for idx, mv in enumerate(moves):
        key = (mv.old_rg, mv.old_row)
        if key in seen_old:
            raise RearrangeError(
                f"重复消费旧行：坐标 {key} 在清单中出现多次"
                f"（第 {seen_old[key]} 与第 {idx} 条）"
            )
        # 越界检查（物理坐标）
        phys_to_global(old_sizes, mv.old_rg, mv.old_row)
        seen_old[key] = idx
    expected = {
        (rg, row)
        for rg, size in enumerate(old_sizes)
        for row in range(size)
    }
    missing = expected - set(seen_old)
    if missing:
        miss = sorted(missing)
        raise RearrangeError(
            f"遗漏旧行：{len(miss)} 个坐标未出现在搬迁清单中，例如 {miss[:5]}"
        )
    if len(seen_old) != old_num_rows:
        raise RearrangeError("搬迁清单的旧行数与原快照行数不一致")

    # --- 校验：新位置唯一且恰好填满新布局 -------------------------------
    new_total = sum(new_row_group_sizes)
    if any(s <= 0 for s in new_row_group_sizes):
        raise RearrangeError(
            f"新行组大小必须为正整数（避免空行组破坏坐标映射）: {new_row_group_sizes}"
        )
    placed = [mv for mv in moves if mv.new_rg is not None]
    if len(placed) != new_total:
        raise RearrangeError(
            f"新布局容量 {new_total} 行，但搬入行数为 {len(placed)}，"
            "必须恰好填满（不得留空位或多出行）"
        )
    occupied: dict[tuple[int, int], int] = {}
    for idx, mv in enumerate(placed):
        key = (mv.new_rg, mv.new_row)
        if mv.new_rg < 0 or mv.new_rg >= len(new_row_group_sizes):
            raise RearrangeError(f"第 {idx} 条新行组 {mv.new_rg} 超出新布局组数")
        if mv.new_row < 0 or mv.new_row >= new_row_group_sizes[mv.new_rg]:
            raise RearrangeError(
                f"第 {idx} 条新位置 {key} 越界（组 {mv.new_rg} 容量 {new_row_group_sizes[mv.new_rg]}）"
            )
        if key in occupied:
            raise RearrangeError(
                f"重复占用新位置：{key} 被第 {occupied[key]} 与第 {idx} 条同时占用"
            )
        occupied[key] = idx

    # --- 构造新快照行组，并记录 新全局行 -> 旧全局行 的映射 --------------
    new_groups: list[list[dict[str, Any]]] = [[] for _ in new_row_group_sizes]
    # 先按旧全局行号取内容，再按新坐标填充。
    old_flat = base.flatten()
    mapping: list[int | None] = [None] * new_total
    edits_applied = 0
    retired = 0
    for mv in moves:
        old_global = phys_to_global(old_sizes, mv.old_rg, mv.old_row)
        if mv.new_rg is None:
            retired += 1
            continue
        row = dict(old_flat[old_global])
        for col, value in mv.edits.items():
            if col not in base.schema.names:
                raise RearrangeError(f"搬迁项点名了不存在的字段: {col!r}")
            if row[col] != value:
                edits_applied += 1
            row[col] = value
        new_global = phys_to_global(new_row_group_sizes, mv.new_rg, mv.new_row)
        new_groups[mv.new_rg].append((mv.new_row, row))
        mapping[new_global] = old_global

    # 组内按行号排序，保证物理行序 = new_row
    ordered_groups = [
        [row for _, row in sorted(group, key=lambda pair: pair[0])]
        for group in new_groups
    ]

    out_path = ledger.path_for(new_version)
    try:
        write_snapshot(out_path, base.schema, ordered_groups)
    except Exception as exc:  # 类型不匹配等写入问题，整体拒绝
        raise RearrangeError(f"写出新快照失败，重排未生效: {exc}") from exc

    retired_old = sorted(
        phys_to_global(old_sizes, mv.old_rg, mv.old_row)
        for mv in moves if mv.new_rg is None
    )
    rec = VersionRecord(
        version=new_version,
        parents=[base_version],
        kind="rearrange",
        num_rows=new_total,
        row_group_sizes=list(new_row_group_sizes),
        map=mapping,
        retired_parent_rows=retired_old,
        note=note,
    )
    ledger.add(rec)
    ledger.save()

    return RearrangeResult(
        version=new_version,
        path=out_path,
        num_rows=new_total,
        retired=retired,
        edits_applied=edits_applied,
    )


def rearrange_from_plan(plan: dict[str, Any], ledger: Ledger) -> RearrangeResult:
    """从 JSON 搬迁计划对象执行重排。"""
    for key in ("base", "new_version", "row_group_sizes", "moves"):
        if key not in plan:
            raise RearrangeError(f"重排计划缺少字段 {key!r}")
    return apply_rearrange(
        base_version=str(plan["base"]),
        moves=parse_moves(plan["moves"]),
        new_row_group_sizes=[int(s) for s in plan["row_group_sizes"]],
        new_version=str(plan["new_version"]),
        ledger=ledger,
        note=plan.get("note", ""),
    )
