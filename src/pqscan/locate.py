"""定位入口：把任一祖先版中的坐标解析到指定后继版中的现行坐标或退役状态。

只依赖版本台账中的逐格映射，不读取 Parquet 内容。支持：

- 同一祖先链上**向上**解析（祖先坐标 → 后继坐标）：沿父子映射逐版换算，
  任一版该格退役则返回 ``retired``；
- 同一祖先链上**向下**解析（后继坐标 → 祖先坐标）：沿逆映射回溯；
- 不同链或任一方向不可达：返回 ``incomparable``，并说明两版不在同一条祖先链。

坐标口径为 ``(行组, 组内行号)``，与扫描结果的溯源列一致。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import LocateError
from .versions import Ledger, global_to_phys, phys_to_global

RETIRED = "retired"
LIVE = "live"
INCOMPARABLE = "incomparable"


@dataclass
class LocateResult:
    from_version: str
    to_version: str
    status: str                    # "live" | "retired" | "incomparable"
    from_coord: list[int]
    to_coord: list[int] | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_version": self.from_version,
            "to_version": self.to_version,
            "status": self.status,
            "from_coord": list(self.from_coord),
            "to_coord": None if self.to_coord is None else list(self.to_coord),
            "detail": self.detail,
        }


def locate(ledger: Ledger, from_version: str, to_version: str,
           rg: int, row: int) -> LocateResult:
    src_rec = ledger.get(from_version)      # 未知版本 -> LocateError
    dst_rec = ledger.get(to_version)
    from_global = phys_to_global(src_rec.row_group_sizes, rg, row)
    from_coord = [rg, row]

    def done(status: str, to_coord: list[int] | None, detail: str) -> LocateResult:
        return LocateResult(from_version, to_version, status, from_coord, to_coord, detail)

    if from_version == to_version:
        return done(LIVE, list(from_coord), "起止版本相同，坐标不变")

    anc_from = ledger.ancestors(from_version)   # root..from
    anc_to = ledger.ancestors(to_version)       # root..to

    if to_version in anc_from:
        # 向下：to 是 from 的祖先，沿逆映射回溯。
        current = from_global
        for i in range(len(anc_from) - 1, anc_from.index(to_version), -1):
            parent_version = anc_from[i - 1]
            child_rec = ledger.get(anc_from[i])
            parent = child_rec.map[current]
            if parent is None:
                return done(
                    INCOMPARABLE, None,
                    f"版本 {anc_from[i]} 的坐标 {current} 不存在于祖先 {parent_version}",
                )
            current = parent
        return _resolve(dst_rec, to_version, current, done, "向下")

    if from_version in anc_to:
        # 向上：from 是 to 的祖先，沿正向映射前进。
        current = from_global
        for i in range(anc_from.index(from_version) + 1, len(anc_to)):
            ver = anc_to[i]
            rec = ledger.get(ver)
            if current in rec.retired_parent_rows:
                return done(
                    RETIRED, None,
                    f"坐标在版本 {ver} 中已退役",
                )
            matches = [j for j, parent in enumerate(rec.map) if parent == current]
            if not matches:
                return done(
                    RETIRED, None,
                    f"坐标在版本 {ver} 中无现行对应格（视作退役）",
                )
            if len(matches) > 1:
                return done(
                    INCOMPARABLE, None,
                    f"版本 {ver} 的映射出现歧义，拒绝定位",
                )
            current = matches[0]
        return _resolve(dst_rec, to_version, current, done, "向上")

    return done(
        INCOMPARABLE, None,
        f"版本 {from_version} 与 {to_version} 不在同一条祖先链上，无法定位",
    )


def _resolve(dst_rec, to_version, current, done, direction):
    try:
        out_coord = list(global_to_phys(dst_rec.row_group_sizes, current))
    except LocateError:
        return done(RETIRED, None, f"全局行 {current} 超出目标版布局")
    return done(LIVE, out_coord, f"{direction}解析到版本 {to_version} 的现行位置 {out_coord}")
