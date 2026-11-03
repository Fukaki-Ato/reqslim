# reqslim 异机装配指南（AI 执行版）

> 本文档的读者是 **AI agent**，不是人。按步骤顺序执行；每步先看「动作/预期」，
> 实际结果与「预期」不符时走该步的「失败分支」，不要即兴发挥、不要跳步。
> 人类版图文说明见 [ZCode接入指南.md](ZCode接入指南.md)；本文与其冲突时以代码实际行为为准。

---

## 0. 元信息

### 0.1 任务目标

在一台**装有 ZCode 的新设备**上，把 reqslim 代理装到 ZCode 前面：

- ZCode 的所有 `anthropic-messages` 协议请求改走本地代理（改写 provider 表副本、环境变量与运行时缓存表，均留备份；不改 ZCode 安装目录、不改用户设置、不碰登录凭据）；
- 代理按**上游网关**分端口监听（默认从 8788 递增），一个进程管所有端口；
- 验收标准：ZCode 回答内容不变、`/v1/health` 可达、`requests.jsonl` 随真实使用增长。

### 0.2 硬性前提（逐条检查，全部满足才继续）

| # | 条件 | 检查命令 | 预期 |
|---|------|----------|------|
| P1 | Python ≥ 3.9 可用 | `python -c "import sys; assert sys.version_info >= (3, 9); print(sys.version)"` | 打印版本号，无异常 |
| P2 | ZCode 已安装且含 provider 表 | 步骤 2 的探测 | 找到 `config/provider/zcode-builtin.json` |
| P3 | 可达目标上游网关 | `python -c "import urllib.request; print(urllib.request.urlopen('https://open.bigmodel.cn', timeout=10).status)"` | 任何 HTTP 状态码（能建连即可） |

P1 失败 → 停下，要求用户安装 Python 3.9+（Windows 安装时勾选 *Add python.exe to PATH*）。
P3 失败 → 停下报告网络问题，不要继续（代理装上了也打不通上游）。

### 0.3 禁止假设（每条都对应一次真实翻车）

1. **不要假设 ZCode 的安装路径**——必须走步骤 2 的候选探测或 `--zcode-dir` 显式指定。
2. **不要假设 `python` 可用**——Windows 上 `where python` 可能命中 Microsoft Store stub（假成功），必须先过 P1。
3. **不要复用源码目录里现成的 `start-zcode.bat`**——它是上一台机器的安装器生成的产物，内嵌那台机器的 Python 路径和上游表；换机后必须由安装器重新生成。
4. **不要假设 provider 表里只有一种上游**——先读表（步骤 1）再决定参数；漏掉的上游会绕过代理直连。
5. **不要假设 8788–8792 空闲**——Windows 上 Hyper-V/WSL 会保留动态端口段；绑定失败时换 `--port` 重装。
6. **不要手动编辑 `start-zcode.bat`、`zcode-builtin.proxied.json`**——它们是安装器的产物，下次安装会被整体覆盖；要改行为就改安装参数重跑安装器。

### 0.4 输入变量（执行中逐步确定，代入后续步骤）

| 变量 | 含义 | 确定于 |
|------|------|--------|
| `$REQSLIM` | reqslim 源码目录（含 `reqslim/`、`tools/`） | 步骤 1 |
| `$TABLE` | ZCode 的 provider 表绝对路径 | 步骤 2 |
| `$UPSTREAMS` | 有序上游列表 `(port, url)`，主上游在前 | 步骤 3 |

---

## 1. 把源码拷到目标设备

**动作**：将整个 reqslim 项目目录复制到目标设备任意可写位置（如 `~/reqslim`）。
必须保留的结构：

```
$REQSLIM/
├── reqslim/          # 包本体（必须）
├── tools/install_zcode.py   # 安装器（必须）
├── setup.bat         # Windows 一键装配（可选，人用）
├── pyproject.toml
└── docs/
```

可排除（不参与运行）：`__pycache__/`、`.workbuddy-ai/`、`tests/`（可选保留）。
**必须排除目标设备上无意义但会引起混淆的产物**：本机生成的
`start-zcode.bat`、`start-zcode.sh`、`zcode-stats.bat`、`zcode-report.bat`
（安装器会重新生成并覆盖它们；残留旧文件只会误导排查）。

**验证**（在 `$REQSLIM` 下，用 P1 通过的那个解释器）：

```bash
python -c "import reqslim.zcode; print('ok')"
```

预期输出 `ok`。失败 → 检查目录结构是否完整（缺 `reqslim/` 子包）或 Python < 3.9。

> 启动器用 `%~dp0`（Windows）/ `$(dirname $0)`（POSIX）定位模块目录，与 cwd 无关，
> 因此**目录放在哪里都行**，但装好后不要再挪动 `$REQSLIM`（启动器内嵌该路径）。

---

## 2. 定位 ZCode 与 provider 表

**动作**：按顺序探测以下候选目录，找到含
`config/provider/zcode-builtin.json` 的**资源目录**即命中。

Windows 候选（与安装器内置顺序一致）：

```
%ZCODE_HOME%                              （环境变量，最优先）
D:/Zcode    C:/Zcode                      （作者机器的惯用位置）
%ProgramFiles%/ZCode
%ProgramFiles(x86)%/ZCode
%LOCALAPPDATA%/Programs/ZCode
```

POSIX 候选：

```
$ZCODE_HOME
/Applications/ZCode.app/Contents/Resources
/opt/Zcode/resources   /opt/zcode/resources   /usr/lib/zcode/resources
~/Applications/ZCode.app/Contents/Resources
```

也可以用一条命令枚举：

```bash
python - <<'EOF'
import os
from pathlib import Path
candidates = [os.environ.get("ZCODE_HOME")] if os.environ.get("ZCODE_HOME") else []
if os.name == "nt":
    candidates += ["D:/Zcode", "C:/Zcode",
                   Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "ZCode",
                   Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "ZCode"]
else:
    candidates += ["/Applications/ZCode.app/Contents/Resources",
                   "/opt/Zcode/resources", "/opt/zcode/resources",
                   "/usr/lib/zcode/resources"]
for root in candidates:
    root = Path(root)
    for res in (root, root / "resources", root / "Contents" / "Resources"):
        if (res / "config" / "provider" / "zcode-builtin.json").exists():
            print(res); raise SystemExit(0)
raise SystemExit("NOT FOUND")
EOF
```

**预期**：打印资源目录路径。记为 `$RESOURCES`，则 `$TABLE = $RESOURCES/config/provider/zcode-builtin.json`。

**失败分支**：

- `NOT FOUND` → 请用户给出 ZCode 安装目录，装配时改用
  `--zcode-dir "<安装目录或 Resources 目录>"`（两种都接受），或先 `set ZCODE_HOME=<目录>`。
- 表存在但结构异常（读不出 `config.providerConfigRules`）→ 停下报告，ZCode 版本可能不同，不要硬改。

---

## 3. 读 provider 表，确定上游清单

**动作**：提取表中所有 `anthropic-messages` 协议的 distinct baseUrl（这就是必须接进代理的全集）：

```bash
python - "$TABLE" <<'EOF'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
rules = data.get("config", {}).get("providerConfigRules", {})
urls = set()
for key in ("providerRules", "templateRules"):
    for entry in rules.get(key) or []:
        api = (entry.get("config") or {}).get("api") or {}
        if api.get("type") == "anthropic-messages":
            base = str(api.get("baseUrl", "")).rstrip("/")
            if base and not base.split("//", 1)[-1].split("/", 1)[0].startswith(("127.0.0.1", "localhost")):
                urls.add(base)
print("\n".join(sorted(urls)))
EOF
```

**预期**：每行一个上游 URL，例如：

```
https://open.bigmodel.cn/api/anthropic
https://zcode.z.ai/api/v1/zcode-plan/anthropic
```

**失败分支**：

- 输出为空 → 表里没有 anthropic-messages 协议的 provider，reqslim 对这台 ZCode 无可削减对象，停下并向用户说明。
- 含 `127.0.0.1`/`localhost` 条目 → 该表已被上一轮装配改写过（正常，跳过这些行）。

**确定 `$UPSTREAMS`**：

- 主上游 = URL 列表第一个（或用户指定）；其余全部作为附加上游。
- 端口分配规则（安装器固定行为）：主上游拿 `--port`（默认 8788），每个附加上游依次 +1（8789、8790、…）。
- 上游 URL **必须与表中原样一致**（安装器按剥离尾部 `/` 后的精确串匹配；路径前缀要保留，代理靠它拼转发地址）。

---

## 4. dry-run 装配

**动作**（用 P1 通过的同一个解释器跑安装器——启动器会内嵌该解释器路径）：

```bash
cd "$REQSLIM"
python tools/install_zcode.py \
  --upstream "<主上游URL或preset>" \
  --extra-upstream "<附加上游1>" \
  --extra-upstream "<附加上游2>" \
  --dry-run
```

`--upstream` 也接受 preset 名：`bigmodel` / `zai` / `anthropic` / `moonshot` / `deepseek`。

**预期**：输出含

```
listen      127.0.0.1:8788  ->  <主上游>
            <被改写的 providerId 列表>
uncovered   ...             ← 若还有上游没接进来
dry run: nothing written
```

**失败分支**：

- `no provider in the table uses any of: ...` → 传入的 URL 与表中不符。回步骤 3 逐字符核对（注意大小写、路径尾缀）。
- 出现 `uncovered` 行 → 把每个未覆盖 URL 追加为 `--extra-upstream` 重跑本步，直到 `uncovered` 为空（或经用户确认保留）。

---

## 5. 实装

**动作**：去掉 `--dry-run` 重跑上一条命令。

**预期**：写出以下产物（无需手动创建任何东西）：

| 产物 | 路径 | 作用 |
|------|------|------|
| 改写后的 provider 表 | `~/.zcode/reqslim/zcode-builtin.proxied.json` | 环境变量指向的回落表：缓存尚未生成的新机器/新版本读它 |
| 运行时缓存表改写 | `~/.zcode/v2/runtime/provider/*/*/endpoint-*/zcode-builtin.json` | agent 宿主**优先读取**的表（asar 证实，环境变量只是回落）；原件备份同目录 `.bak-reqslim`，卸载时还原 |
| 原表备份副本 | `~/.zcode/reqslim/zcode-builtin.original.json` | 仅供 diff 参考，运行时不读 |
| 自启配置 | `~/.zcode/reqslim/autostart.json` | `--autostart` 模式读：监听表、记录路径、ZCode 表路径 |
| 无窗口代理入口 | `~/.zcode/reqslim/proxy_autostart.pyw` | pythonw 启动，日志重定向 `proxy.log` |
| 启动项（仅 Windows） | 启动文件夹 `reqslim proxy.lnk` | 登录自动起代理；用户级环境变量由 `setx` 写入 |
| 手动启动器 | `$REQSLIM/start-zcode.bat`（POSIX 为 `start-zcode.sh`） | 兜底：设环境变量 + 起代理 + 起 ZCode |
| 查数助手（仅 Windows） | `$REQSLIM/zcode-stats.bat`、`zcode-report.bat` | 双击看报告 |

Windows 上安装即完成自动装载：代理登录自启、ZCode 任何方式启动都走代理，无需再点 bat。
POSIX 上仅生成手动启动器（自启未实现），把 `start-zcode.sh` 加进桌面会话自启即可。

同时输出每个端口的改写明细。**验证**：

```bash
python -c "import json; d=json.load(open(r'<HOME>/.zcode/reqslim/zcode-builtin.proxied.json', encoding='utf-8')); print('written')"
```

再跑一次 `python tools/install_zcode.py --dry-run`：所有运行时缓存表应显示 `untouched`（首次安装为 rewritten），说明 agent 宿主实际优先读取的缓存层也已接上。

**失败分支**：安装器报 `cannot bind` 类错误不会在这一步（安装器不绑端口）；
端口冲突在步骤 6 才暴露，见下。

---

## 6. 启动并验证

**前置**：先关闭正在运行的 ZCode（它必须重启才会带上新的环境变量）。

**动作**（Windows 优先用自启入口，已注册的话代理此刻应已在跑）：

```bat
:: Windows —— 直接启动 ZCode 即可（代理已由登录自启拉起）；
:: 若自启项不存在（如被杀软清除），用手动启动器兜底：
"$REQSLIM\start-zcode.bat"
```
```bash
# POSIX
"$REQSLIM/start-zcode.sh"
```

手动启动器行为：起代理进程（Windows 为最小化独立窗口）→ 轮询健康端点直到就绪（最多约 10 秒）→ 启动 ZCode。
自启模式下代理由 `proxy_autostart.pyw`（pythonw 无窗口）拉起，输出在 `~/.zcode/reqslim/proxy.log`；重复触发启动项无害——代理检测到等价实例在跑会直接退出。

**启动器已内置健康探测**：`start-zcode.bat` 会在启动 ZCode 前轮询
`http://127.0.0.1:8788/v1/health`（最多 10 次）；代理没起来（端口被占、导入失败）
时启动器会停下并提示看代理窗口，**不会**把 ZCode 指向死端口。
启动约 5 秒后做下面的探测作为复核：

```bash
curl -s http://127.0.0.1:8788/v1/health
# 或无 curl 时（Windows）：
powershell -NoProfile -Command "irm http://127.0.0.1:8788/v1/health"
```

**预期**：

```json
{"ok": true, "upstreams": {"8788": "https://...", "8789": "https://..."}, "cache": true, "coalesce": true}
```

`upstreams` 的端口→URL 映射必须与步骤 3 确定的 `$UPSTREAMS` 一致。

**失败分支**：

- 健康检查连不上 → 代理没起来。看代理窗口的报错（或手动前台跑
  `python -m reqslim.zcode --listen 8788=<URL>` 复现）：
  - `cannot bind ... port is taken / reserved range` → 换 `--port 18888`（或其他空闲口）重跑步骤 4–5；
  - `reqslim cannot be imported` → 回步骤 1 的验证，检查 `$REQSLIM` 位置是否被挪动。
  - 修好后**重新跑安装器**再启动（启动器内嵌端口，改口必须重装）。
- 健康检查 OK 但 ZCode 请求全挂 → 逐层确认 provider 的 `baseUrl` 真的指向 `127.0.0.1`：环境变量表 `~/.zcode/reqslim/zcode-builtin.proxied.json` **之外**，还要查运行时缓存表 `~/.zcode/v2/runtime/provider/<版本>/endpoint-*/zcode-builtin.json`（agent 宿主优先读它）；改写后必须重启 ZCode 才会重建 provider registry。

**验收（三选一都过才算完成）**：

1. 在 ZCode 里随便跑一轮对话，然后：
   `curl -s http://127.0.0.1:8788/v1/stats` → `requests` 计数增长；
2. `curl -s http://127.0.0.1:8788/v1/report` → 出现 `requests / cache hits / tokens` 报告；
3. `~/.zcode/reqslim/requests.jsonl` 存在且行数在增长（只记哈希与计数，**不含 prompt 内容**）。

---

## 7. 日常使用与回滚

**日常**：以后每次都用 `start-zcode.bat`（或 `.sh`）启动 ZCode。直接开 ZCode = 不走代理（不报错，只是没有削减）。

**看数据**：

```bash
curl http://127.0.0.1:8788/v1/report              # 实时
python -m reqslim.zcode --report "<HOME>/.zcode/reqslim/requests.jsonl"   # 离线
```

**回滚**（零残留，ZCode 自身从未被改动）：

1. 直接正常启动 ZCode（不经启动器）→ 立即恢复原行为；
2. 彻底清除：删 `~/.zcode/reqslim/` 目录 + `$REQSLIM` 下四个生成物
   （`start-zcode.bat|.sh`、`zcode-stats.bat`、`zcode-report.bat`）。
   也可以整目录删除 `$REQSLIM`。

---

## 8. 已知限制（向用户转述时不要漏）

- 只处理 `anthropic-messages` 协议；OpenAI 兼容 provider 不在改写范围。
- **macOS / Linux 路径未经实机验证**（开发机为 Windows）；POSIX 分支代码在但无实机背书。
- 超过 1 MB 的请求体不缓存（内存保护）；流式命中回放无打字机效果；同端口第二个进程会拒绝启动而非抢流。
- 缓存/合并/失败记忆按上游分区——不同网关的缓存互不串用（这是设计约束，不是 bug）。
- 完整清单见 README「已知限制」与 `ZCode接入指南.md` 第 11 节。

---

## 附：步骤状态机速查

```
P1-P3 前提 ──fail──> 停，报告
   │ok
步骤1 拷贝 ──import 失败──> 补结构/换解释器
   │ok
步骤2 定位 ZCode ──NOT FOUND──> 问用户要 --zcode-dir / ZCODE_HOME
   │ok
步骤3 读表 ──空──> 停，无 anthropic provider
   │ok → $UPSTREAMS
步骤4 dry-run ──uncovered──> 追加 --extra-upstream 重跑
   │        ──URL 不匹配──> 回步骤 3 核对
   │ok
步骤5 实装 ──> 4 个产物落盘
   │ok
步骤6 启动 ──health 不通──> 代理未起：换口重装（4→5→6）
   │ok
验收 3 项 ──> 完成（日常走启动器）
```
