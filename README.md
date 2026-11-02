# 模型核对 · Model Observatory

macOS 本地 Codex 请求模型核对工具。AppKit 原生窗口承载 WebKit 界面，红黄绿按钮可实际操作窗口；Python 标准库后端只输出必要元数据。

## 能做什么

- 按对话最近一轮查看“选择 → 请求 → 上游”，追查每次推理与重试。
- 用同线程/回合的选择快照和稳定 ID 关联独立响应字段，保持模型原名精确比较。
- 分开显示待验证、不一致、超时和读取异常；取消无响应可默认忽略并恢复查看。
- 读取分段历史会话的选择来源，保留项目归属候选，排除明确子智能体与内部标题任务。
- 可选透明无损压缩已完成快照；保留原路径和普通JSON读取。

**不能承诺全部验证。** 未保存的上游字段、未完成请求、缺选择或关联冲突仍待验证；超过24小时按既有规则只供历史追查。上游model声明不证明内部权重。这是独立项目，不是Apple或OpenAI官方工具。

## 环境与启动

macOS 12+、Python 3.9–3.12、Xcode Command Line Tools。Python 3.13移除了本项目当前使用的imghdr，暂不支持。无需npm、Electron或API key。

先在克隆目录根运行合成测试，不会发模型请求：

```sh
python3 -m unittest discover -v
```

构建本机原生App（会写入你自己的 `~/Applications/模型核对.app`，只允许覆盖相同bundle ID）：

```sh
zsh model_monitor/native/build.command
open "$HOME/Applications/模型核对.app"
```

构建时记录当前项目路径；运行仍依赖源目录，不是独立后端发行包。移动后可在App“文件 → 选择项目文件夹”重新选择。仅运行网页兼容面板可用 `python3 -m model_monitor.open_panel`，会启动或复用本机18083服务并打开浏览器；只启动服务可用 `python3 -m model_monitor.app --no-open`。原生App复用18085。

## 数据与可选采集

需要本机已有Codex SQLite会话数据及原生trace，默认读取用户home下的对应目录；SQLite只读。Codex版本和原生trace schema变化可能造成不兼容。没有现成trace时不能仅凭安装本工具就取得历史上游证据。

原生记录往往只存请求模型，独立上游观测是可选步骤。`python3 -m model_monitor.capture_control enable` **会修改你的Codex路由配置并安装用户级LaunchAgent**；请先审阅 `capture_control.py`，仅在确认本机环境适配后手动执行。回退为 `python3 -m model_monitor.capture_control rollback`。不改系统代理、DNS或证书；采集器沿用已配置的代理。建连预算30秒、上游空闲预算300秒；发送后的请求不会自动重放。

设置、壁纸和授权文件在各自data目录，采集元数据在本机用户目录。文件保留必要稳定ID/路径，仍应视作私有数据，不能连同故障报告上传。仓库不包含真实对话、trace、凭据或私人运行态。

无损压缩默认仅预演：`python3 -m model_monitor.storage_maintenance`。`--apply`会实际压缩满足安全条件的已完成快照；不会自动安装定时任务，调度由使用者自行决定。

## 授权与贡献

贡献者见 [CONTRIBUTORS.md](CONTRIBUTORS.md)，包含 Codex 的开发协助说明。

下载后可把 [存储维护提示词](docs/AGENT_STORAGE_MAINTENANCE.md) 交给本机 Agent；现有压缩不会删除历史，也不保证容量永不增长。其他客户端/模型来源的扩展边界与 Agent 提示词见 [来源适配指南](docs/SOURCE_ADAPTER_GUIDE.md)：可以开发适配器，目前并非通用导入工具。

本项目原创源代码采用Apache-2.0，见[LICENSE](LICENSE)。第三方图标保留原有ISC及Feather派生MIT声明，见[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。报告问题前请阅读[SECURITY.md](SECURITY.md)，只提交脱敏元数据。
