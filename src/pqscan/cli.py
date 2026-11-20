"""命令行入口。

用法：
    python -m pqscan.cli run --files fixtures/files.json --queries fixtures/queries.json \
        --out output
    python -m pqscan.cli verify --files ... --queries ... --out output   # 只跑独立核对

版本/订正（均围绕一个目录下的版本台账 versions.json）：
    python -m pqscan.cli init --dir vdir --source a.parquet --version v0
    python -m pqscan.cli rearrange --dir vdir --plan rearrange.json
    python -m pqscan.cli locate --dir vdir --from v0 --to v2 --row-group 0 --row 0
    python -m pqscan.cli patch --dir vdir --patch patch.json --target v2 --new-version v3
"""
from __future__ import annotations

import argparse
import json
import sys

from .errors import PqScanError
from .expr import parse_queries, validate, validate_projection
from .reference import reference_scan
from .runner import run


def _load_json_file(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _cmd_run(args: argparse.Namespace) -> int:
    doc = run(args.files, args.queries, args.out)
    all_ok = all(q["passed"] for q in doc["verification"]["queries"])
    for q in doc["verification"]["queries"]:
        flag = "PASS" if q["passed"] else "FAIL"
        print(f"[verify:{flag}] {q['query_id']}: {q['detail']} "
              f"(engine={q['engine_rows']}, reference={q['reference_rows']})")
    print(f"\n计划与结果已写入 {args.out}/ (plans/、results/、run_record.json、verification.json)")
    return 0 if all_ok else 2


def _cmd_verify(args: argparse.Namespace) -> int:
    import os
    import pyarrow.parquet as pq

    fixture_dir = os.path.dirname(os.path.abspath(args.files))
    with open(args.files, encoding="utf-8") as fh:
        files = [os.path.join(fixture_dir, n) for n in json.load(fh)]
    with open(args.queries, encoding="utf-8") as fh:
        queries = parse_queries(json.load(fh))

    failed = 0
    for query in queries:
        for path in files:
            validate(query.where, pq.ParquetFile(path).schema_arrow)
            validate_projection(query.columns, pq.ParquetFile(path).schema_arrow)
        ref = reference_scan(query, files)
        result_path = os.path.join(args.out, "results", f"{query.id}.parquet")
        got = pq.read_table(result_path)
        same = got.schema == ref.schema and got.to_pylist() == ref.to_pylist()
        print(f"[{'PASS' if same else 'FAIL'}] {query.id}: {got.num_rows} 行")
        failed += 0 if same else 1
    return 0 if failed == 0 else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pqscan", description="Parquet footer 统计驱动的保守 plan/scan")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="计划 + 扫描 + 独立核对")
    p_run.add_argument("--files", required=True)
    p_run.add_argument("--queries", required=True)
    p_run.add_argument("--out", default="output")
    p_run.set_defaults(func=_cmd_run)

    p_ver = sub.add_parser("verify", help="对已产出的结果再跑一次无剪枝全扫描核对")
    p_ver.add_argument("--files", required=True)
    p_ver.add_argument("--queries", required=True)
    p_ver.add_argument("--out", default="output")
    p_ver.set_defaults(func=_cmd_verify)

    p_init = sub.add_parser("init", help="把一个既有 Parquet 登记为版本链起点")
    p_init.add_argument("--dir", required=True, help="版本台账目录（生成 versions.json 与快照副本）")
    p_init.add_argument("--source", required=True)
    p_init.add_argument("--version", required=True)
    p_init.add_argument("--note", default="")
    p_init.set_defaults(func=_cmd_init)

    p_rear = sub.add_parser("rearrange", help="按搬迁清单离线重排，产出新版本")
    p_rear.add_argument("--dir", required=True)
    p_rear.add_argument("--plan", required=True, help="重排计划 JSON")
    p_rear.set_defaults(func=_cmd_rearrange)

    p_loc = sub.add_parser("locate", help="把祖先版坐标解析到后继版现行坐标或退役状态")
    p_loc.add_argument("--dir", required=True)
    p_loc.add_argument("--from", dest="from_version", required=True)
    p_loc.add_argument("--to", dest="to_version", required=True)
    p_loc.add_argument("--row-group", type=int, required=True)
    p_loc.add_argument("--row", type=int, required=True)
    p_loc.set_defaults(func=_cmd_locate)

    p_patch = sub.add_parser("patch", help="回放订正单，裁决并写出新版本")
    p_patch.add_argument("--dir", required=True)
    p_patch.add_argument("--patch", required=True, help="订正单 JSON")
    p_patch.add_argument("--target", required=True, help="订正裁决所依据的现行版本")
    p_patch.add_argument("--new-version", required=True, help="成功写入时产生的新版本名")
    p_patch.set_defaults(func=_cmd_patch)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except PqScanError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


def _cmd_init(args: argparse.Namespace) -> int:
    from .versions import register_genesis

    rec = register_genesis(args.dir, args.source, args.version, args.note)
    print(f"已登记起点版本 {rec.version}: {rec.num_rows} 行，行组 {rec.row_group_sizes}")
    return 0


def _cmd_rearrange(args: argparse.Namespace) -> int:
    from .rearrange import rearrange_from_plan
    from .versions import Ledger

    result = rearrange_from_plan(_load_json_file(args.plan), Ledger(args.dir))
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


def _cmd_locate(args: argparse.Namespace) -> int:
    from .locate import locate
    from .versions import Ledger

    result = locate(Ledger(args.dir), args.from_version, args.to_version,
                    args.row_group, args.row)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.status != "incomparable" else 3


def _cmd_patch(args: argparse.Namespace) -> int:
    from .patch import replay_patch
    from .versions import Ledger

    result = replay_patch(_load_json_file(args.patch), args.target, Ledger(args.dir),
                          args.new_version)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    failed = any(r.status == "conflict" for r in result.rows)
    return 0 if result.new_version is not None and not failed else 4


if __name__ == "__main__":
    raise SystemExit(main())
