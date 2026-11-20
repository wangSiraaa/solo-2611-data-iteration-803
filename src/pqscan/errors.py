"""异常类型。"""
from __future__ import annotations


class PqScanError(Exception):
    """所有工具层错误的基类（不用于损坏 Parquet 恢复，仅做输入校验）。"""


class PlanError(PqScanError):
    """查询表达式或 schema 不支持。"""


class RearrangeError(PqScanError):
    """搬迁清单不完整、重复或新位置冲突，整次重排被拒绝。"""


class PatchError(PqScanError):
    """订正单结构非法，或起始版不是目标版的祖先。"""


class LocateError(PqScanError):
    """版本/坐标无法定位（未知版本、坐标越界等）。"""
