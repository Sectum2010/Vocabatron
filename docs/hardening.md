# 核心加固回归矩阵

本文件仅使用去标识化描述。私人原件、候选、命名、运行指标及故障差异图保存在忽略的数据域。机器上的实际验收结论以本轮私人报告为准，不以测试代码的存在代替执行。

| 项目 | 缺陷与复现 | 修复入口 | 回归证据 | 兼容语义 | 实际状态 |
|---|---|---|---|---|---|
| H01 | 字符计数相同仍可能发生转置、候选换序和字段交换 | `transcription.ordered_evidence`、`ingest.extract_local` | 合成主词/候选转置、候选顺序、跨字段、标点、重复单元格及实际原件检查 | 保留全部字段和原标签；仅定义的排版空白可不同 | FIXED_AND_VERIFIED |
| H02 | 字形内部覆盖仍留少量墨迹，旧检查接受 | `pdf_validation.reference_items/verify_local` | 内容流不可见/透明/错位/重复/裁剪、错误 glyph 加正确 Unicode、局部白黑覆盖、双渲染器参考比较 | 原模板与正常旧 PDF 必须通过；人工状态独立 | FIXED_AND_VERIFIED |
| H03 | 已完成任务提前返回、空快照字典绕过验证 | `requests.request_identity`、`manifest.Manifest`、`services.generate/_read_result` | 身份语义变化、预算不影响身份、必需快照/任务 ID/文件名/跨对象负向测试 | 历史验证读取自身快照；旧清单严格校验后推导 | FIXED_AND_VERIFIED |
| H04 | 检查重新导入并覆盖业务状态 | `ingest.extract/check_transcription`、`services.import_lesson` | 检查前后哈希、导入指针发布失败、旧冻结保持 | 旧正式文件只读；新导入使用版本目录和单一指针 | FIXED_AND_VERIFIED |
| H05 | 宽度异常跳过字号回退；保守估算误删解 | `pdf.plan/clue_capacity`、`solver.solve_pair` | 11.5/11pt 回退、单栏可放、39词共享起点、最终精确排版 | 必要下界只作安全约束，实际排版失败排除具体布局继续求解 | FIXED_AND_VERIFIED |
| H06 | 响应文件覆盖、按最大 attempt 猜测 | `clues.select`、`evidence.verify_evidence` | 旧 attempt 1/新 attempt 0、批次数变化、取消、缺失/篡改/跨版本证据 | 每次运行不可变；旧证据不足时明确标识，结构有效性单独保留 | FIXED_AND_VERIFIED |
| H07 | 扫描工作区代替索引内容 | `privacy.scan/git_read/inspect_archive` | 内存对象模拟真实 cat-file 请求、删除/安全工作区、冲突/链接/索引变化、包限额 | 不扫描或重写用户授权的作者和提交说明；内容中的秘密无此例外 | FIXED_AND_VERIFIED |
| H08 | PDF 解析渲染缺少可终止边界 | `supervisor`、`worker_launcher`、`worker`、`limits` | 真进程挂起/后代/超输出/资源上限、畸形文档、取消和后续恢复 | 资源监督不是完整 OS 沙箱；不修改系统策略 | FIXED_AND_VERIFIED |
| H09 | 阶段心跳、取消竞态、只恢复首份、异常悬挂 | `execution.Execution`、服务操作和发布锁 | 实际 SIGTERM/SIGKILL、尝试隔离、发布前取消、发布后完成权威、导出失败恢复两份 | 同 ID 重试已验证检查点；坏检查点重验拒绝；不恢复搜索树 | FIXED_AND_VERIFIED |
| H10 | 缺少可关闭、有预算的美观阶段 | `optimization.optimize_pair` | 零预算、耗尽、居中改善、退化/副本/排版失败/取消 | 先保存可行双份；交叉、紧凑、居中依次优化，不宣称未证明的最优 | FIXED_AND_VERIFIED |
| H11 | 只有带见证布局测试、缺少普通主流程证据 | `tests/test_end_to_end.py` 与私人集成报告 | 7/20/39 词 import→协议选择→正常 generate→verify→rebuild→幂等/变输入 | 见证只证明样本存在两解，绝不传给主流程；不推算任意课程成功率 | FIXED_AND_VERIFIED |

每项实际状态仅能为 `FIXED_AND_VERIFIED`、`ALREADY_FIXED_WITH_REGRESSION_EVIDENCE` 或 `BLOCKED`。最终报告会写明执行命令、失败后的修复与复测，以及仍未覆盖的限制。

本轮原有公共测试先运行并通过 66 项；修复后的完整套件通过 166 项。此后新增负向用例与修复分别复测：求解、资源和协议 70 项，普通端到端与生命周期 36 项，最终资源监督与租约检查 19 项，均通过。这些测试组存在重叠，不能相加作为独立用例总数。7、20、39 词端到端均走正常求解入口，没有传入预制布局。失败复现、修复过程和命令输出保留在私人验收记录中。

上述状态只覆盖本轮核心与本地验收。新输出人工查看状态单独记录，自动渲染检查不能代替人工查看；本轮没有实施或验收 Web/PWA、网络入口、常驻服务和重启后调度恢复。
