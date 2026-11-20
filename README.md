# Parquet footer 统计驱动的保守 plan/scan

用 Python 3.11 + PyArrow 19 实现的小型 Parquet 查询工具：计划阶段只读 footer 统计、
保守剪枝行组；执行阶段先读过滤列做 SQL 三值逻辑精确过滤，再只为命中行读投影列。
不解析 SQL、不做损坏恢复、不使用 page index、不含远程 Range 服务与性能压测。

## 目录

```
fixtures/                 输入：files.json、queries.json、stats.parquet、no-stats.parquet
tools/make_fixtures.py    从同一份数据生成“有统计/无统计”两个 Parquet（行组与行序一致）
src/pqscan/
  expr.py       谓词树解析、schema 校验、PyArrow 表达式翻译、纯 Python 3VL 逐行求值
  stats.py      只读 footer 的行组统计提取与可靠性判定
  planner.py    三值逻辑保守行组推理（can_be_true / can_be_false、trace）
  scanner.py    两阶段列读取、精确行选择、读取请求在执行入口即时登记
  reference.py  独立核对：无剪枝全扫描 + 纯 Python 3VL（与引擎不同代码路径）
  runner.py     串联计划/扫描/核对，落盘 plans、results、run_record、verification
  versions.py   版本台账：快照血缘与逐格映射，坐标换算
  snapshot.py   快照 Parquet 读写（每行组一个 row group，可再扫描）
  rearrange.py  离线重排：逐行搬迁清单校验与落盘，产出新版本
  locate.py     定位：祖先版坐标 -> 指定后继版现行坐标 / 退役 / 不同链
  patch.py      订正单回放：按行裁决旧值/目标值/冲突，写出新版本
  cli.py        命令行入口
tests/          pytest 套件（12 个原用例 + 15 个重排/定位/订正用例）
docs/plan_format.md  计划与运行记录的 JSON 格式契约
docs/versions_rearrange_patch.md  版本台账、重排、定位、订正回放契约
examples/       命令行与带断言的可复跑样例
output/         运行产物（git 忽略）
```

## 版本、重排与订正回放（新增）

在原有 `run`/`verify` 之外新增离线能力，围绕一个版本台账目录（`versions.json`
+ 各版 `{version}.parquet`）。快照只追加不覆盖，老版可继续按原坐标读取，
新快照也是真正可再扫描的 Parquet：

```bash
PYTHONPATH=src .venv/bin/python -m pqscan.cli init \
    --dir vdir --source fixtures/stats.parquet --version v0
PYTHONPATH=src .venv/bin/python -m pqscan.cli rearrange --dir vdir --plan rearrange.json
PYTHONPATH=src .venv/bin/python -m pqscan.cli locate \
    --dir vdir --from v0 --to v2 --row-group 0 --row 0
PYTHONPATH=src .venv/bin/python -m pqscan.cli patch \
    --dir vdir --patch patch.json --target v2 --new-version p1
```

- **重排**：每个旧行恰好消费一次，可搬入指定新位置或退役，可点名改字段；
  遗漏、重复消费旧行、重复占用新位置、容量不符都整次拒绝，不产生可见版本。
- **定位**：将任一祖先版坐标解析到指定后继版的现行坐标或退役状态，
  区分版本是否在同一祖先链；支持向上与向下两个方向。
- **订正回放**：订正单写明起始版与当时坐标，按行裁决每个字段
  （`现值==旧值` 可改、`现值==目标值` 已达成、否则冲突，NULL 参与相等判断）。
  行内任一冲突则整行不写，其他行仍可成功；成功项写入可扫描的新版本，老版不受影响。
  订正版仍可定位祖先行。

格式契约见 `docs/versions_rearrange_patch.md`，命令行可复跑样例见
`examples/demo_cli.sh`，带断言的 Python 样例见 `examples/demo_rearrange_patch.py`。

## 运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python tools/make_fixtures.py           # 重新生成两个夹具（可选，仓库已带）
PYTHONPATH=src .venv/bin/python -m pqscan.cli run \
    --files fixtures/files.json --queries fixtures/queries.json --out output
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
```

输出：`output/plans/*.json`（行组选择说明）、`output/results/*.parquet`（查询结果）、
`output/run_record.json`（实际列读取请求）、`output/verification.json`（独立核对结论）。

## 支持范围

- 平面 schema；整数（有/无符号 int32/int64 等）与字符串列；
- 比较 `eq/ne/lt/le/gt/ge`、`is_null`、`and/or/not`、列投影；
- 谓词以 JSON 前缀树表达（见 `fixtures/queries.json`），不解析 SQL；
- 类型不匹配、列不存在、嵌套 schema 等在计划期报错；不处理文件损坏。

## 关键设计约束（与需求逐条对应）

1. **计划只读 footer。** `stats.py` 只访问 `FileMetaData/RowGroupMetaData/Statistics`，
   不调用任何数据读取接口。
2. **三值逻辑双结论。** 每个谓词子树求 `can_be_true`/`can_be_false`，
   NULL 产生的 UNKNOWN 既不算 TRUE 也不算 FALSE。`NOT` 同时翻转两个结论，
   不会出现“把 UNKNOWN 当 FALSE 再取反”的错误剪枝（见 `planner.py`）。
3. **统计缺失/不可靠即保守保留。** 无 Statistics、`has_min_max=False`（全 NULL 组的
   典型形态）、`null_count` 缺失、物理类型不受支持，叶子结论退化为
   `(true, true)` 并在 trace 的 `unknown` 中登记原因。PyArrow 19 的 Statistics
   不暴露 min/max 截断标记，因此对存在且类型受支持的 min/max 视为精确；
   若环境提供截断信息，应在 `stats.py` 将其降级为不可靠。
4. **AND/OR 不跨列关联极值。** 只组合独立子树的两个结论，不假设不同列的
   min/max 来自同一行。只有证明 `can_be_true=false` 才 `skip`。
5. **两阶段扫描。** 保留组先读过滤列；`Table.filter` 的 Kleene 语义丢弃
   UNKNOWN/FALSE、只留 TRUE，再用临时行号列取回组内物理行号。无命中组到此结束，
   不读 payload 等投影专用列；有命中组才读剩余投影列。过滤列同时在投影中时复用
   第一阶段数组，不会发起第二次读取（空列集合不产生请求）。
6. **读取计数来自执行入口。** `InstrumentedParquetFile.read_row_group` 在真实
   调用发生时登记 `(文件, 行组, phase, 列集合)`，计划器不参与填写；记录的是工具
   发起的列读取请求，不是操作系统字节数，也不用 footer 的压缩大小冒充 I/O。
7. **结果与溯源。** 输出按 files.json 文件顺序、行组物理索引、组内行号排列；
   附 `_source_file/_source_row_group/_source_row_in_group` 三列；重复数据保留；
   空结果写出同 schema 的零行 Parquet。
8. **独立核对。** `reference.py` 不看任何统计、每个行组全列全量读取，用纯 Python
   3VL 逐行求值，逐行比对引擎结果的内容、顺序与溯源。

## 夹具与现成查询的预期现象

两个 Parquet 由同一份 20 行数据写成，各 5 个行组、每组 4 行；RG2 的 `k` 全 NULL。

| 查询 | stats.parquet 计划 | no-stats.parquet 计划 |
| --- | --- | --- |
| `range`（k∈[14,30)） | 跳过 RG0/RG1/RG4；RG2 全 NULL 保守保留但过滤后无命中；仅 RG3 命中 | 5 组全保留，结果完全一致 |
| `nulls`（k IS NULL） | null_count=0 的 RG1/RG3/RG4 证明跳过；RG0/RG2 命中 | 全保留，结果一致 |
| `negated`（NOT kind='A'） | 全部保留（含 NULL 行，UNKNOWN 不取反为 TRUE） | 全保留，结果一致 |
| `or_missing_stats`（kind='A' OR amount>160） | 全部保留（OR 跨列独立推理） | 全保留，结果一致 |
| `no_hits`（k>1000） | RG0/RG1/RG3/RG4 证明跳过；RG2 统计缺失保守保留、过滤后无命中 | 全保留、全部无命中 |

可在 `output/run_record.json` 验证：`no_hits` 与 `range` 中所有
`no_hits_after_filter` 行组只有 `phase=filter` 的请求，从不读取投影专用列
（`row_id/payload/kind/amount`）；`planned_skip` 行组零请求。

## 测试覆盖

`tests/test_pqscan.py`（12 用例）覆盖：范围剪枝、全 NULL 组、IS NULL 的
null_count 推理、NOT 的 UNKNOWN 处理与可证明 FALSE、OR 跨列不关联极值、
无统计全保留、3VL 真值表、无命中不读投影列、过滤列复用、端到端与无剪枝
全扫描一致、有/无统计结果相同而计划保守度不同、空结果 schema 与输出顺序。

`tests/test_rearrange_patch.py`（15 用例）覆盖：重排的遗漏/重复消费/重复占用/
容量不符整次拒绝、连续两次重排后旧版可读且新快照可再扫描、定位的现行/退役/
不同链/向下回溯、订正的成功/退役/整行冲突/已达/NULL 相等/跨链不可达与
单行冲突不影响其他行。
