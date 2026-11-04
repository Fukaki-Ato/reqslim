# Changelog

## 0.2.5 (2026-11-04)

**请求削减对比：预估请求次数 vs 实际上游调用**

- 新增纯后端模块 `reqslim/zcode/comparison.py`：`compare_requests()` 以 `Event.hit` 为唯一口径，计算 `estimated_requests`（ZCode 逻辑请求数）、`actual_requests`（真正打到上游的次数）、`reduced_requests` 与减少比例，并按 `raw` / `normalized` / `coalesced` / `failure_memo` 归因
- 计数守恒：`actual + sum(by_mechanism) == estimated`；空/非法 `hit` 值按上游调用处理，避免虚报节省
- `/v1/stats` 新增 `comparison` 对象；`/v1/report` 顶部新增醒目的 `request reduction` 区块，直接显示减少量；旧字段 `requests` / `upstream` / `hits` 保留兼容
- 测试 77 → 80：覆盖对比守恒、非法/非字符串 hit、离线损坏记录、报告输出与 `/v1/stats` 暴露

## 0.2.4 (2026-11-04)

**ZCode 3.17.2 真实请求打通：本机 HTTPS + 客户端签名握手**

- **根因**：ZCode 3.17 对套餐账号启用客户端请求签名，签名器构造阶段拒绝 `http://` baseURL（`ClientRequestSigningV4Error: Client signing handshake requires HTTPS.`）。旧实现把 provider 指向 HTTP 本机端口，请求在发出前就失败，上游只看到缺少 `Authorization` 的 401；这不是账号凭据问题
- **本机 TLS**：新增 `reqslim/zcode/tls.py`，安装器用 OpenSSL 生成私有 CA、`127.0.0.1`/`localhost` 服务器证书和 `ca-bundle.pem`（系统根 + 私有 CA），代理所有监听改为 HTTPS；`ZCODE_AGENT_CA_CERT` 设为用户级环境变量，只让 ZCode 信任本机监听
- **签名握手转发**：代理新增 `/api/paas/...` 透传，按 provider **origin** 转发（不再错误拼成 `/api/anthropic/api/paas/...`），保留 `Authorization` 与全部签名头，且不进入缓存/流量记录
- **生命周期**：`autostart.json` 记录 TLS 证书；`--autostart` 同时比较 transport，旧 HTTP 实例会自动停掉；`health_on_port`/`stop_running_proxy`/启动器按 scheme 探测；`--uninstall` 删除 `tls/` 与两个环境变量；`--no-tls` 仅保留给旧客户端/排查
- **文档/测试**：README、README.en、ZCode 接入指南、AI 装配指南更新 HTTPS/CA/签名说明；测试 75 → 77（新增 HTTPS 监听与握手 origin 透传用例）
- **本机实测**：ZCode 3.17.2 + 5 个 HTTPS 监听，真实请求 `requests.jsonl` 新增 6 条 200；同一提示第二次发送命中 `normalized` 缓存（`cache hits 1 / 16.7%`，tokens avoided ~469）

## 0.2.3 (2026-11-03)

**第三轮审计修复：协议安全、卸载/重装生命周期与本机 ZCode 3.17.2 验证**

- **卸载/重装生命周期**：新增本地 `/v1/shutdown`，安装器与 `--uninstall` 先停旧代理再改缓存；修复“运行中的 CacheWatcher 把还原后的缓存重新改写回去”和 Windows 下 `proxy.log` 被占用导致卸载中断；换端口/换上游时从 `.bak-reqslim` 原始表重映射，不再把旧 loopback 端口误报为 `untouched`
- **`--autostart` 参数与等价实例**：`autostart.json` 保存 `ttl/max_cache/cache/coalesce/coalesce_wait/failure_ttl`；安装器新增对应参数；启动前比较 health 的 `upstreams` 映射，映射不同或只起了一部分时先停旧实例；`--no-cache` 等不再静默失效
- **请求体兼容**：支持 `Transfer-Encoding: chunked`（ZCode 3.17.2 的 agent 实际会用分块请求，旧实现会以 411/400 拒绝且不记录）
- **运行时缓存只读**：改写后设只读，阻止 ZCode 云刷新在启动窗口覆盖回直连地址；ZCode 日志会记录 `EPERM` 并继续使用本地表，重跑安装器/卸载会处理只读标记
- **协议/输入加固**：非对象 JSON 请求返回 400；非对象 200、非法 SSE 字段、非 dict content block、截断 tool input、合法 JSON 但错误表结构都转为受控 400/502/unreadable，不再让 handler/watcher 崩溃；`usable` 要求 stop_reason、合法 content block 与 usage
- **流式与响应头**：流式请求遇到 200 非 SSE 返回 502，不再伪造成空 SSE；中途断流写 SSE `error` 事件并记 502；保留上游 `Content-Type`、`Retry-After` 等响应头；`message_stop` 缺 `message_delta` 不再缓存 null stop_reason；支持 `/v1/messages/count_tokens`
- **请求路径/keep-alive**：不支持的 POST 路径关闭连接而不是留下未读 body；缺失/负数 Content-Length 拒绝、分块请求受支持；`urllib` 禁用环境代理与自动重定向，避免 POST 被改成 GET
- **归因与分析**：新增 `coalesced` 命中档；超过 1 MB 或未缓存的成功/失败响应通过短寿命 handoff 共享，单飞不再只对大请求失效；分析按网关 + 凭据 + key 分组，跨网关不再误报 duplicates/storms
- **缓存/规范化分区**：`cache_control` 只在 Anthropic 定义的位置剥离，工具 input/schema 中的同名字段保留；缓存/单飞/失败记忆加入凭据与 `anthropic-version` scope
- **报告**：JSONL 轮转按 UTF-8 字节并保持在预算内；离线 `--report` 不再截断 5 万条；文件不存在返回非零；空报告补齐 `avg_latency`
- 文档更新卸载/回滚/`--no-autostart`/轮转说明；测试 49 → 75；本机 ZCode 3.17.2 + 5 端口实例验证

## 0.2.2 (2026-11-03)

**修复回归：恢复运行时 provider 缓存表改写（0.1.0 的关键修复在 0.2.0 重构中丢失）**

- 反编译 ZCode 3.16.3 与 3.17.2 的 asar 确认：agent 宿主进程**优先读云端刷新的运行时缓存表**（`~/.zcode/v2/runtime/provider/<平台>/<版本>/endpoint-*/zcode-builtin.json`），`ZCODE_BUILTIN_PROVIDER_CONFIG_FILE` 指向的表只是回落——0.2.0 的「环境变量 + proxied 副本」机制在已有缓存的机器上会被静默绕过。本机 3.17.2 升级后实测复现：请求直连，`requests` 归零，且重跑安装器无效
- 安装器恢复缓存表改写：glob 所有版本目录逐份改写、每份一次性 `.bak-reqslim` 备份（重复运行与云刷新都不覆盖）、幂等；`--dry-run` 可预览
- 代理 `--autostart` 新增缓存表监视线程（`CacheWatcher`）：ZCode 升级带来的新版本缓存一分钟内自动改写，与 bundled 表监视共用 wiring 逻辑
- `--uninstall` 从 `.bak-reqslim` 还原所有缓存表——否则摘掉代理后 ZCode 永远指向死端口（永久 ECONNREFUSED）
- 修正安装器头注释与《ZCode 接入指南》§1 对读取优先级的错误断言；排查表补「升级后 requests 归零」「本地校验拒绝（零网络连接）」两行
- 测试 44 → 49

## 0.2.1 (2026-11-03)

**修复(遗留审计项,按修复方案设计全部落地)**

- **单飞等待改为截止时间制**:旧实现固定 `3×180s`,而 socket 超时只约束单次读间隙、不约束流总时长——慢速长流(128k `max_tokens` 常见)可以合法跑超过 540s,follower 随即并发转发,单飞恰好在最需要它的长回答场景失效。新增 `--coalesce-wait`(默认 600s,`0` 表示无限等),follower 在截止前持续等待并复查缓存,截止后并发转发保证进展
- **`anthropic-beta` 头纳入缓存键**(L1 raw hash 三元组 + L2 normalized):此前两个仅差 beta 开关的同体请求共享缓存项,可能把 beta A 的答案回放给 beta B——这是唯一能违反「摘掉后行为不变」承诺的已知路径。beta 特性集合排序归一,顺序/空格不敏感;ZCode 每会话 beta 集稳定,命中率不受影响
- **JSONL 写入时按字节轮转**:自动装载让代理常驻数周,旧的「仅启动时轮转」使日志无限增长;现在字节计数器超限即轮转(保留一份 `.1`),`--report` 语义不变。`proxy.log` 同样获得 5MB 运行期轮转(pyw 的 `_Log`)
- macOS 装配 `_as_install` 的 exe 路径修正(`Resources` 的 parent 是 `Contents`,未实机验证)
- POSIX 启动器按 `uname` 分派启动方式:Linux 不再使用 macOS 专属的 `open -a`,改为探测本目录或 PATH 下的二进制(未实机验证)
- 测试 38 → 44

## 0.2.0 (2026-11-03)

**自动装载（不再需要手动运行启动器）**

- 安装器默认注册开机自启（Windows）：启动文件夹快捷方式 + `pythonw` 无窗口运行（日志 `~/.zcode/reqslim/proxy.log`）
- `ZCODE_BUILTIN_PROVIDER_CONFIG_FILE` 写入用户级环境变量，ZCode 从任何入口启动都自动走代理
- 代理新增 `--autostart` 模式：读 `autostart.json`，检测等价实例避免重复启动（幂等）
- **适配 ZCode 更新**：表监视线程每 60 秒检查 ZCode 自带 provider 表，变更后自动重新改写 proxied 副本（原子替换）；源表路径失效后自动重新发现
- 新增 `reqslim/zcode/wiring.py`：表发现/改写/原子生成，安装器与监视线程共用同一份逻辑
- `--no-autostart` / `--uninstall` 参数；卸载保留 `requests.jsonl`

**正确性与健壮性（两轮审计）**

- 修复：上游 `200 + event: error`（过载形状）、非 JSON 200（网关 HTML）、截断 tool_use 输入三类坏响应不再被缓存回放
- 修复：缓存命中不再重复计入 token 用量；字节相同命中单独归因（`raw` 档）
- 修复：客户端中断的流记录为 499、上游中断记录为 502（此前都记 200）
- 修复：压缩请求分类对齐 ZCode 实际提示词措辞（对照运行时实证），不再漏报/误报
- 加固：失败记忆按 TTL 清扫 + 条数上限；缓存加字节预算（默认 128MB）；原始哈希映射改 LRU 逐条淘汰；请求体超 64MB 拒绝（413）；启动器在代理未就绪时不启动 ZCode
- 测试 25 → 38；CI 覆盖 Python 3.9/3.11/3.13 × Windows/Linux

## 0.1.0 (2026-11-02)

- 首个可用版本：原始字节哈希 / 规范化键 / 单飞合并 / 失败记忆四层请求削减
- 多上游分端口监听（缓存按上游分区）
- 流装配器完整处理 `text` / `thinking`+`signature` / `redacted_thinking` / `tool_use`
- ZCode 接入安装器（provider 表改写 + 启动器生成）与流量报告（JSONL + 实时/离线两种）
