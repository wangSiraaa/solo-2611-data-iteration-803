"""快照读写：把内存中的行（按行组划分）写成可再扫描的 Parquet。

一个快照 = 一个 Parquet 文件，行组与组内行序是物理坐标 ``(行组, 组内行号)``
的来源。:
  - :func:`read_snapshot` 读出全部行组及其行，用于重排/回放前取旧内容；
  - :func:`write_snapshot` 按给定的行组列表写出新 Parquet（每组一个 row group），
    保证新文件仍可被 pqscan 的扫描入口与 footer 统计读取。

为支持“以某版坐标为起点的行”，本模块还负责把版本坐标解析成具体的行内容。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .errors import LocateError


@dataclass
class Snapshot:
    """一个已落盘快照的内存视图。"""

    path: str
    schema: pa.Schema
    row_groups: list[list[dict[str, Any]]]   # 每组一组行（dict）

    @property
    def row_group_sizes(self) -> list[int]:
        return [len(g) for g in self.row_groups]

    @property
    def num_rows(self) -> int:
        return sum(self.row_group_sizes)

    def flatten(self) -> list[dict[str, Any]]:
        return [row for group in self.row_groups for row in group]


def read_snapshot(path: str) -> Snapshot:
    pf = pq.ParquetFile(path)
    schema = pf.schema_arrow
    groups: list[list[dict[str, Any]]] = []
    for rgi in range(pf.num_row_groups):
        groups.append(pf.read_row_group(rgi).to_pylist())
    return Snapshot(path=path, schema=schema, row_groups=groups)


def write_snapshot(path: str, schema: pa.Schema,
                   row_groups: list[list[dict[str, Any]]]) -> None:
    """写出快照；每个内层列表对应一个新行组。

    以临时文件 + 原子替换落盘，确保失败时不会留下半成品可见版本。
    """
    os_tmp = path + ".tmp"
    writer = pq.ParquetWriter(os_tmp, schema, write_statistics=True)
    try:
        for group in row_groups:
            table = pa.Table.from_pylist(group, schema=schema)
            writer.write_table(table)
    finally:
        writer.close()
    os.replace(os_tmp, path)


def resolve_coord(snapshot: Snapshot, rg: int, row: int) -> dict[str, Any]:
    if rg < 0 or rg >= len(snapshot.row_groups):
        raise LocateError(f"坐标 ({rg},{row}) 的行组越界：快照 {snapshot.path} 共 "
                          f"{len(snapshot.row_groups)} 组")
    group = snapshot.row_groups[rg]
    if row < 0 or row >= len(group):
        raise LocateError(f"坐标 ({rg},{row}) 的组内行号越界：该组 {len(group)} 行")
    return group[row]
