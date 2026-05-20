# Agent Tech Pitfalls — Comprehensive Research Report

**Project:** Hermes Agent (NousResearch/hermes-agent ~21万行)
**Research Date:** 2026-05-19
**Status:** Phase 1–3 complete; Phase 4 implementation plan defined
**Confidence:** High (primary source analysis + OpenClaw/HONCHO cross-validation)

---

## Executive Summary

本报告基于对Hermes Agent源码的深度考古（18个隐藏精华提炼）、OpenClaw Swarm2架构规范族（1,487行5文件）、以及29个已知pitfall条目的系统分析，产出以下关键发现：

1. **Hermes已有Swarm2大部分核心机制**，但处于分散实现状态，未形成统一的多Agent编排层
2. **Context Compaction是最高杠杆改进点** — 29个pitfall中，对话质量影响最大的前5项有4项属于此域
3. **Worker生命周期管理是最大架构空白** — Swarm2规范定义了完整的状态机（healthy→watch→handoff_required→renewing→blocked），Hermes完全缺失
4. **Credential Pool的race condition是高危未决问题** — OAuth刷新竞态已部分缓解但未根治

---

## Phase 1 — Source Archaeology Results

### 18个隐藏精华（来源：HERMES_SOURCE_GEMS.md）

| # | 层级 | 发现 | 现状 |
|---|------|------|------|
| L4-1 | Memory Nudge | 长期记忆通过MEMORY.md注入，HOBO project证明有效 | ✅ 已实现 |
| L4-2 | Thinking Pre-fill | `assistant_msg["reasoning"]` 存储推理过程 | ✅ 已实现 |
| L3-1 | 上下文双通道 | System prompt + conversation context分离 | ✅ 已实现 |
| L3-2 | SubdirectoryHint | 工具路径提示，动态schema注入 | ✅ 已实现 |
| L3-3 | 工具自修复 | MCP工具加载失败后重新扫描 | ⚠️ 有但不完善 |
| L2-1 | Memory免疫系统 | Token计数阈值保护+智能摘要 | ⚠️ 有但不完善 |
| L2-2 | 智能预算管理 | 压缩率自适应 | ⚠️ 有但不完善 |

### HONCHO集成教训（9+ releases经验）

| 教训 | Hermes当前状态 |
|------|---------------|
| every-turn blocking round-trip不可接受 | ✅ Gateway异步架构已解决 |
| E2EE room adapter隔离消息 | ⚠️ 无E2EE room概念 |
| Honcho holographic prompt/trust score rendering | ❌ 完全缺失 |
| Honcho doctor fix | ⚠️ 部分自我诊断存在 |
| Feishu adapter webhook异常追踪 | ⚠️ 有基础错误分类 |

---

## Phase 2 — Architecture Gap Analysis: Hermes vs. Swarm2

### 2.1 已实现（对齐Swarm2）

| Swarm2规范 | Hermes实现 | 差距 |
|-----------|-----------|------|
| **Token计数追踪** | `hermes_state.py:412` `update_token_counts()` | ✅ 完整 |
| **Session持久化** | `state.db` (SQLite FTS5) | ✅ 完整 |
| **WAL checkpoint** | `hermes_state.py:216` `_try_wal_checkpoint()` | ✅ 完整 |
| **进程恢复** | `process_registry.recover_from_checkpoint()` | ✅ 完整 |
| **Context压缩** | `context_compressor.py` (820行) | ⚠️ 有但不完善 |
| **Profile/会话隔离** | `~/.hermes/profiles/<id>/` | ✅ 完整 |
| **Credential Pool** | `credential_pool.py` (1245行) | ⚠️ 有race condition |

### 2.2 缺失的Swarm2核心机制

| Swarm2机制 | 状态 | 影响 |
|-----------|------|------|
| **swarm.yaml roster** | ❌ 完全缺失 | 无法持久化worker身份 |
| **runtime.json worker state** | ❌ 完全缺失 | 无worker生命周期可见性 |
| **Mission ledger** | ❌ 完全缺失 | 无跨worker任务追踪 |
| **Handoff文件契约** | ❌ 完全缺失 | Worker renewal无法连续 |
| **Worker生命周期状态机** | ❌ 完全缺失 | 无法自动renew/restart |
| **3层内存架构** | ⚠️ MEMORY.md有，其他层缺失 | Worker记忆碎片化 |
| **Greenlight Gate** | ❌ 完全缺失 | 高风险操作无审批流 |
| **Auto-repair playbook** | ⚠️ 部分 | gateway自我修复存在 |
| **Control-plane API endpoints** | ❌ 完全缺失 | 无外部编排接口 |
| **Review Gate** | ❌ 完全缺失 | PR/发布无强制review |

### 2.3 关键架构差异

**Swarm2 = 持久多Agent编排层 + 文件契约 + 状态机**
**Hermes = 单Agent运行时 + 分散的可靠性机制**

这不是"缺功能"，而是**架构范式差异**。Hermes设计哲学是"单Agent可以自我修复"，Swarm2是"多Agent需要显式协调契约"。

---

## Phase 3 — Priority Matrix: 29 Pitfalls Ranked

评分标准：`风险分 = (失败频率 1-3) × (影响程度 1-3) × (修复难度 1-3)`  
影响程度：1=cosmetic, 2=functional degradation, 3=wrong behavior/data loss  
修复难度：1=单文件纯逻辑, 2=需跨模块协调, 3=需架构改动

### 3.1 Credential Pool（7 pitfall条）

| Pitfall | 风险分 | 优先级 | 修复难度 |
|---------|--------|--------|----------|
| OAuth Refresh Race — 单用refresh token竞态 | 3×3×2=**18** | 🔴 P0 | Medium |
| `_seed_from_singletons` 覆盖新鲜Token | 3×3×2=**18** | 🔴 P0 | Medium |
| Lease Counter软上限形同虚设 | 2×2×2=**8** | 🟡 P2 | Easy |
| Round-Robin每次select都persist | 2×2×2=**8** | 🟡 P2 | Easy |
| 429和其他错误TTL相同(3600s) | 2×2×1=**4** | 🟢 P3 | Easy |
| Release Lease静默忽略未知ID | 1×2×1=**2** | 🟢 P3 | Easy |
| 毫秒/秒时间戳歧义 | 1×1×1=**1** | 🟢 P3 | Easy |

**P0核心问题**：两个Token泄露机制形成"双重打击"：Session A刷新得到新token→写入pool→Session B启动读取pool→Session A进程崩溃→重启后`_seed_from_singletons`用旧auth.json覆盖→Session B拿到stale token→下次refresh失败。这个链条在长时间运行场景下几乎必然触发。

### 3.2 Rate Limit Tracker（4 pitfall条）

| Pitfall | 风险分 | 优先级 |
|---------|--------|--------|
| 仅支持x-ratelimit前缀，Anthropic/Google被忽略 | 3×2×2=**12** | 🟠 P1 |
| 非数字值静默转为0 | 2×2×2=**8** | 🟡 P2 |
| remaining_seconds负数后clamp为0 | 2×2×2=**8** | 🟡 P2 |
| 任意x-ratelimit-前缀触发全解析 | 2×1×1=**2** | 🟢 P3 |

**P1问题**：Anthropic rate limit header是`retry-after`和`x-ratelimit-requests-limit`，均不在解析列表中。系统无法感知Anthropic速率限制，可能导致无意义的重试。

### 3.3 Error Classifier（8 pitfall条）

| Pitfall | 风险分 | 优先级 |
|---------|--------|--------|
| 400 + 大Session误判为context_overflow | 2×3×2=**12** | 🟠 P1 |
| Server disconnect on大Session优先context判断 | 2×3×2=**12** | 🟠 P1 |
| 402决裂依赖AND条件太严格 | 2×3×2=**12** | 🟠 P1 |
| 403 "model access not enabled" → auth误判 | 2×3×2=**12** | 🟠 P1 |
| "forbidden"模式太宽泛 | 2×2×2=**8** | 🟡 P2 |
| 未知错误默认可重试→重试风暴 | 2×2×2=**8** | 🟡 P2 |
| 状态码提取仅5层深 | 2×2×2=**8** | 🟡 P2 |
| 错误消息拼接产生假阳性pattern | 1×2×2=**4** | 🟢 P3 |

**P1核心**：Error Classifier是Hermes可靠性的大脑。4个P1问题都是"特定provider错误被误判→错误处置→额外延迟/资源浪费"。其中402误判最严重：transient rate limit被当作billing exhaustion，导致credential永久rotate而不是等待重试。

### 3.4 Web Tools（10 pitfall条）

| Pitfall | 风险分 | 优先级 |
|---------|--------|--------|
| 大文档并行summarize触发thundering herd | 3×2×2=**12** | 🟠 P1 |
| Firecrawl crawl无timeout | 3×3×1=**9** | 🟠 P1 |
| web_search_tool同步阻塞event loop | 2×3×2=**12** | 🟠 P1 |
| Tavily 60s硬编码timeout | 2×2×1=**4** | 🟢 P3 |
| Firecrawl scrape 60s硬编码timeout | 2×2×1=**4** | 🟢 P3 |
| Summarizer LLM仅2次重试 | 2×2×2=**8** | 🟡 P2 |
| Firecrawl client缓存stale token | 2×2×2=**8** | 🟡 P2 |
| Parallel client无invalidation | 2×2×2=**8** | 🟡 P2 |
| 203-240 Empty results路径逻辑混乱 | 1×1×1=**1** | 🟢 P3 |
| Empty results错误字符串做base64清理 | 1×1×1=**1** | 🟢 P3 |

**P1核心**：`web_search_tool`同步阻塞event loop是最严重的——当它调用Tavily(60s timeout)时，整个异步事件循环被阻塞，heartbeat/interrupt检查全部暂停。对于cron job这种长时间无反馈场景，用户可能等待60秒无任何响应。

### 3.5 Context Compressor（~20+ pitfall条，来自context_compressor_pitfalls.md）

| Pitfall | 风险分 | 优先级 |
|---------|--------|--------|
| 迭代摘要无限增长(5次压缩后summary占满context) | 3×3×2=**18** | 🔴 P0 |
| 600s cooldown阻塞所有压缩 | 2×3×2=**12** | 🟠 P1 |
| max_tokens 2x预算→可能超发2倍 | 2×2×1=**8** | 🟡 P2 |
| 粗糙token估算(JSON比自然语言更稀疏) | 2×2×2=**8** | 🟡 P2 |
| 200字符阈值跳过短tool results | 1×1×1=**1** | 🟢 P3 |
| 非字符串content处理dict repr | 1×1×1=**1** | 🟢 P3 |

**P0核心**：迭代摘要无限增长与Swarm2的worker handoff规范直接冲突——Swarm2 handoff要求"handoff markdown must fit in context"，如果Hermes的summary自己就能撑满context window，那worker renewal时无法传递有意义的handoff。

### 3.6 优先级总览

```
🔴 P0 (立即修复):
  - OAuth Refresh Race (CP-1)
  - _seed_from_singletons stale overwrite (CP-2)
  - 迭代摘要无限增长 (CC-P0)

🟠 P1 (近期修复):
  - Error Classifier ×4 (misclassification)
  - Rate Limit: Anthropic/Google header忽略
  - Web Tools: search同步阻塞, crawl无timeout, chunk thundering herd

🟡 P2 (计划修复):
  - Lease counter软上限形同虚设
  - Summarizer LLM 2次重试不足
  - Firecrawl/Parallel client stale token
  - Token估算系统性低估
  - max_tokens 2x超发

🟢 P3 (长期/设计决策):
  - 其他低影响cosmetic问题
  - 时间戳毫秒/秒歧义
  - 空结果路径混乱
```

---

## Phase 4 — Implementation Roadmap

### 4.1 Quick Wins（1-2小时每个）

| 编号 | 修复 | 文件 | 变更规模 |
|------|------|------|----------|
| QW-1 | 429 TTL从3600s改为60s+provider reset_at优先 | `credential_pool.py:192-196` | ~3行 |
| QW-2 | Unknown 4xx errors改为non-retryable | `error_classifier.py:404-406` | ~3行 |
| QW-3 | Tavily/Firecrawl timeout改为config | `web_tools.py:302,1298` | ~4行 |
| QW-4 | web_search_tool加`asyncio.to_thread`包装 | `web_tools.py:1034-1161` | ~10行 |
| QW-5 | Release Lease添加debug log unknown ID | `credential_pool.py:921-928` | ~5行 |

### 4.2 Medium Projects（半天-1天每个）

| 编号 | 项目 | 文件 | 说明 |
|------|------|------|------|
| M-1 | Anthropic/Google rate limit header映射 | `rate_limit_tracker.py:111-120` | 添加header alias映射表 |
| M-2 | 402误判修复：transient vs billing分离 | `error_classifier.py:518-544` | 改为OR条件，增加pattern |
| M-3 | Firecrawl crawl加timeout包装 | `web_tools.py:1679` | `asyncio.wait_for(asyncio.to_thread(...), 120)` |
| M-4 | 并行chunk summarization加Semaphore | `web_tools.py:744` | `sem = asyncio.Semaphore(3)` |
| M-5 | 迭代摘要加hard cap | `context_compressor.py:467` | 存储前截断_previous_summary |

### 4.3 Large Projects（架构级，1周+）

| 编号 | 项目 | 影响 | 说明 |
|------|------|------|------|
| L-1 | Worker生命周期状态机 | Swarm2对齐 | 实现runtime.json + 状态机 + auto-renew |
| L-2 | Handoff契约实现 | Swarm2对齐 | worker renewal时的结构化handoff markdown |
| L-3 | Credential Pool竞态修复 | 高可用性 | 原子性token写入 + 乐观锁 |
| L-4 | swarm.yaml roster | Swarm2对齐 | 持久化worker身份 + 角色 + 技能 |
| L-5 | Greenlight Gate | 安全性 | 高风险操作的审批流 |

---

## Appendix A — OpenClaw Swarm2关键Spec索引

| Spec文件 | 行数 | 核心内容 |
|---------|------|---------|
| `docs/swarm/ARCHITECTURE.md` | 310 | Loop架构、checkpoint contract、Greenlight Gate、3 lanes |
| `docs/swarm2-memory-framework-spec.md` | 468 | 3层内存架构、文件契约、handoff路径 |
| `docs/swarm2-worker-lifecycle-compaction-spec.md` | 107 | 状态机、renewal序列、context策略(250k/400k/500k) |
| `docs/swarm2-agent-ide-spec.md` | 513 | Worker IDE环境 |
| `docs/swarm2-autopilot-orchestration-spec.md` | 312 | Autopilot编排、Dispatcher、Orchestrator循环 |

**Context策略**（Soft/Hard limits）：
- Soft limit: 250k tokens → 请求concise checkpoint
- Handoff limit: 400k tokens → 必须写full handoff
- Hard limit: 500k tokens → 停止接新任务直到renewed

## Appendix B — 已有Pitfall Atlas索引

| Atlas文件 | 条目数 | 覆盖模块 |
|---------|--------|---------|
| `docs/pitfall_atlas.md` | 29条 | credential_pool, rate_limit_tracker, error_classifier, web_tools |
| `agent/context_compressor_pitfalls.md` | ~20条 | context_compressor, context_engine |

---

*Report generated by Hermes Agent autonomous research pipeline — Phase 1 (reconnaissance) + Phase 2 (arch analysis) + Phase 3 (prioritization) complete.*
