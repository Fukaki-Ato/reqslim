# reqslim

[![CI](https://github.com/Fukaki-Ato/reqslim/actions/workflows/ci.yml/badge.svg)](https://github.com/Fukaki-Ato/reqslim/actions/workflows/ci.yml)

[简体中文](README.md) | [English](README.en.md)

A request-reduction layer that sits in front of ZCode — **it never changes answers, only the number of requests**.

A coding agent burns dozens of turns and tool calls on a single task, and a large share of those requests are duplicates, predictable, or never needed the model at all. `reqslim` is a local proxy that intercepts them and keeps two separate ledgers — *how many times you logically asked* vs *how many times the upstream was physically hit* — so every saving is attributable to a specific mechanism.

Zero third-party dependencies. Standard library only.

```
ZCode ──HTTP──> 127.0.0.1:8788 (reqslim) ──HTTPS──> upstream API
                    ├─ hit:    answered locally, no request sent
                    └─ miss:   forwarded verbatim (Authorization header included)
```

---

## What it saves

| Waste | What it looks like | Treated by |
|---|---|---|
| **Exact duplicates** | Timeout retries, sub-agents asking the same thing concurrently | Raw byte hash, in-flight coalescing |
| **Same semantics, different bytes** | Cache breakpoints moving every turn, regenerated tool ids, reordered tool arrays | Request normalization |
| **Upstream failure amplification** | Identical retries right after a 429/5xx | Failure memo |

Four paths are tried in order; only if all miss is the request forwarded:

1. **Raw byte hash** — identical request bodies are settled by a single sha256, the cheapest possible check
2. **Normalized key** — strips the three things that churn every turn without changing meaning (below)
3. **In-flight coalescing** — the same request arriving while a previous one is still running waits for it instead of hitting the upstream again
4. **Failure memo** — when the upstream returns 429/5xx the request is remembered; identical retries within 20 seconds get the error replayed

---

## Three key decisions

**Normalization strips exactly three things.** `cache_control` breakpoints, `tool_use.id`, and `tools` array order — all three churn every turn with no semantic change, and without stripping them the hit rate is near zero. Everything else stays in the cache key: text, file contents, tool inputs, thinking budgets — because they genuinely change the answer.

**Stream assembly preserves `thinking` + `signature` verbatim.** Lose the signature and the upstream rejects the session several turns later — the symptom is far from the cause, the hardest class of bug to trace. The assembler handles every block type Anthropic actually emits: `text`, `thinking`, `redacted_thinking`, `tool_use`.

**No context trimming.** Trimming an Anthropic conversation requires keeping `tool_use` / `tool_result` strictly paired; getting it wrong explodes turns later, and the cost is never worth it.

---

## Quick start

```bash
git clone https://github.com/Fukaki-Ato/reqslim.git
cd reqslim
python tools/install_zcode.py
```

Requires Python 3.9+ (standard library only, no pip install needed; CI covers 3.9 / 3.11 / 3.13 × Windows / Linux).

The installer locates the ZCode install, copies the provider table, points its `baseUrl`s at the local proxy, and sets up **logon autoload** (Windows):

- the proxy is registered in the Startup folder and runs at logon via `pythonw` — no console window; logs at `%USERPROFILE%\.zcode\reqslim\proxy.log`;
- `ZCODE_BUILTIN_PROVIDER_CONFIG_FILE` is set as a user-level environment variable — ZCode then routes through the proxy no matter how it is started (taskbar, Start menu, other tools). **No launcher to click, ever**;
- the proxy checks ZCode's bundled provider table once a minute — **after a ZCode update, newly added providers are picked up within a minute**, no reinstall required.

Daily use: just open ZCode. `start-zcode.bat` remains as a manual fallback (handy for debugging on a different port). Uninstall:

```bash
python tools/install_zcode.py --uninstall   # removes the autostart entry and env var; keeps requests.jsonl
```

Full documentation — options, troubleshooting, rollback — in **[docs/ZCode接入指南.md](docs/ZCode接入指南.md)** (Chinese).
To set reqslim up on another machine's ZCode, there is a step-by-step runbook an AI agent can execute: **[docs/AI装配指南.md](docs/AI装配指南.md)** (Chinese).

The installer **never touches** the ZCode install directory, **never touches** user settings, and **never touches** credentials. It comes off cleanly at any time.

### Multiple upstreams

API-key templates and plan accounts often live on **different gateways** (e.g. `open.bigmodel.cn` vs `zcode.z.ai`). Each gateway gets its own local port inside the same proxy process and the same report; cache, coalescing and the failure memo are partitioned per upstream — replaying gateway A's cached answer to a gateway B request would break the "unmount and nothing changes" guarantee.

```bash
python tools/install_zcode.py \
  --extra-upstream https://zcode.z.ai/api/v1/zcode-plan/anthropic
```

The installer prints which upstreams in the table are still not proxied.

---

## Reading the numbers

```bash
curl http://127.0.0.1:8788/v1/report          # live
python -m reqslim.zcode --report <jsonl path>  # offline
```

```
requests        8
upstream calls  3
cache hits      5  (62.5%)
  by level       failure_memo=2, normalized=2, raw=1
tokens avoided  ~1320
duplicates      5   storms 5   near-dupes 2
```

`near-dupes` are requests with the same final turn but different history — never cacheable, but a high count means your context is ballooning.

![Live traffic report (synthetic demo data)](docs/assets/report-demo.png)

---

## Install (optional)

The launcher itself **does not require installing** — it sets `PYTHONPATH` to the module directory itself, independent of cwd and pip. Copy the folder to another machine, run the installer once, done.

If you want a console script:

```bash
pip install -e .        # provides the reqslim-zcode command
```

Requires **Python 3.9+** (declared in `requires-python`; CI covers 3.9 / 3.11 / 3.13 × Windows / Linux).

## Tests

```bash
python -m pytest        # 38 cases
```

pytest not required — the test file ships a minimal runner:

```bash
python tests/test_zcode.py
```

---

## Layout

```
reqslim/
├── types.py           response & usage value objects
├── cache.py           LRU response cache with TTL
└── zcode/             ZCode specifics (Anthropic Messages protocol)
    ├── normalize.py   request canonicalization (strips drift fields)
    ├── stream.py      stream assembly / replay (incl. thinking signatures)
    ├── analytics.py   traffic recording & reporting
    ├── proxy.py       proxy core
    └── __main__.py    CLI
tools/install_zcode.py installer (probe + rewrite provider table + register autostart)
tests/test_zcode.py    38 cases
docs/                  ZCode integration guides (Chinese)
```

## Design constraints

Priority order; conflicts resolve in favor of the top:

1. **Unmountable** — removing the proxy changes the request count and nothing else. No response rewriting, no model swapping, no request mutation.
2. **Framework-agnostic** — the core has zero dependencies; the proxy speaks the Anthropic Messages protocol and recognizes no client framework.
3. **Auditable** — logical requests and physical requests are ledgered separately, and every saving is attributed to the mechanism that produced it. Without that split you cannot tell a cache that never hits from one that always hits, and a failure-retry storm looks like ordinary traffic.

## What it does not do

- **No model swapping** (no routing down to cheaper models)
- **No context trimming** (see above)
- **No response rewriting**
- **No semantic guessing** — exact matching only, no embedding cache. Silently guessing is worse than not caching.

## Known limitations

- **Near-duplicates are not cached** — same question, longer history: the content genuinely differs, so it must not hit. These are counted in `near-dupes`.
- **Streaming errors are not cached and not memoized** — when the upstream reports `200 + event: error` (overloads do this), the response is not cached and the retry genuinely hits the upstream. Better one extra request than replaying a fake empty answer.
- **Request bodies over 1 MB are not cached** — memory protection; the last turns of long-context sessions are usually in this class.
- **Cache hits are not streamed verbatim** — the full SSE is returned at once, faster, but without the typewriter effect.
- **Only the `anthropic-messages` protocol is proxied** — OpenAI-compatible provider templates are out of scope.
- **Single process, single instance** — a second instance on the same port fails loudly. Windows' default `SO_REUSEADDR` would let it silently steal traffic, so it is deliberately disabled.
- **macOS / Linux paths are untested on real hardware** — the development machine is Windows.
