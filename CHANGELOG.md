# Changelog

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
