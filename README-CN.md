# Agentic Metric

[English](README.md)

本地化的 AI coding agent 指标监控工具 — 类似 `top`,但监控的是你的 coding agent。追踪 **Claude Code** 和 **Codex** 的 token 用量和成本,提供 TUI 仪表盘和 CLI 命令。

**支持平台:Linux 和 macOS。**

**所有数据完全存储在本地,使用过程不会联网。** 工具仅读取本机的 agent 数据文件(`~/.claude/`、`~/.codex/`)和进程信息,不发送任何数据到外部服务器。

![Agentic Metric TUI](agentic-metric-screenshot.png)

## 功能

- **实时监控** — 检测运行中的 agent 进程,增量解析 JSONL 会话数据
- **成本估算** — 基于各模型定价表计算 API 等效成本,支持 CLI 管理定价
- **统一的用量报告** — 单个 `report` 命令覆盖今日 / 本周 / 本月 / 自定义区间,含 agent × model 明细、项目排行、会话排行、小时/天/周热图
- **TUI 仪表盘** — 终端图形界面,实时刷新,含汇总卡片、热图条、30 天成本柱图、agent × model 分解
- **多 Agent 支持** — 插件架构;目前支持 Claude Code 和 Codex,可扩展

## 各 Agent 指标覆盖情况

| 字段 | Claude Code | Codex |
|------|:-----------:|:-----:|
| 会话 ID | ✓ | ✓ |
| 项目路径 | ✓ | ✓ |
| Git 分支 | ✓ | ✓ |
| 模型名称 | ✓ | ✓ |
| Input tokens | ✓ | ✓ |
| Output tokens | ✓ | ✓ |
| Cache tokens | ✓ | ✓¹ |
| 用户轮次 | ✓ | ✓ |
| 消息总数 | ✓ | ✓ |
| 首条/末条 prompt | ✓ | ✓ |
| 成本估算 | ✓ | ✓ |
| 实时活跃状态 | ✓ | ✓ |

> ¹ Codex 仅暴露 cache-read tokens,cache-write 不上报。OpenAI 的 `input_tokens`
> 字段本身包含了已缓存部分,collector 存储时会扣掉 `cached_input_tokens`,
> 避免在 input 价和 cache-read 价上重复计费。

## 安装

需要 Python 3.10+。

```bash
pip install agentic-metric
```

或使用 [uv](https://docs.astral.sh/uv/):

```bash
uvx agentic-metric              # 直接运行,无需安装
uv tool install agentic-metric   # 持久安装
uv tool upgrade agentic-metric   # 升级到最新版
```

## 使用

```bash
agentic-metric                       # 启动 TUI 仪表盘(无参数时默认启动)
agentic-metric tui                   # 显式启动 TUI 仪表盘
agentic-metric sync                  # 强制同步各 collector 到本地数据库
agentic-metric report --today        # 今日用量报告
agentic-metric report --week         # 本周(周一至今)
agentic-metric report --month        # 本月
agentic-metric report --range 2026-04-01:2026-04-23   # 自定义日期区间
agentic-metric today                 # `report --today` 的快捷方式
agentic-metric week                  # `report --week` 的快捷方式
agentic-metric month                 # `report --month` 的快捷方式
agentic-metric history -d 30         # 最近 N 天(默认 14 天)
agentic-metric pricing               # 管理模型定价
```

`report` 会输出:总成本 / sessions / 用户轮次 / tokens / 缓存命中率的汇总条,
与上一同类周期的差额,一个热图条(`--today` 按小时、`--week` 按日、`--month`
按周),30 天成本柱图,以及 agent × model、项目、会话、时间桶的明细表。

### 定价管理

模型定价用于成本估算。常见模型已内置定价,你可以通过 CLI 添加新模型或覆盖现有价格 — 用户自定义定价存储在 `$DATA/agentic_metric/pricing.json`。

```bash
agentic-metric pricing list                                                # 查看所有模型定价
agentic-metric pricing set deepseek-r2 -i 0.5 -o 2.0                       # 添加新模型
agentic-metric pricing set claude-opus-4-7 -i 4.0 -o 20.0 -cr 0.4 -cw 5.0  # 覆盖内置定价
agentic-metric pricing reset deepseek-r2                                   # 恢复单个模型为内置默认
agentic-metric pricing reset --all                                         # 恢复所有定价为默认
```

对于未知模型,会按模型族自动匹配定价(如 `claude-sonnet-*` 使用 Sonnet 定价,`gpt-5*` 使用 GPT-5 定价),最后才使用全局默认值。

### TUI 快捷键

| 键 | 功能 |
|----|------|
| `←` / `→` | 切换视图(Today / Week / Month) |
| `↑` / `↓` | 时间范围往前 / 往后 |
| `.` | 回到"现在"(清空 offset) |
| `t` / `w` / `m` | 直接聚焦 Today / Week / Month |
| `r` | 刷新数据 |
| `q` | 退出 |

## 数据来源

数据路径因平台而异,下表中 `$DATA` 含义如下:

| | Linux | macOS |
|--|-------|-------|
| `$DATA` | `~/.local/share` | `~/Library/Application Support` |

| Agent | 数据路径 | 采集内容 |
|-------|---------|---------|
| Claude Code | `~/.claude/projects/` | JSONL 会话、token 用量、模型、分支 |
| Claude Code | `~/.claude/stats-cache.json` | 每日活动统计 |
| Claude Code | 进程检测 | 运行状态、工作目录 |
| Codex | `~/.codex/sessions/` | JSONL 会话、token 用量、模型 |
| Codex | 进程检测 | 运行状态、工作目录 |

Claude Code 支持 `CLAUDE_CONFIG_DIR`,Codex 支持 `CODEX_HOME`,如果你改了
这两个 agent 的配置目录,collector 会自动读取环境变量。

所有数据汇总存储在 `$DATA/agentic_metric/data.db`(SQLite)。

## 不支持的 Agent

- **Cursor** — Cursor 自 2026 年 1 月左右(约 2.0.63+ 版本)起不再向本地 `state.vscdb` 数据库写入 token 用量数据(`tokenCount`),所有 `inputTokens`/`outputTokens` 值均为 0。Cursor 已将用量追踪迁移至服务端。由于本工具的设计原则是完全离线、不联网,无法通过网络 API 获取 Cursor 的用量数据,因此无法支持监测 Cursor 的用量。
- **OpenCode / Qwen Code / VS Code Copilot Chat** — 这三个 collector 在
  v0.1.8 之前存在,v0.2.0 起因本 fork 聚焦 Claude Code + Codex 而移除。
  如果你仍需要这些 agent 的统计,请使用上游的 v0.1.8。

## 隐私

- 不联网,不发送任何数据
- 不修改 agent 的配置或数据文件(只读)
- 所有统计数据存储在本地 SQLite 数据库
- 可随时删除数据目录清除所有数据(Linux: `~/.local/share/agentic_metric/`,macOS: `~/Library/Application Support/agentic_metric/`)

## 开发

```bash
git clone https://github.com/xihuai18/agentic-metric
cd agentic-metric
pip install -e ".[dev]"
pytest
```

## 许可证

MIT — 详见 [LICENSE](LICENSE)。

Fork 自 [MrQianjinsi/agentic-metric](https://github.com/MrQianjinsi/agentic-metric)(基于上游 v0.1.8)。本 fork 相对上游的变更见 [CHANGELOG.md](CHANGELOG.md)。
