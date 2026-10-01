# 修正电子部件良率把测量点当成器件的问题基础平台

本项目是一套可离线运行的 Python 服务端平台，服务于具身智能机器人控制域、AI 计算域和国产电子部件质量域。平台管理控制计算节点、实时控制总线、资源时隙、机器人构建、验证测量、分析租约、部件批次与质量决定，业务状态、幂等结果和审计事件保存在 SQLite 中。

## 目录

- `src/robot_control/`：控制节点、实时总线、资源批次、时隙申请、容量分配和架构情景；
- `src/embodied_ai/`：机器人构建、验证协议、测量导入、排除复核、分析任务和准入决定；
- `src/component_qualification/`：国产电子部件批次、器件身份、测量序列（含复测/撤销）、版本化器件良率分析、账号权限和质量审批；
- `fixtures/`：离线验收使用的验证协议与结构化测量；
- `tests/`：领域规则、事务、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m robot_control.acceptance --workspace .
PYTHONPATH=src python3 -m embodied_ai.acceptance --workspace .
PYTHONPATH=src python3 -m component_qualification.acceptance
```

三条命令会在临时 SQLite 数据库中完成控制资源分配、AI 验证分析和国产电子部件质量流程，不访问外部网络。

## 部件良率口径（device-yield/2）

`component_qualification` 明确区分三个层次，修正“把每个达标测量点当作一颗合格器件”的缺陷：

- **器件身份**：批次登记 `unit_count` 颗器件（`chip_devices`），一颗器件一行；
- **测量序列**：每次测量一行（`measurements`），带 `device_id`、`sequence_no`、`instrument`、撤销状态；
- **批次数量**：良率分母恒为批次登记的器件数。

单颗器件先按冻结规则（`rules.py`，版本 `device-yield/2`，计划频点 450/520/650 Hz，门限 0.8）归并出唯一结论，再汇总批次：

- `pass`：计划内所有频点都有有效值且全部达标；
- `reject`：所有频点都有有效值，但至少一个不达标；
- `unknown`：至少一个频点没有有效值（缺失、全部撤销或未测）。

边界处理都是确定的：重复测量/跨仪器复测按 `sequence_no` 取最新有效值（序号相同再比时间、测量编号）；撤销的测量留痕但不参与判定（缺失记 `unknown` 而非按坏读数拒绝）；计划外频点、非有限数值、未知器件等无效样本返回业务错误（`422 invalid_sample` / `404 not_found`），不会让底层计算异常逃逸成 500。

分析结果（`analyses`）按输入摘要 `input_sha256` 去重、写入即不可变；响应中的 `traceability` 把一项良率追溯到具体的 `passed/rejected/unknown_device_ids` 及每颗器件的逐频点结论（含采纳的 measurement_id 与仪器）。

历史算法 `point-count/1` 冻结在 `legacy.py`，`GET /lots/{id}/legacy-report` 只读复现旧报告及其输入摘要；旧批次迁移后标记为 `point-count/1`，新分析拒绝在其上运行，**不会静默改写旧决定**。新分析只使用 `device-yield/2`。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m robot_control.api --database robot-control.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m embodied_ai.api --database embodied-ai.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m component_qualification.api --database component.sqlite3 --host 127.0.0.1 --port 8082
```

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。

部件质量服务（8082）主要接口：`POST /lots`（含 `unit_count` 与 `device_ids`）、`POST /lots/{id}/measurements`（含 `device_id`、可选 `sequence_no`）、`POST /measurements/retract`、`POST /lots/{id}/analysis`、`GET /lots/{id}/analyses[/{n}]`、`GET /lots/{id}/devices`、`GET /lots/{id}/measurements`、`GET /lots/{id}/legacy-report`。
