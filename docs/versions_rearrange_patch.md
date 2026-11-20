# 版本、重排与订正回放

在原有 `run`/`verify` 之外新增的离线能力，围绕一个**版本台账目录**工作：
目录里有 `versions.json`（台账）和各版本快照 `{version}.parquet`。
每个快照都是真正可再扫描的 Parquet，原 `run` 命令可直接把它当作输入文件。

```
python -m pqscan.cli init      --dir VDIR --source a.parquet --version v0
python -m pqscan.cli rearrange --dir VDIR --plan rearrange.json
python -m pqscan.cli locate    --dir VDIR --from v0 --to v2 --row-group 0 --row 3
python -m pqscan.cli patch     --dir VDIR --patch patch.json --target v2 --new-version p1
```

坐标口径与扫描结果溯源列一致：`(行组物理索引, 组内物理行号)`，写作 `[rg, row]`。
本工具每个快照只有一个文件，因此坐标不含文件名。

## 版本台账 `versions.json`

```json
{
  "versions": [
    {
      "version": "v1",
      "parents": ["v0"],
      "kind": "rearrange",            // genesis | rearrange | patch
      "num_rows": 1,
      "row_group_sizes": [1],
      "map": [0],                     // 本版全局行 -> 父版全局行；null 表示退役
      "retired_parent_rows": [1],     // 父版中被退役的全局行
      "note": "..."
    }
  ]
}
```

`map` 是定位的唯一依据：它把“本版某格来自父版哪一格”逐行记下来。
快照只追加、不覆盖，重排或回放都只是新增一个版本。

## 重排 `rearrange`

计划 JSON：

```json
{
  "base": "v0",
  "new_version": "v1",
  "row_group_sizes": [2, 1],          // 新文件的逐组行数（决定新布局）
  "moves": [
    {"old": [0, 0], "new": [0, 0], "edits": {"label": "B"}},
    {"old": [0, 1], "retire": true},
    {"old": [1, 0], "new": [1, 0]}
  ],
  "note": "可选"
}
```

规则：

- 每个旧物理坐标**恰好出现一次**；`retire: true` 表示退役，否则必须给 `new`；
- 搬迁可对搬过去的行点名改字段（`edits`，值可为 `null`）；
- 新位置必须唯一且恰好填满 `row_group_sizes` 的容量（不得留空位或多出行）；
- 下列任一情况**整次拒绝**，不会产生可见版本：
  遗漏旧行、重复消费同一旧行、重复占用同一新位置、新位置越界或容量不符；
- 以临时文件 + 原子替换写入，写入失败也不会留下半成品。

连续执行两次重排后，`v0`、`v1` 仍保留且可按各自原坐标读取。

## 定位 `locate`

把 `--from` 版中的坐标解析到 `--to` 版：

- `status=live`：给出 `to_coord`（目标版现行坐标）；
- `status=retired`：该行在链上某次重排中退役；
- `status=incomparable`：两版不在同一条祖先链（含兄弟分支），无法定位；
- 支持向上（祖先→后继）与向下（后继→祖先）两个方向。

## 订正回放 `patch`

订正单 JSON：

```json
{
  "base": "v0",
  "rows": [
    {"coord": [0, 0], "fields": [
      {"column": "amount", "old": 5, "new": 7},
      {"column": "label",  "old": "A", "new": "C"}
    ]},
    {"coord": [0, 1], "fields": [
      {"column": "amount", "old": 5, "new": 7}
    ]}
  ]
}
```

裁决（**按行**）：

1. 先把 `base` 版坐标经定位入口换算到 `--target` 现行版的坐标；
   若已退役，整行记为 `retired`，不写任何字段；
2. 对每个字段，比较**目标版现值**：
   - `现值 == old`：可改，`applied`；
   - `现值 == new`：视为已达成，`already`；
   - 其他：`conflict`。NULL 参与相等判断（`null == null` 为真）；
3. 该行只要有任一 `conflict`，**整行所有字段都不写**（本可应用的字段标记为
   `skipped_on_conflict`），该行保留现值；
4. 同一张单的**其他行**继续独立裁决、独立成功。

输出逐行解释（每行的 `status`、`target_coord`、各字段 `current/old/new/status`）。
只要存在可成功写入的字段，就写出一个可扫描的新版本（`--new-version`），
内容为“目标版现值 + 已应用字段”；否则不产生新版本。老版不受回放影响。
订正版在台账中带有指向目标版的映射，仍可用 `locate` 定位祖先行。

## 可复跑样例

`examples/demo_cli.sh` 覆盖需求中的全部场景（两行除位置外完全相同）：

- `v0`：两行 amount=5、label=A；
- 第二行退役、第一行两次搬迁并把 label 改为 B，得到 `v2`；
- 从 `v0` 给 `v2` 下订正单：第一行 amount 5→7 成功，第二行退役；
- 同单再要求 label A→C 时，label 现值 B 冲突 → 整行保留 5/B；
- 另做一条把搬迁中 label 改回 A 的链 `v2a`，同一张双字段单写入 7/C；
- 每一步都用现有 `pqscan run` 扫描入口读回各版结果。

```bash
PYTHON=.venv/bin/python bash examples/demo_cli.sh          # 命令行复跑
PYTHONPATH=src .venv/bin/python examples/demo_rearrange_patch.py   # 带断言的 Python 版
```
