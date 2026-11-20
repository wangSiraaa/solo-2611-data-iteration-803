"""版本台账：记录每个快照版本的物理坐标与祖先血缘。

每次重排或回放订正都会写出一个**新的** Parquet 文件（不改动任何既有快照），
并在台账中追加一条版本记录。版本记录包含：

- ``version``：版本名（文件名即 ``{version}.parquet``）；
- ``parents``：直接父版本（重排/订正均为单父，起版无父）；
- ``map``：本版坐标 → 父版坐标的逐格映射，``null`` 表示该格为“退役”。
  物理坐标按 ``[文件行组, 组内行号]`` 记（本工具每个快照只有一个文件）。
  该映射是定位算法的唯一依据，天然支持“某旧坐标所指内容已被重排搬走”。
- ``retired_parent_rows``：父版中因重排而退役、在本版无对应格的旧坐标。

台账是普通 JSON 文件，与 Parquet 快照放在同一目录；定位入口只读台账即可
在祖先链之间换算坐标，不依赖快照内容。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from .errors import LocateError

LEDGER_NAME = "versions.json"
GENESIS = "genesis"


@dataclass
class VersionRecord:
    version: str
    parents: list[str]
    kind: str                      # "genesis" | "rearrange" | "patch"
    num_rows: int
    row_group_sizes: list[int]
    map: list[list[int] | None]    # 本版全局行 -> 父版全局行 / None(退役)
    retired_parent_rows: list[int] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "parents": list(self.parents),
            "kind": self.kind,
            "num_rows": self.num_rows,
            "row_group_sizes": list(self.row_group_sizes),
            "map": self.map,
            "retired_parent_rows": list(self.retired_parent_rows),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "VersionRecord":
        return cls(
            version=d["version"],
            parents=list(d["parents"]),
            kind=d["kind"],
            num_rows=d["num_rows"],
            row_group_sizes=list(d["row_group_sizes"]),
            map=list(d["map"]),
            retired_parent_rows=list(d.get("retired_parent_rows", [])),
            note=d.get("note", ""),
        )


# ---------------------------------------------------------------------------
# 物理坐标 <-> 全局行号。物理坐标沿用 pqscan 溯源列的口径：
# (行组物理索引, 组内物理行号)。快照内只有一个文件，故不含文件名。
# ---------------------------------------------------------------------------

def global_to_phys(row_group_sizes: list[int], g: int) -> tuple[int, int]:
    if g < 0 or g >= sum(row_group_sizes):
        raise LocateError(f"全局行号 {g} 越界（共 {sum(row_group_sizes)} 行）")
    rgi = 0
    while g >= row_group_sizes[rgi]:
        g -= row_group_sizes[rgi]
        rgi += 1
    return rgi, g


def phys_to_global(row_group_sizes: list[int], rg: int, row: int) -> int:
    if rg < 0 or rg >= len(row_group_sizes):
        raise LocateError(f"行组 {rg} 越界（共 {len(row_group_sizes)} 组）")
    if row < 0 or row >= row_group_sizes[rg]:
        raise LocateError(f"行组 {rg} 内行号 {row} 越界（该组 {row_group_sizes[rg]} 行）")
    return sum(row_group_sizes[:rg]) + row


class Ledger:
    """版本台账的内存视图，可读写 JSON 文件。"""

    def __init__(self, directory: str):
        self.directory = directory
        self.path = os.path.join(directory, LEDGER_NAME)
        self.records: dict[str, VersionRecord] = {}
        if os.path.exists(self.path):
            self._load()

    def _load(self) -> None:
        with open(self.path, encoding="utf-8") as fh:
            doc = json.load(fh)
        for item in doc.get("versions", []):
            rec = VersionRecord.from_dict(item)
            self.records[rec.version] = rec

    def save(self) -> None:
        os.makedirs(self.directory, exist_ok=True)
        doc = {
            "note": "版本台账：记录每版物理坐标与祖先映射，供定位入口换算坐标。",
            "versions": [r.to_dict() for r in self.records.values()],
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def path_for(self, version: str) -> str:
        return os.path.join(self.directory, f"{version}.parquet")

    def get(self, version: str) -> VersionRecord:
        if version not in self.records:
            raise LocateError(
                f"未知版本 {version!r}；台账 {self.path} 中现有版本: {sorted(self.records)}"
            )
        return self.records[version]

    def add(self, rec: VersionRecord) -> None:
        if rec.version in self.records:
            raise LocateError(f"版本 {rec.version!r} 已存在，拒绝覆盖（快照不可变）")
        self.records[rec.version] = rec

    # -- 血缘 ---------------------------------------------------------------

    def ancestors(self, version: str) -> list[str]:
        """从根到该版本的祖先链（含自身），用于判定两条版本是否同链。"""
        rec = self.get(version)
        if not rec.parents:
            return [version]
        # 单父链；若未来多父，取第一条父链即可（本工具不产生多父）。
        return self.ancestors(rec.parents[0]) + [version]

    def is_ancestor(self, older: str, newer: str) -> bool:
        """older 是否为 newer 的祖先（或自身）。"""
        try:
            return older in self.ancestors(newer)
        except LocateError:
            return False


def register_genesis(directory: str, source_parquet: str, version: str,
                     note: str = "") -> VersionRecord:
    """把一个既有 Parquet 文件登记为某版本的起点快照。

    复制到 ``{directory}/{version}.parquet``，并在台账登记一条无父记录；
    其 ``map`` 为恒等映射（本版每行对应自身）。
    """
    import shutil

    import pyarrow.parquet as pq

    ledger = Ledger(directory)
    if version in ledger.records:
        raise LocateError(f"版本 {version!r} 已存在，拒绝覆盖")
    os.makedirs(directory, exist_ok=True)
    dest = ledger.path_for(version)
    shutil.copyfile(source_parquet, dest)
    pf = pq.ParquetFile(dest)
    sizes = [pf.metadata.row_group(i).num_rows for i in range(pf.num_row_groups)]
    total = sum(sizes)
    rec = VersionRecord(
        version=version,
        parents=[],
        kind="genesis",
        num_rows=total,
        row_group_sizes=sizes,
        map=list(range(total)),
        retired_parent_rows=[],
        note=note,
    )
    ledger.add(rec)
    ledger.save()
    return rec
