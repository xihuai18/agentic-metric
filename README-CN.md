# Agentic Metric X

[![PyPI](https://img.shields.io/pypi/v/agentic-metric-x)](https://pypi.org/project/agentic-metric-x/)
[![Python](https://img.shields.io/pypi/pyversions/agentic-metric-x)](https://pypi.org/project/agentic-metric-x/)
[![Downloads](https://static.pepy.tech/badge/agentic-metric-x)](https://pepy.tech/project/agentic-metric-x)
[![Downloads/month](https://img.shields.io/pypi/dm/agentic-metric-x)](https://pypi.org/project/agentic-metric-x/)

[English](README.md)

本地化的 AI coding agent 指标监控工具 — 类似 `top`,但监控的是你的 coding agent。追踪 **Claude Code** 和 **Codex** 的 token 用量和成本,提供 TUI 仪表盘和 CLI 命令。

**支持平台:Linux、macOS 和 Windows。**

**所有数据都在你的控制范围内,不会发送遥测。** 默认情况下工具仅读取本机的
agent 数据文件(`~/.claude/`、`~/.codex/`);如果配置了 SSH
远程,会通过 SSH 读取远程 agent 数据文件,并缓存到本机后参与汇总。

![Agentic Metric TUI](agentic-metric-screenshot.png)

## 功能

- **成本估算** — 基于各模型定价表计算 API 等效成本,支持 CLI 管理定价;支持长上下文和缓存时长定价
- **统一的用量报告** — 单个 `report` 命令覆盖今日 / 本周 / 本月 / 自定义区间,含 agent × provider × model 明细、项目排行和小时/天热图
- **TUI 仪表盘** — 终端图形界面,自动刷新,含汇总卡片、热图条、自适应 14 天 / 12 周 / 12 月成本趋势和 agent → provider → model 分解
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

> ¹ OpenAI 的 `input_tokens` 字段本身包含了已缓存部分,collector 存储时会扣掉
> `cached_input_tokens`,避免在 input 价和 cache-read 价上重复计费。GPT-5.6 及
> 之后的模型还会上报 `cache_write_input_tokens`(input 的子集):这些 token 会移入
> cache-write 桶,按官方 1.25× 写入价计费;更早的模型没有写入费,写入 token 保留在
> input 中按原价计。

## 安装

需要 Python 3.10+。

```bash
pip install agentic-metric-x
```

或使用 [uv](https://docs.astral.sh/uv/):

```bash
uvx agentic-metric-x              # 直接运行,无需安装
uv tool install agentic-metric-x   # 持久安装
uv tool upgrade agentic-metric-x   # 升级到最新版
```

## 使用

```bash
agentic-metric                       # 启动 TUI 仪表盘(无参数时默认启动)
agentic-metric tui                   # 显式启动 TUI 仪表盘
agentic-metric sync                  # 强制同步各 collector 到本地数据库
agentic-metric sync --rebuild        # 从原始 session 日志重建派生数据库
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

### Report 选项

| 选项 | 说明 |
|------|------|
| `--today` | 今日用量 |
| `--week` | 本周用量(周一至今) |
| `--month` | 本月用量 |
| `--range FROM:TO` | 自定义日期区间,如 `2026-04-01:2026-04-23` |
| `--full` | 显示额外明细表(按 host、agent、provider、model 与时间分解) |
| `--limit N` / `-n N` | 明细表与 provider 内模型分解的行数(1–25,默认 8) |
| `--json` | 输出机器可读 JSON(便于脚本 / 管道),不渲染表格 |
| `--watch N` / `-w N` | 每 N 秒刷新报告(Ctrl-C 退出) |
| `--no-sync` | 跳过查询前的 collector 同步 |

`report` 会输出:总成本 / sessions / 用户轮次 / tokens / 缓存命中率的汇总条,
与上一同类周期的差额、provider 成本汇总,一个热图条(`--today` 按小时、`--week` 和 `--month`
按日),默认展示 agent × provider × model 明细、项目排行,并可按需展开额外明细表。

### 定价管理

模型定价用于成本估算。常见模型已内置定价,你可以通过 CLI 添加新模型、覆盖现有价格、
配置长上下文费率和缓存时长费率。用户自定义定价存储在 `$DATA/agentic_metric/pricing.json`。

#### 基础模型定价

```bash
agentic-metric pricing list                                                # 查看所有模型定价
agentic-metric pricing set deepseek-r2 -i 0.5 -o 2.0                       # 添加新模型
agentic-metric pricing set claude-opus-4-7 -i 4.0 -o 20.0 -cr 0.4 -cw 5.0  # 覆盖内置定价
agentic-metric pricing reset deepseek-r2                                   # 恢复单个模型为内置默认
agentic-metric pricing reset --all                                         # 恢复所有定价为默认
```

#### 长上下文定价

某些模型在单次请求超过 token 阈值时会使用更高的费率。工具在 collector 提供事件级用量时按事件应用这些费率。

```bash
agentic-metric pricing long-context set gpt-5.5 --threshold 272000 -i 10 -o 45 -cr 1 -cw 0
agentic-metric pricing long-context reset gpt-5.5        # 删除用户覆盖
agentic-metric pricing long-context disable gpt-5.5      # 禁用内置规则
agentic-metric pricing long-context enable gpt-5.5       # 重新启用内置规则
```

#### 缓存时长定价

Anthropic 对不同 TTL 的 cache write 收取不同费率。工具默认使用 5 分钟费率;如需 1 小时缓存时长,可手动覆盖。

```bash
agentic-metric pricing cache set claude-sonnet-4 --write-1h 6    # 设置 1 小时 cache write 价格
agentic-metric pricing cache reset claude-sonnet-4                # 删除覆盖
```

未知模型不会自动套用默认价或模型族价格。它们的用量和 tokens 仍会纳入报告,但在你用 `agentic-metric pricing set` 添加明确价格前不会计入费用总额。CLI 和 TUI 会集中显示 `Pricing missing` 提示及模型名称,不会在费用数字里混入 `?`。

费用一律按官方 API 牌价折算;session 日志里网关回报的单请求费用(可能含供应商折扣)会被忽略。当历史记录带有明确标记时,非标准速度/优先级模式会按官方溢价表计费:Codex 会话记录 `thread_settings_applied.service_tier`(`priority`,或按 OpenAI priority 费率计费的 `fast`),Claude 会话记录 API 实际服务速度 `usage.speed`(受支持的 Opus 模型按 fast mode 溢价)。没有标记的历史按标准价计。

定价变更后,命令会自动重新同步历史数据,从原始 JSONL 数据重新计算事件级成本(如长上下文请求)。

如果切换 roots/provider 后发现缓存历史不对,可以运行
`agentic-metric sync --rebuild`。它只会删除本工具自己的派生 SQLite 数据库,
再从 Claude Code 和 Codex 的原始 session 文件重建;`config.json`、`pricing.json`
以及 agent 数据目录不会被修改。

### TUI 快捷键

底栏显示为:`PgUp/PgDn Range · ←→ View · . Now · r Auto-refresh · p Pricing · ? Help · q Quit`。

| 键 | 底栏 | 功能 |
|----|------|------|
| `←` / `→` | View | 切换视图(Today / Week / Month) |
| `PageUp` / `PageDown` | Range | 时间范围往前 / 往后 |
| `.` | Now | 回到"现在"(清空 offset) |
| `↑` / `↓` | — | 滚动明细面板 |
| `r` | Auto-refresh | 切换快速自动刷新;开启时暂停慢速周期 sync |
| `p` | Pricing | 打开只读价格视图(标出缺少价格的模型) |
| `?` | Help | 显示快捷键速查表 |
| `q` | Quit | 退出 |

默认每 5 分钟自动同步,没有手动一次性同步键。
如需把数据从面板里导出,请使用 CLI(`agentic-metric report` / `today` / `week` / `month`)。
热图面板会显示当前选中范围的 provider 成本汇总;趋势面板会显示更长趋势窗口内的 provider 总额。

刷新间隔可在配置文件(`$DATA/agentic_metric/config.json`)覆盖:

```json
{ "intervals": { "data_sync": 300, "auto_refresh": 30 } }
```

## 内置模型定价

价格为 USD / 1M tokens。数据来源为官方定价页面(2026-04-25 核实；Claude Fable 5 于 2026-06-12 核实；Claude Opus 4.8 于 2026-06-02 核实；Claude Opus 5 于 2026-07-28 核实)。

<details>
<summary>Anthropic Claude</summary>

| 模型 | Input | Output | Cache Read | Cache Write |
|------|------:|-------:|-----------:|------------:|
<!-- pricing:anthropic:start -->
| claude-fable-5 | $10.00 | $50.00 | $1.00 | $12.50 |
| claude-sonnet-5 / claude-sonnet-4-6 / claude-sonnet-4-5 / claude-sonnet-4 / claude-sonnet-3-7 / claude-3-7-sonnet / claude-3-5-sonnet | $3.00 | $15.00 | $0.30 | $3.75 |
| claude-opus-5 / claude-opus-4-8 / claude-opus-4-7 / claude-opus-4-6 / claude-opus-4-5 | $5.00 | $25.00 | $0.50 | $6.25 |
| claude-opus-4-1 / claude-opus-4 / claude-3-opus | $15.00 | $75.00 | $1.50 | $18.75 |
| claude-haiku-4-5 | $1.00 | $5.00 | $0.10 | $1.25 |
| claude-haiku-3-5 / claude-3-5-haiku | $0.80 | $4.00 | $0.08 | $1.00 |
| claude-3-haiku | $0.25 | $1.25 | $0.03 | $0.30 |
<!-- pricing:anthropic:end -->

</details>

<details>
<summary>OpenAI GPT</summary>

| 模型 | Input | Output | Cache Read | Cache Write |
|------|------:|-------:|-----------:|------------:|
<!-- pricing:openai:start -->
| gpt-5.6-sol | $5.00 | $30.00 | $0.50 | $6.25 |
| gpt-5.6-terra | $2.50 | $15.00 | $0.25 | $3.125 |
| gpt-5.6-luna | $1.00 | $6.00 | $0.10 | $1.25 |
| gpt-5.5 | $5.00 | $30.00 | $0.50 | — |
| gpt-5.4-mini | $0.75 | $4.50 | $0.075 | — |
| gpt-5.4-nano | $0.20 | $1.25 | $0.02 | — |
| gpt-5.4 | $2.50 | $15.00 | $0.25 | — |
| gpt-5.2-codex / gpt-5.2-chat-latest / gpt-5.2 / gpt-5.3-codex / gpt-5.3-chat-latest / gpt-5.3 | $1.75 | $14.00 | $0.175 | — |
| gpt-5.1-codex-max / gpt-5.1-codex / gpt-5.1-chat-latest / gpt-5.1 / gpt-5-codex / gpt-5-chat-latest / gpt-5 | $1.25 | $10.00 | $0.125 | — |
<!-- pricing:openai:end -->

</details>

<details>
<summary>Google Gemini</summary>

| 模型 | Input | Output | Cache Read | Cache Write |
|------|------:|-------:|-----------:|------------:|
<!-- pricing:gemini:start -->
| gemini-3.6-flash | $1.50 | $7.50 | $0.15 | — |
| gemini-3.5-flash | $1.50 | $9.00 | $0.15 | — |
| gemini-3.1-pro / gemini-3-pro | $2.00 | $12.00 | $0.20 | — |
| gemini-3.1-flash-lite | $0.25 | $1.50 | $0.025 | — |
| gemini-3-flash | $0.50 | $3.00 | $0.05 | — |
| gemini-2.5-flash | $0.30 | $2.50 | $0.03 | — |
| gemini-2.5-flash-lite | $0.10 | $0.40 | $0.01 | — |
| gemini-2.0-flash | $0.10 | $0.40 | $0.025 | — |
| gemini-2.0-flash-lite | $0.075 | $0.30 | $0.00 | — |
<!-- pricing:gemini:end -->

</details>

运行 `agentic-metric pricing list` 查看完整定价表(包含你的覆盖配置)。

## 架构

```
src/agentic_metric/
├── cli.py              # Typer CLI 命令和 Rich 报告渲染
├── config.py           # 平台路径、collector roots、SSH 远程配置
├── pricing.py          # 内置 + 用户定价,成本估算引擎
├── formatting.py       # 纯格式化辅助(成本 / token、source / host 标签)
├── collectors/
│   ├── __init__.py     # Collector 注册中心和基类
│   ├── claude_code.py  # Claude Code JSONL 历史解析器
│   ├── codex.py        # Codex JSONL 历史解析器
│   └── remote.py       # SSH 封装:把远程 root 镜像到本地缓存后解析
├── store/
│   ├── __init__.py
│   ├── database.py     # SQLite 数据库(sessions, session_usage 分桶表)
│   └── aggregator.py   # 查询层:区间汇总、热图、多维分解
└── tui/
    ├── __init__.py
    ├── app.py            # Textual TUI 应用
    ├── widgets.py        # 自定义 TUI 组件(汇总卡片、热图、趋势)
    ├── help_screen.py    # 快捷键速查表弹窗
    ├── pricing_screen.py # 只读定价视图(标出缺少价格的模型)
    └── styles.tcss       # Textual CSS
```

### 数据流

1. **Collectors** 读取 agent 数据文件(`~/.claude/`、`~/.codex/`),将会话历史同步到数据库。
2. **Database** 将 sessions 和按日拆分的 `session_usage` 桶存入 SQLite。
3. **Aggregator** 执行 SQL 查询生成报告(区间汇总、热图、agent/model/project 分解)。
4. **CLI** 使用 Rich 渲染表格和面板。**TUI** 使用 Textual 提供仪表盘。
5. **Pricing** 引擎按事件计算成本(支持长上下文)。

## 数据来源

数据路径因平台而异,下表中 `$DATA` 含义如下:

| | Linux | macOS | Windows |
|--|-------|-------|---------|
| `$DATA` | `~/.local/share` | `~/Library/Application Support` | `%LOCALAPPDATA%` |

| Agent | 数据路径 | 采集内容 |
|-------|---------|---------|
| Claude Code | `~/.claude/projects/` | JSONL 会话、token 用量、模型、分支 |
| Claude Code | `~/.claude/stats-cache.json` | 每日活动统计 |
| Codex | `~/.codex/sessions/` | JSONL 会话、token 用量、模型 |

默认情况下,Claude Code 支持 `CLAUDE_CONFIG_DIR`,Codex 支持 `CODEX_HOME`,
collector 会读取这些环境变量。需要同时扫描多个目录、手动指定 provider,
或加入 SSH 远程机器时,可以创建本工具自己的配置文件:

```json
{
  "collectors": {
    "codex": {
      "roots": [
        {"path": "~/.codex", "provider": "openai"},
        {"path": "~/.codex-custom", "provider": "custom"}
      ]
    },
    "claude_code": {
      "roots": [
        {"path": "~/.claude"},
        {"path": "~/.claude-alt"},
        {"path": "~/.claude-provider-b", "provider": "provider-b"}
      ]
    }
  },
  "remotes": [
    {
      "name": "remote-dev",
      "host": "remote-dev",
      "collectors": {
        "codex": {
          "roots": [{"path": "~/.codex", "provider": "openai"}]
        },
        "claude_code": {
          "roots": [{"path": "~/.claude"}]
        }
      }
    }
  ]
}
```

默认配置路径是 `$DATA/agentic_metric/config.json`,也可以用
`AGENTIC_METRIC_CONFIG` 指向其他 JSON 文件。Claude Code 目录没有 provider
时不会被强行推断;Codex 未配置 provider 时会尝试读取 JSONL 里的
`model_provider`。

远程配置会使用已有的 `ssh` 配置。`host` 可以是 SSH alias 或主机名;可选字段有
`name`、`user`、`port`、`timeout`、`ssh_options`。如果某个远程没有配置
collector 目录,它会复用本机的 collector roots;本机也没有配置时,等价于远程的
`~/.claude` 和 `~/.codex`。远程同步会在远程主机上展开 `~`,通过 SSH 读取
`projects/` 和 `sessions/`,缓存到 `$DATA/agentic_metric/remote-cache/`,再和本机用量一起汇总。
缓存里也会保存远程文件 manifest,所以重复 sync 只会下载变化过的 session/index
文件,不会每次重新传完整 agent 目录。下载按体积分批进行,每批下载完成后 manifest
就会推进,因此 `timeout` 是按批生效的;大目录第一次同步被中断后,下次只补剩下的
部分,不会从头重来。已经从远程 manifest 消失的缓存文件会被移到
`.stale/`,不再参与解析;但之前已入库的用量仍保留在本地数据库里,历史报表照常包含。
如果远程目录不存在,这个 collector 会被跳过,不会拿旧缓存继续入库。CLI/TUI
总量保持合并,明细和 Top projects 会保留 host/source 维度。

报告顶部的总量会合并本机和远程数据。明细表使用紧凑的 `Source` 标签:本机行显示
root,例如 `~/.codex`;远程行显示 `host:root`,例如 `remote-dev:~/.codex`。
Top projects 也会给远程路径加同样的前缀,例如
`remote-dev:/workspace/project`,避免本机和远程相同项目路径被静默合并。
而同名项目若来自多个**本机** root,会合并为一行(否则显示完全相同),不会重复列出。
`--full` 会额外展示按 source 汇总的 provider 表;默认报告保留
source × agent × provider × model 明细和 Top projects。

所有数据汇总存储在 `$DATA/agentic_metric/data.db`(SQLite)。

## 不支持的 Agent

- **Cursor** — Cursor 自 2026 年 1 月左右(约 2.0.63+ 版本)起不再向本地 `state.vscdb` 数据库写入 token 用量数据(`tokenCount`),所有 `inputTokens`/`outputTokens` 值均为 0。Cursor 已将用量追踪迁移至服务端。由于本工具的设计原则是完全离线、不联网,无法通过网络 API 获取 Cursor 的用量数据,因此无法支持监测 Cursor 的用量。
- **OpenCode / Qwen Code / VS Code Copilot Chat** — 这三个 collector 在
  v0.1.8 之前存在,v0.2.0 起因本 fork 聚焦 Claude Code + Codex 而移除。
  如果你仍需要这些 agent 的统计,请使用上游的 v0.1.8。

## 隐私

- 默认本地运行;不配置 SSH 远程时不发网络请求
- 不修改 agent 的配置或数据文件(只读)
- 所有统计数据存储在本地 SQLite 数据库
- 可随时删除数据目录清除所有数据(Linux: `~/.local/share/agentic_metric/`,macOS: `~/Library/Application Support/agentic_metric/`,Windows: `%LOCALAPPDATA%\agentic_metric\`)

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
