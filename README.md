# Vocabatron

Vocabatron 在已授权的本地工作目录中，从讲义文本层保留完整词条和来源，使用本机 `gemma4:31b` 选择原有候选，冻结后用 OR-Tools CP-SAT 生成两个完整、连通且结构不同的填字布局。结果通过 ReportLab 内容层和 pypdf 叠加到原模板前两页，并独立检查最终 PDF 的字符、坐标和实际渲染。

本阶段提供 Python 核心与 CLI。Web/PWA、网络入口、后台生产服务和系统配置属于后续阶段，当前没有安装或开放。

## 本地环境与私人数据

先核对执行环境、原目录、main 分支和已有工作；遵守 [AGENTS.md](AGENTS.md)。项目依赖使用 Python 3.12 与 `uv.lock`。在已确认环境中建立 `.venv`，从可信包源安装 uv 后执行：

```sh
UV_CACHE_DIR="$PWD/.cache/uv" UV_PYTHON_DOWNLOADS=never .venv/bin/uv sync --locked
```

`.private/`、项目虚拟环境、缓存和生成产物由 `.gitignore` 隔离。原始 PDF 只读。原件工作副本置于 `.private/sources/`，配置使用 `.private/config.json`；字段参见中性的 [config.example.json](config.example.json)。实际输出前缀仅来自私人配置。忽略规则不能保护已跟踪文件，运行私人处理前仍需检查 Git 跟踪和暂存范围。

## CLI

在项目根目录执行；错误默认仅报告状态与错误类型，不打印原文、词表或模型响应。

```sh
.venv/bin/vocabatron ingest
.venv/bin/vocabatron check-transcript
.venv/bin/vocabatron select
.venv/bin/vocabatron generate --task-id example-run
.venv/bin/vocabatron status example-run
.venv/bin/vocabatron verify example-run
.venv/bin/vocabatron rebuild example-run --new-task-id example-rebuild
.venv/bin/vocabatron private-test example-run --allow-private-integration
.venv/bin/vocabatron metrics
.venv/bin/vocabatron benchmark-model --allow-private-integration --sample-words 1
.venv/bin/vocabatron privacy-scan
```

已有有效冻结线索时，`select` 直接复用；需要明确的新选择版本时使用 `select --new-version`。`generate` 可用 `--seconds` 延长每份求解预算、用 `--workers` 限制线程数。中断后使用相同任务 ID 继续；完整验证的两份布局会被保留，输入版本发生变化则拒绝复用。`cancel TASK_ID` 请求取消，取消按执行尝试隔离，覆盖解析、模型请求、求解、导出、验证和发布前边界。

结果只有在双份结构验证、PDF 检查和渲染检查全部通过后，才通过目录原子重命名出现在 `.private/results/TASK_ID/`。完成清单列出两份 PDF、哈希、依赖版本和快照。`rebuild` 只读取保存的模板、布局、课表和冻结线索，不调用模型或求解器。渲染预览与检查报告保存在同一私人结果目录；自动检查不等于人工验收。

`benchmark-model` 需显式启用私人集成，使用同一个小样本做两次本机协议调用，记录初始/已加载状态和实际耗时。它在独立的私人采样目录中保存响应，不改正式冻结线索；单词条样本不能冒充整课热调用性能。正常生成和重建不会自动执行该命令。

退出码：成功为 0；输入/规则错误为 2；输入不可用或 schema 无效为 3；已有任务为 5；求解未知为 6；精确约束无解为 7；模型编码错误为 8；取消为 130。超时、取消和未知不会被报告为无解。

## 公共检查

公共测试只使用新建的合成词表与程序绘制的合成模板，默认禁止网络访问，不依赖私人原件或本机模型。

```sh
.venv/bin/python -m pytest --basetemp=.cache/pytest
UV_CACHE_DIR="$PWD/.cache/uv" .venv/bin/uv build --out-dir .private/packages
.venv/bin/vocabatron privacy-scan --packages .private/packages
```

隐私扫描分别覆盖工作目录、通过 `ls-files --stage -z` 与 `cat-file` 读取的真实索引 blob，以及有解压容量上限的 wheel/sdist 内容；检测常见凭据模式、私人配置前缀、当前主机名和已知原文长片段。该检查有明确范围，不能保证识别所有未知敏感字符串。

CLI 配置、资源限制和恢复语义见 [docs/cli.md](docs/cli.md)，逐项回归见 [docs/hardening.md](docs/hardening.md)。现有作者、commit message 和历史由用户授权保留；本项目的提交与推送由用户本人执行。

设计、模型编码与后续接口见 [docs/architecture.md](docs/architecture.md)。实际私人验收数量、耗时、输出名称和机器信息只写入 `.private/`。
