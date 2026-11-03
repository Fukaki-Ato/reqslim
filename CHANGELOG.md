# Changelog

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
