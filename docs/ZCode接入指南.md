# ZCode 接入指南

把 `reqslim` 挂在 ZCode 前面，减少模型请求次数，同时记录每一笔流量。

适用于任何装了 ZCode 的设备。文中不写死盘符和用户名，所有路径都用占位符或环境变量表示。

- **不改** ZCode 安装目录
- **不改** ZCode 的用户配置
- **不碰** 你的登录凭据
- 随时可以摘掉，摘掉后行为与原来完全一致

---

## 0. 路径约定

先认清三个位置，后面所有命令都用这套写法：

| 占位符 | 含义 | 怎么确定 |
|---|---|---|
| `<模块目录>` | 含 `reqslim\` 和 `tools\` 的那个文件夹 | 你解压/拷贝到哪里就是哪里，启动器就生成在里面 |
| `<ZCode 安装目录>` | ZCode 装在哪 | **一般不用管**，安装器会自动探测；探测失败再用 `--zcode-dir` 指定 |
| `%USERPROFILE%` | 当前用户主目录 | 命令行里直接写 `%USERPROFILE%` 即可；bash 下是 `$HOME` |

安装器会按顺序探测这些位置，命中即用：

**Windows**

```
%ZCODE_HOME%
D:\Zcode
C:\Zcode
%ProgramFiles%\ZCode
%ProgramFiles(x86)%\ZCode
%LOCALAPPDATA%\Programs\ZCode
```

**macOS / Linux**（尽力适配，见第 8 节）

```
$ZCODE_HOME
/Applications/ZCode.app/Contents/Resources
~/Applications/ZCode.app/Contents/Resources
/opt/ZCode/resources
/opt/zcode/resources
/usr/lib/zcode/resources
```

---

## 1. 它是怎么接进去的

ZCode 从 `<ZCode 安装目录>\resources\config\provider\zcode-builtin.json` 读取 provider 表。主进程启动时会先看环境变量 `ZCODE_BUILTIN_PROVIDER_CONFIG_FILE`，有值就用它，没有才回落到安装目录。

所以接入方式只有一步：**启动时换一份 provider 表**，把 `baseUrl` 指向本机代理。

```
ZCode ──HTTP──> 127.0.0.1:8788（reqslim） ──HTTPS──> 上游 API
                    │
                    ├─ 命中缓存：直接返回，不发出请求
                    └─ 未命中：原样转发（含 Authorization 头）
```

provider 用的网关不止一个时（API key 模板和套餐账号常不同），每个上游占一个本地端口，但仍是一个代理进程、一份流量记录。

凭据不经过我们的代码：ZCode 自己带 `Authorization`，代理只做转发，不读取、不落盘。代理只监听 `127.0.0.1`，局域网访问不到。

---

## 2. 安装（一次性）

```bat
cd /d "<模块目录>"
python tools\install_zcode.py
```

输出应该类似：

```
zcode       <自动探测到的安装目录>
module      <模块目录>
config      %USERPROFILE%\.zcode\reqslim
listen      127.0.0.1:8788  ->  https://open.bigmodel.cn/api/anthropic
            account:bigmodel-individual-coding-plan, account:bigmodel-team-coding-plan, bigmodel-api
listen      127.0.0.1:8789  ->  https://zcode.z.ai/api/v1/zcode-plan/anthropic
            account:zai-start-plan, account:bigmodel-start-plan
launcher    <模块目录>\start-zcode.bat
```

一个上游一行 `listen`。如果 `zcode` 那行报错说找不到，手动指一下：

```bat
python tools\install_zcode.py --zcode-dir "<ZCode 安装目录>"
```

安装器会写两类东西：

**模块目录里**（脚本，跟着代码走，可随文件夹整体搬迁）

| 文件 | 作用 |
|---|---|
| `start-zcode.bat` | 启动代理 + 启动 ZCode |
| `zcode-stats.bat` | 实时报告（需代理在运行） |
| `zcode-report.bat` | 离线报告（读 JSONL，代理停了也能看） |

**`%USERPROFILE%\.zcode\reqslim\`**（数据，每台机器独立）

| 文件 | 作用 |
|---|---|
| `zcode-builtin.proxied.json` | 改写后的 provider 表 |
| `zcode-builtin.original.json` | 原表备份，出问题可对比 |
| `requests.jsonl` | 运行后生成，逐请求记录 |

安装器参数：

| 参数 | 说明 |
|---|---|
| `--upstream` | 上游地址，默认 BigModel 的 anthropic 端点；也可填预设名 `bigmodel` / `zai` / `anthropic` / `moonshot` / `deepseek` |
| `--extra-upstream` | 额外上游（URL 或预设名，可重复）。同一个套餐家族常落在不同网关（API key 模板走 `open.bigmodel.cn`，套餐账号走 `zcode.z.ai`），每个上游分一个本地端口，全部收进同一个代理进程、同一份报告 |
| `--port` | 主端口，默认 `8788`，额外上游依次 +1 |
| `--no-autostart` | 只写配置和手动启动器，不注册开机自启、不设用户级环境变量（Windows 默认注册） |
| `--uninstall` | 移除自启项、用户级环境变量和生成的配置文件；保留 `requests.jsonl` |
| `--zcode-dir` | 手动指定 ZCode 安装目录（或 macOS 的 `Contents/Resources`） |
| `--package-dir` | 模块目录，默认取脚本的上级，一般不用给 |
| `--dry-run` | 只打印会改哪些 provider，不写文件 |

安装器还会打印 `uncovered` 一行：表里存在但没接进来的 anthropic 上游。你的 provider 若在其中，真实流量就不会经过代理——用 `--extra-upstream` 补上即可。

> 安装器是从代码目录直接运行的脚本，**不随 `pip install` 分发**。`pip install` 只提供运行时（`reqslim-zcode` 命令）；ZCode 接入仍然要在代码目录里跑一次 `tools/install_zcode.py`，因为它要读本机的 provider 表并生成启动器。

---

## 3. 日常使用

**Windows 装好后什么都不用做**：代理已注册到启动文件夹（登录即运行，`pythonw` 无窗口），`ZCODE_BUILTIN_PROVIDER_CONFIG_FILE` 已设为用户级环境变量——直接打开 ZCode（任务栏 / 开始菜单 / 任何方式），流量自动走代理。

- 代理日志：`%USERPROFILE%\.zcode\reqslim\proxy.log`
- ZCode 更新后无需重装：代理每分钟检查 ZCode 自带 provider 表，变更后一分钟内自动重新改写接入
- 代理已在运行时再点一次启动项无害（检测到等价实例直接退出）

`start-zcode.bat` 保留为手动兜底：换端口调试、或自启项被杀软清掉时用。它会弹一个最小化的黑窗口（代理），然后 ZCode 正常启动，**窗口不能关**。

首次验证：双击 `<模块目录>\zcode-stats.bat`，看到 `requests` 在涨就说明接通了。

---

## 4. 看数据

| 方式 | 操作 |
|---|---|
| 实时（代理在跑） | 双击 `zcode-stats.bat` |
| 浏览器 | `http://127.0.0.1:8788/v1/report` |
| 离线（读历史） | 双击 `zcode-report.bat` |

输出示例：

```
requests        8
upstream calls  3
cache hits      5  (62.5%)
  by level       failure_memo=2, normalized=2, raw=1
tokens in/out   6000 / 320
tokens avoided  ~1320
request kinds   {'tool_turn': 8}
duplicates      5   storms 5   near-dupes 2

top repeated requests
  d3e2b2f9e9e8  x4   tool_turn    msgs=3   hits=3
```

字段含义：

| 字段 | 含义 |
|---|---|
| `requests` | ZCode 发起的请求总数 |
| `upstream calls` | 真正打到上游的次数 |
| `by level` | `raw` = 字节完全相同命中（最便宜）；`normalized` = 规范化后命中；`failure_memo` = 失败记忆命中 |
| `tokens in/out` | **真正打到上游**的 token 数，命中回放不重复计入 |
| `tokens avoided` | 省掉的请求体估算 token（按字节 / 3） |
| `request kinds` | `first_turn` / `tool_turn` / `continuation` / `compaction` |
| `duplicates` | 同一个请求出现了几次以上 |
| `storms` | 60 秒内重复的请求数（重试循环的特征） |
| `near-dupes` | 最后一轮相同但历史不同——**缓存不了，但说明上下文在膨胀** |

`requests.jsonl` 默认在 32 MB 处轮转：每次启动代理时检查，超限就把上一份改名为 `requests.jsonl.1` 再重新开始。轮转只在启动时发生，所以一次会话的记录始终完整；`--report` 读的是当前文件。

---

## 5. 代理参数

一般不用改，需要时在启动器里追加：

```bat
python -m reqslim.zcode --upstream https://open.bigmodel.cn/api/anthropic --port 8788 --ttl 1800
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--upstream` | BigModel | 单上游模式的地址 |
| `--port` | `8788` | 单上游模式的端口 |
| `--listen` | — | `PORT=URL`，一端口一上游，可重复；多上游时由启动器生成 |
| `--ttl` | `3600` | 缓存存活秒数 |
| `--max-cache` | `4096` | 最多缓存多少条响应 |
| `--failure-ttl` | `20` | 失败记忆秒数，`0` 关闭 |
| `--no-cache` | — | 关闭缓存，只保留转发和记录（排查用） |
| `--no-coalesce` | — | 关闭单飞合并 |
| `--record` | `%USERPROFILE%\.zcode\reqslim\requests.jsonl` | 记录路径 |
| `--no-record` | — | 不写记录 |

多上游时缓存、单飞、失败记忆都**按上游分区**：同一个请求换一个网关就是另一段对话，不会拿 A 网关缓存的回答回放给 B 网关的请求——否则摘掉代理后行为就不一致了。

`--host` 默认 `127.0.0.1`，只监听本机回环。**不要改成 `0.0.0.0`**：代理会原样转发 `Authorization` 头，暴露到局域网等于把 API key 交给同网段的任何人。

---

## 6. 它到底省了什么

ZCode 每轮重建请求，有三处「含义没变但字节变了」的漂移，代理把它们从缓存键里剥离：

| 漂移 | 处理 |
|---|---|
| `cache_control` 断点每轮挪位置 | 剥离 |
| `tool_use.id` / `tool_use_id` 每次会话重新生成 | 剥离 |
| `tools` 数组由内置 + skills + MCP 拼装，顺序可能变 | 按名字排序 |

另外两个机制：

- **单飞合并**：同一个请求在上一个还没结束时又来一次（子 agent 并发、超时重试），后到的等前一个完成，不重复打上游。
- **失败记忆**：上游 429/5xx 时按请求记下，20 秒内相同重试直接回放该错误，避免重试风暴把一次失败放大成十次。

**它不做的事**：不改写响应、不裁剪上下文、不换模型。裁剪 Anthropic 对话需要保持 `tool_use` / `tool_result` 严格配对和 thinking 签名，改错会在几轮之后才炸，代价太大。

---

## 7. 换一台机器 / 多设备

模块本身是可搬的：**拷贝整个 `<模块目录>` 过去，跑一次安装器即可。**

```bat
:: 1. 把整个文件夹拷到新机器任意位置
:: 2. 进到那个文件夹
cd /d "<新位置>\reqslim"
:: 3. 重新生成配置与启动器（会自动探测 ZCode 装在哪）
python tools\install_zcode.py
:: 4. 双击 start-zcode.bat
```

需要重跑安装器的原因：`zcode-builtin.proxied.json` 是**从本机 ZCode 的 provider 表复制**出来的快照，不同版本、不同套餐的表可能不一样；启动器里的 ZCode 可执行文件路径也是本机的。

`%USERPROFILE%\.zcode\reqslim\` 下的数据（`requests.jsonl` 等）不会跟着走，每台机器各记各的。

多台机器同时用同一个 `--port` 没有冲突——代理只监听本机回环。

---

## 8. macOS / Linux

安装器会生成 `start-zcode.sh` 而不是 `.bat`，探测 `Contents/Resources` 和常见 Linux 安装路径。

```bash
cd "<模块目录>"
python3 tools/install_zcode.py
./start-zcode.sh
```

**这一路径未经实机验证**（开发机是 Windows）。如果探测失败：

```bash
python3 tools/install_zcode.py --zcode-dir "/Applications/ZCode.app/Contents/Resources"
```

报告手动看：

```bash
python3 -m reqslim.zcode --report "$HOME/.zcode/reqslim/requests.jsonl"
```

---

## 9. 排查

| 症状 | 原因 | 处理 |
|---|---|---|
| 安装器报找不到 ZCode | 装在非常规位置 | `--zcode-dir "<安装目录>"`，或设 `ZCODE_HOME` |
| 安装器报 no provider uses ... | 你的套餐走的是别的上游 | 用 `--upstream` 填真实地址；`--dry-run` 可先看会改哪些 |
| ZCode 报连接失败 / 一直转圈 | 代理窗口没起来或已关闭 | 重开启动器；或手动 `cd /d "<模块目录>"` 后跑 `python -m reqslim.zcode`，看报错 |
| 启动器一闪而过 | 端口被占用 | `netstat -ano \| findstr 8788` 找进程，或换 `--port 8799` 并重跑安装器 |
| `zcode-stats.bat` 提示 proxy is not running | 代理没启动 | 先开启动器 |
| `requests` 一直是 0 | 请求没走代理 | 确认 ZCode 当前选中的 provider 出现在安装器的 `listen` 行里；在 `uncovered` 行里就再跑一次安装器加 `--extra-upstream` |
| 报 `invalid signature` | thinking 签名重放被拒 | 用 `--no-cache` 启动确认是否缓存导致；确认后长期加该参数 |
| 报 401 | 上游没收到 Authorization | 检查是否改用了别的网关；本代理默认透传所有头 |
| ZCode 更新后失效 | 新版 provider 表结构可能变化 | 重跑一次安装器 |
| 缓存命中率很低 | 每轮历史都不同，属正常 | 看 `near-dupes`，数字高说明该压缩上下文或拆任务 |

---

## 10. 回滚

**临时关闭**：用 `--no-autostart` 安装、或不经自启启动时，直接双击 ZCode 图标即可——环境变量不设置，走原路径。

**彻底清理**（推荐）：

```bash
python tools/install_zcode.py --uninstall
```

它会删掉启动文件夹里的 `reqslim proxy.lnk`、用户级环境变量 `ZCODE_BUILTIN_PROVIDER_CONFIG_FILE` 和 `%USERPROFILE%\.zcode\reqslim\` 下的生成文件（保留 `requests.jsonl` 流量记录）。已开的窗口仍带着旧环境变量，重启 ZCode 后恢复直连；注销/重登录后环境变量从所有新进程消失。

也可以手动删：删启动项 lnk + `reg delete HKCU\Environment /v ZCODE_BUILTIN_PROVIDER_CONFIG_FILE /f` + 删配置目录。

ZCode 安装目录和用户配置自始至终没有被修改过。

---

## 11. 已知限制

- **近似重复缓存不了**：同一个问题但历史更长，内容确实不同，不该命中。这类请求会体现在 `near-dupes` 里。
- **流式错误不缓存**：上游用 `200 + event: error` 报错时（过载就是这样），这次响应不会被缓存，重试会真实打到上游——宁可多打一次，也不回放一个假的空回答。
- **超过 1 MB 的请求体不缓存**：保护内存，长上下文会话的最后几轮通常属于这类。
- **缓存命中时不是逐字流式**：会一次性返回完整 SSE，速度更快，但少了打字机效果。
- **只代理 `anthropic-messages` 协议的 provider**：OpenAI 兼容的那几个模板不在改写范围。
- **代理是单进程单实例**：同一端口开第二个会直接报错退出，不会自动换端口。Windows 默认的 `SO_REUSEADDR` 允许两个进程绑同一端口并静默抢流量，所以这里特意关掉了。
- **ZCode 更新后表同步有 ≤1 分钟窗口**：自启代理每 60 秒检查一次 ZCode 自带 provider 表，更新落地后的新 provider 在下一次检查时自动接入；若恰好在检查间隙重启 ZCode，那一次启动读到的还是上一份表，之后再自动补齐。
- **新增网关需要重跑安装器**：表监视只覆盖已知上游上的变化；ZCode 若接入了全新网关（新的 `baseUrl`），需要重跑一次安装器把它加进监听（`--extra-upstream`）。
- **自启依赖三样东西完好**：Python 解释器路径（lnk 里写死 `pythonw.exe`）、模块目录位置、启动文件夹权限。移动模块文件夹或升级 Python 后重跑一次安装器即可修复；杀软若清掉启动项，手动兜底 `start-zcode.bat` 始终可用。

---

## 12. 目录速查

```
<模块目录>\
├── reqslim\                          核心代码
│   ├── types.py                      响应与用量值对象
│   ├── cache.py                      带 TTL 的 LRU 响应缓存
│   └── zcode\                        ZCode 特化实现
│       ├── normalize.py              请求规范化（剥离漂移字段）
│       ├── stream.py                 流装配 / 重放（含 thinking 签名）
│       ├── analytics.py              流量记录与统计
│       ├── proxy.py                  代理主逻辑
│       └── __main__.py               CLI
├── tools\install_zcode.py            安装器（自动探测 + 生成启动器 + 注册自启）
├── tests\test_zcode.py               32 个用例
├── start-zcode.bat                   启动器（安装时生成）
├── zcode-stats.bat / zcode-report.bat 报告（安装时生成）
└── docs\ZCode接入指南.md             本文

%USERPROFILE%\.zcode\reqslim\         运行时数据
├── zcode-builtin.proxied.json
├── zcode-builtin.original.json
└── requests.jsonl
```
