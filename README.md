# Vocabatron

Vocabatron 是本机运行的私人课程库与填字应用。它保留 PDF 文本层、原文字段和来源证据，由本机 `gemma4:31b` 选择已有候选，再冻结线索。CP-SAT 在完整位置域中搜索新结构；独立验证器检查布局和最终两页 PDF。

英文 React/PWA、持久任务队列和全历史结构档案已实现。私有服务部署需要单独批准。实现与验收范围以 [交付矩阵](docs/web-delivery.md) 为准；实际部署地址、运行状态和最终回归记录保存在私人交付报告中。

## 数据与部署

遵守 [AGENTS.md](AGENTS.md)。真实材料、配置、授权名单、数据库、模型证据、日志和已验证原版只保存在忽略的私人目录中。`Outputs/` 仅保存最终 PDF 的独立副本。删除导出不删除内部历史，不重置编号。

生产使用显式代码、数据、运行、数据库、导出、静态资产和 HTTPS 基址配置。配置通过 `VOCABATRON_CONFIG` 指向私人 JSON；字段由 `vocabatron.app.config.AppConfig` 验证。不要把实际配置加入公开仓库。系统服务和 Tailscale Serve 变更必须按 [部署说明](deploy/README.md) 单独审批。

所有获准 Tailscale 身份映射到同一个 owner，共用课程、变体和带版本的设置。服务仅监听回环地址。Tailscale Serve 是唯一远程入口；不使用 Funnel、公共转发、外部 PDF 服务或云模型。

## 课程和结果

上传只接收有大小限制的原始字节并排队。每课按顺序、表格字段与 Poppler 独立来源证据核验后发布；不使用 OCR，也不由模型改写词表。多课文档部分失败时保留其他已验证课程，并显示具体问题。

内容身份不包含物理页号、坐标和文件名。重新包装同一课程只新增来源绑定，不重置冻结线索或历史。结构档案同时排除交叉关系重复和带字母及所有者信息的几何对称重复。

默认每课请求两份，可输入任意可精确表示的正整数，或查找全部剩余结构。搜索超时是 UNKNOWN，只有完整 CP-SAT 剩余域结论才允许标记 Exhausted。每份 PDF 独立验证、持久登记和导出；完成一部分即可下载。

## CLI

CLI 与网页使用同一持久队列和资源规则，不直接启动绕过历史与准入的旧生成流程。先配置 `VOCABATRON_CONFIG`，再使用：

```sh
.venv/bin/vocabatron library
.venv/bin/vocabatron ingest --source .private/sources/invented-lesson.pdf
.venv/bin/vocabatron generate --count 4
.venv/bin/vocabatron generate --all
.venv/bin/vocabatron status TASK_ID
.venv/bin/vocabatron pause TASK_ID
.venv/bin/vocabatron resume TASK_ID
.venv/bin/vocabatron cancel TASK_ID
.venv/bin/vocabatron restore
```

课程不止一份时，在命令前指定 `--lesson-id FULL_CONTENT_ID`。任务 ID 使用提交返回的持久 ID。`--task-id` 为生成提交提供幂等键；不再用输出目录名决定历史或编号。`select` 排队准备线索，`verify` 排队显式重新验收，`rebuild` 恢复已保存导出而不求解。旧原件和清单保持只读。

## 资源与检查

计算永远低于其他应用。工作进程使用 SCHED_IDLE、Nice 19 和 idle I/O，并在部署后由专用 cgroup 限制 CPU、内存、交换和 I/O。采样过期、外部繁忙、余量不足或外部 hold 都会暂停工作。共享 Ollama 不属于本应用 cgroup，客户端优先级不能即时抢占 GPU 推理。

公共测试仅用合成资料，默认禁止网络。构建和测试通过空闲资源门控，不能为验收绕过让位规则：

```sh
.venv/bin/python -m vocabatron.app.dev_runner -- .venv/bin/python -m pytest
.venv/bin/python -m vocabatron.app.dev_runner -- npm ci --prefix frontend --ignore-scripts --no-audit --no-fund
.venv/bin/python -m vocabatron.app.dev_runner -- .venv/bin/python frontend/scripts/icons.py
.venv/bin/python -m vocabatron.app.dev_runner -- npm run build --prefix frontend
.venv/bin/python -m vocabatron.app.dev_runner -- .venv/bin/python frontend/scripts/browser-check.py
.venv/bin/python -m vocabatron.app.dev_runner -- .venv/bin/uv build --out-dir .private/packages
.venv/bin/vocabatron privacy-scan --packages .private/packages
```

隐私扫描覆盖工作目录、真实 Git 索引 blob、后端包及前端发布文件；仅明确列出的原创图标路径允许 PNG。禁止生产 source map、私人端点配置、身份名单或来源内容进入公开包。模式扫描不声称能识别所有未知秘密。

经明确授权在其他 GPU 工作期间进行 CPU 检查时，可给门控添加 `--cpu-only --cpu-budget 4`。它只影响自己的进程树，并持续保留内存和 I/O 让位检查；这不授权共享模型推理。浏览器检查使用隔离的合成课程和临时回环 TLS 服务，完成后关闭。截图位于忽略的 `frontend/test-results/`；日志位于 `.cache/browser-checks/`。浏览器引擎须预先通过项目 Playwright CLI 下载；Ubuntu ARM64 的缺失 WebKit 共享库仅下载并解包到项目缓存，不安装系统包。WebKit 自动化不等于真机 iOS 验收。

在线备份通过 SQLite backup API 保存一致数据库及不可变对象引用闭包，并恢复到新的隔离目录进行检查。同盘备份不抵御整块磁盘损坏。提交与推送由用户本人执行。
