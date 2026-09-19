# 本地 CLI 与服务契约

在现有项目根目录和 `main` 执行，使用项目 `.venv`。`--private-dir` 只能选择本项目 `.private/` 下的数据域。服务层测试可使用本项目 `.cache/` 中的合成隔离域。不能以参数把主目录或其他项目当成私人目录。

| 操作 | 行为 |
|---|---|
| `ingest` | 在锁内读取配置，将同一份受监督输入快照解析和哈希；全部成功后发布不可变版本与 `current.json` |
| `check-transcript` | 检查已有课表；诊断在独立 `checks/`，不发布新课表或冻结版本 |
| `select [--new-version]` | 复用有效冻结或显式运行新选择；实际采用响应绑定唯一运行 |
| `generate --task-id ID [--seconds N] [--workers N]` | 先比较请求身份；已有完整结果重验后幂等复用；未完成任务恢复验证过的两份布局 |
| `status ID` | 完整清单优先；失去确切进程所有者的非终态显示 `INTERRUPTED` |
| `cancel ID` | 对当前 attempt 写取消标记；已完成发布返回 `COMPLETE` |
| `verify ID` | 根据结果自身快照、严格清单和最终 PDF 字节重验 |
| `rebuild ID --new-task-id NEW` | 从保存布局重建，不调用模型或求解器 |
| `metrics` | 读取当前冻结版本明确引用的模型证据，旧证据不猜测 latest |
| `private-test ID --allow-private-integration` | 显式私人验收；不把自动验证标成人工查看 |
| `privacy-scan [--packages DIR]` | 分别审计工作区、真实索引 blob、wheel/sdist 内容，不修改 Git |

配置中的 `solver.optimization_seconds` 默认为短预算，设为 `0` 可关闭。`resources` 使用 `limits.Limits` 的有限数值范围配置 PDF 页数、文件字节、页面尺寸、渲染像素、字符/对象、IPC、输出、CPU/墙钟、地址空间、fd、单文件和临时总量。`total_seconds` 和 `stage_seconds` 控制业务生命周期；NaN、无限值和越界值均拒绝。

服务层稳定入口为 `prepare_request(store, task_id)`、`generate`、`task_status`、`cancel_attempt`、同 ID 重试、`verify_set` 和 `result_files`。文件描述只包含两份受验证文件的编号、名称、字节数和哈希；未来 Web 适配器按 task ID 和编号读取，不能接受客户端任意路径。当前没有队列、常驻 worker、SQLite 调度或生产启动器。

`TASK_INPUT_CHANGED` 必须由调用者明确处理，服务不偷偷换 ID。延长预算或变更线程不会制造新的交付身份。`UNKNOWN`/超时不能标成 `INFEASIBLE`。`RESOURCE_LIMIT`、`WORKER_FAILED`、`MODEL_SELECTION_INVALID`、`CANCELLED`、输入错误和内部异常分别记录；只有完整原子目录发布才代表生成完成。

退出码：0 成功；2 规则错误；3 输入不可用或 schema 无效；5 忙；6 UNKNOWN；7 INFEASIBLE；8 MODEL_INVALID；9 RESOURCE_LIMIT；10 WORKER_FAILED；11 PUBLISH_FAILED；70 未处理内部错误；130 取消。

取消模型请求只关闭本任务客户端/连接，不停止共享 Ollama，也不保证服务端立即停止已发出的推理。已取消尝试不能冻结迟到响应。重试保留旧尝试证据，不删除新尝试收到的取消标记。
