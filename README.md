# 修正电子部件良率把测量点当成器件的问题基础平台

本项目是一套可离线运行的 Python 服务端平台，服务于具身智能机器人控制域、AI 计算域和国产电子部件质量域。平台管理控制计算节点、实时控制总线、资源时隙、机器人构建、验证测量、分析租约、部件批次与质量决定，业务状态、幂等结果和审计事件保存在 SQLite 中。

## 目录

- `src/robot_control/`：控制节点、实时总线、资源批次、时隙申请、容量分配和架构情景；
- `src/embodied_ai/`：机器人构建、验证协议、测量导入、排除复核、分析任务和准入决定；
- `src/component_qualification/`：国产电子部件批次、器件台账、测量序列、器件级良率和质量审批；
- `fixtures/`：离线验收使用的验证协议与结构化测量；
- `tests/`：领域规则、事务、权限、HTTP API 和命令行验收测试。

## 电子部件良率模型（device-yield/2）

旧报告把每个"达标测量点"计为一颗合格器件：两颗样件各测三个频点会得到 6 个
"合格器件"，良率超过 100% 或以计数不一致直接失败。模型已修正为三层身份：

1. **器件身份（unit_id）**：良率唯一计数对象，批次先登记器件台账，分母恒为登记器件数；
2. **测量序列（sequence_no / measured_at）**：测量挂在器件上，支持重复测量与仪器复测；
3. **批次数量（unit_count）**：只用于核对台账，登记数量不符拒绝分析。

冻结的 v2 规则：

- 同器件同频点重复测量，按 `sequence_no 最大 → 测量时间最新 → measurement_id 最小`
  确定性取唯一有效值，较早记录标记为 `superseded`；不同仪器复测同规则仲裁；
- 被撤销（revoke）的测量只追加标记、不删除、不参与判定，重复撤销返回业务冲突；
- 必测频点（默认 450/520/650 Hz）缺失任一 → 器件结论 `unknown`；
- 频点齐全且响应全部 `>= 阈值`（默认 0.8）→ `pass`，任一不达标 → `reject`；
- 批次结果为 pass/reject/unknown 三类计数，比例之和恒为 1。

分析结果按 `(规则版本, 参数+输入摘要)` 不可变去重留存在 `lot_analyses`；复测或撤销
产生新版本，旧分析与旧放行决定原样保留（`lot_decisions` 只追加），不会被静默改写。
历史算法保留在 `analytics.py`（`point-count-yield/1`），可通过 `analysis-legacy`
端点复算留档对照；其计数矛盾被转译为 `invalid_lot_state` 业务错误而非底层异常。

每条良率都可追溯：分析结果含每颗器件的结论、缺失/失败频点以及 selected /
superseded / revoked / extra 四类逐点证据（含 measurement_id 与仪器），
审计事件 `analysis.recorded` 同样携带器件级结论。

### 质量域接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/lots` | 建批（声明器件数量） |
| POST | `/lots/{id}/units` | 登记器件台账（数量必须与声明一致） |
| GET | `/lots/{id}/units` | 器件台账 |
| POST | `/lots/{id}/measurements` | 提交测量（unit_id、频点、响应、仪器、可选序列号） |
| POST | `/measurements/revoke` | 撤销测量（原因必填，不可重复撤销） |
| POST | `/lots/{id}/analysis` | 按 device-yield/2 计算并留档 |
| POST | `/lots/{id}/analysis-legacy` | 按旧算法复算留档（仅对照） |
| GET | `/lots/{id}/analyses` | 分析版本清单（含各自放行决定） |
| GET | `/analyses/{id}` | 单个分析版本与器件级证据 |
| POST | `/lots/{id}/decisions` | 基于指定分析版本放行/暂缓/拒收 |
| GET | `/lots/{id}/audit` | 审计事件 |

无效样本（NaN/Inf、未登记器件、空白仪器）返回 `422 invalid_sample`；
台账矛盾返回 `422 invalid_lot_state`；重复登记、重复撤销、重复决定返回
`409 conflict`；未知资源返回 `404 not_found`。

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

三条命令会在临时 SQLite 数据库中完成控制资源分配、AI 验证分析和国产电子部件质量流程，不访问外部网络。部件域场景为两颗样件：U1 三频点齐全且 520 Hz 在另一台仪器复测达标（pass），U2 缺 650 Hz（unknown），批次良率 0.5，旧版测量点计数以业务错误拒绝，最终暂缓放行。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m robot_control.api --database robot-control.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m embodied_ai.api --database embodied-ai.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m component_qualification.api --database component.sqlite3 --host 127.0.0.1 --port 8082
```

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。
