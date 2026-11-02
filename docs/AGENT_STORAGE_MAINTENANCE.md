# 给本机 Agent 的存储维护提示词

当前只支持 macOS 原生透明无损压缩，不删除历史；压缩并不限制总容量，数据继续增长时仍需监测磁盘余量。不要把删除或截断日志当作默认释放方案。复制下面的提示词给能够访问本机仓库的 Agent：

```text
请维护本仓库模型快照占用。先阅读 README、AGENTS.md 和
model_monitor/storage_maintenance.py，确认系统为 macOS、文件系统支持
透明压缩。确认本轮使用的代码版本并记录 SHA256，发现代码被并行修改就停下。
所有检查和执行都在本机进行，不上传原始记录、正文、图片、token或授权链接。

先检查是否已有 storage_maintenance 进程；有则不要启动第二个。
运行 python3 -m model_monitor.storage_maintenance 预演，读取默认报告：
~/.local/share/codex-request-inspector/model-monitor-capture/metadata/storage-maintenance.json
如果预检失败，查清元数据异常，不清零、不伪造完成。
注意 --dry-run 不是参数：省略 --apply 即为预演。

我授权你在预演安全、没有并行修改时运行：
python3 -m model_monitor.storage_maintenance --apply
仅使用现有实现。保留原路径、内容 SHA256、权限及普通 JSON 读取；
只处理稳定至少120秒、至少128KiB，且有唯一终态或完整rollout关闭证明的快照。
活跃、歧义、共享活跃引用、未配对和未关闭producer保护优先。
保留原始历史、全部诊断计数与数据库，不改前端、取消筛选和模型核对规则。
不发模型探针，不重放请求，不改系统代理/DNS/证书/Codex配置/LaunchAgent。
不得通过递归删除缓存、日志或data目录来释放空间。

完成后读取报告及 storage-maintenance-history/<run_id>.json，核对两者一致，
汇报 verified_files 数量、reclaimed_bytes、失败和跳过原因，失败必须可见。
不要上传这些本机报告，其中路径/标识仍属私人数据。
如要每6小时执行，请先提供具体调度方案并征得我的确认；获得确认后再安装。
自动任务正常完成保持安静，仅失败、异常增长、低磁盘空间或需人工操作时通知。
若空间仍不足，给出占用分析与可恢复归档方案，删除必须另获明确授权。
```

默认源目录为 `~/.local/share/codex-request-inspector/native-traces`。下载者应先确认这是自己的真实数据源；其他来源不能直接套用此脚本的关闭证明。Linux/Windows 不执行 `--apply`，也不自行替换成有损清理。
