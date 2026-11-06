# 模型接口抽样审计

本模块在原有逐请求声明核对之外，复用 AI-Infra-Guard 的统计算法检查指定接口的抽样表现。回包模型名相同、抽样未发现异常，都不能证明模型真身或认证某一条对话请求。

## 准备独立引擎

主面板仍使用 Python 标准库。可选审计引擎使用独立 Python 3.12 环境，源码固定到 `3b12a868d865e007bb098221d0c9129def10929a`。在仓库根目录执行：

```sh
audit_cache="$HOME/Library/Caches/model-observatory-aig"
mkdir -p "$audit_cache"
git clone --filter=blob:none --no-checkout https://github.com/Tencent/AI-Infra-Guard.git "$audit_cache/upstream"
git -C "$audit_cache/upstream" sparse-checkout set services/api_checker
git -C "$audit_cache/upstream" fetch --depth=1 origin 3b12a868d865e007bb098221d0c9129def10929a
git -C "$audit_cache/upstream" checkout --detach 3b12a868d865e007bb098221d0c9129def10929a
python3.12 -m venv "$audit_cache/venv"
"$audit_cache/venv/bin/python" -m pip install -r model_monitor/relay_audit/requirements.lock
python3 -m model_monitor.relay_audit diagnose
```

以上安装命令面向尚未配置的独立缓存；已有目录时先诊断，不覆盖或清理既有环境。运行时核对 `engine-hashes.json` 中的上游算法哈希，引擎不可用会明确失败。第三方源码、解释器和依赖不提交到本仓库。

## 隔离启动

```sh
python3 -m model_monitor.relay_audit.launch
```

启动两个动态回环端口，两个界面共用作业后端。对话列表来自合成数据，不读取真实 Codex 数据库、聊天、traces 或现役设置。Ctrl-C 只停止本次启动的候选服务；不会安装 App、切换现役端口或启动采集器。历史保留在独立缓存中，未完成作业在重启后明确标为失败并计数。

默认仅允许本机合成目标，远程执行关闭。已有独立凭据和费用授权时，操作者可通过 `serve` 的 stdin JSON 设置 `allow_remote: true`，使用 HTTP/CLI 提交显式 HTTPS `/v1` 目标。开启后网页仍保留合成说明，应从 HTTP/CLI 操作真实目标。发布此功能不代表真实调用或费用授权。

## Agent 与作业合同

Agent 从已启动实例发现 `/api/audit/v1/capabilities`，检查 `coverage`，再显式提交 `jobs`、查询进度或取消。完整流程见项目 [model-audit Skill](../../.agents/skills/model-audit/SKILL.md) 与 [schema.json](schema.json)。CLI 使用相同后端：

```sh
python3 -m model_monitor.relay_audit --help
python3 -m model_monitor.relay_audit capabilities --url http://127.0.0.1:<candidate-port>
```

本地鉴权和目标密钥只通过内存或 stdin JSON 传递，不能放进 URL、进程参数、报告或浏览器持久存储。轮询只读取状态，不启动探针。取消阻止后续请求并终止在途工作进程。

Responses、Chat Completions、Anthropic Messages 必须显式选择，不按模型名猜协议。最多 200 个数字探针加一个上下文检查，请求墙钟超时为 0.05–5 秒，作业期限为 120 秒。请求数、有效样本、失败和跳过分开记录；用量预算不是供应商计费保证。

导入或标定的基线须记录来源、模型原名、协议、日期、完整采样条件、样本数和哈希。至少需要两个不同模型的兼容基线；目标未覆盖、条件不同或不足 40 个有效样本时报告证据不足。来源声明和哈希不能证明基线可信，需独立建立来源。

## 验证与限制

```sh
python3 -m unittest discover -v
python3 -m model_monitor.relay_audit.acceptance --break-fixture
python3 -m model_monitor.relay_audit.acceptance
```

故意损坏合成回包的 `--break-fixture` 应退出 1；恢复后应退出 0。测试只向动态回环端口发送合成请求。浏览器复验另需 Playwright，可运行 `model_monitor.relay_audit.browser_acceptance`。示例结果均为合成统计，不是生产基线。

当前消费完整 JSON，不支持完整 SSE 审计。真实 HTTPS、官方当前型号基线、实站身份、真实计费和已安装 App 不在本轮合成验证范围。审计报告绑定目标、模型、协议、采样条件、基线哈希与时间，不按时间邻近挂到对话上；原逐请求核对仍按稳定 ID 关联。
