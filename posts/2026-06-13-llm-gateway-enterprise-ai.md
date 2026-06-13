---
title: "The LLM Gateway: How to Route AI Requests Intelligently and Cut Costs by 60–80%"
date: 2026-06-13
tags: Enterprise AI, LLM Gateway, NVIDIA NIM, NVAIE, Anthropic, Cost Optimization, AI Architecture
excerpt: Every enterprise AI request doesn't need the same model. A routing decision costs microseconds on a 7B model. A deep reasoning chain needs your most capable. The LLM Gateway is the component that enforces this — and the difference in monthly cost is not 10%. It's 60–80%.
image: assets/images/blog/covers/AI-Infrastructure.png
---

# The LLM Gateway: How to Route AI Requests Intelligently and Cut Costs by 60–80%

Early in building Getraind, every AI request went to the same model. I didn't think much about it — I just picked the most capable model available, set a single API key, and wired it into the platform. It worked. It was also expensive, slow for simple tasks, and fragile — one provider outage meant the entire platform was down.

That's the naive approach, and almost every enterprise I've worked with starts there.

The LLM Gateway was the first component I rebuilt from scratch once I understood what was actually happening. It's now the most impactful single piece of the architecture — not because of what it does during a complex reasoning task, but because of what it *doesn't* do for the 80% of requests that don't need a reasoning model at all.

---

## The Core Insight: Not All AI Work Is Equal

Think about what actually happens when a user sends a message to an enterprise AI agent:

1. A **router** reads the message and classifies it into one of a handful of categories — maybe five options. It needs to output one word. One correct word, deterministically, every time.
2. A **planner** breaks that categorized task into a sequence of steps. It needs structured output, low creativity, reliable formatting.
3. An **executor** runs one of those steps — perhaps calling an API or searching documents. It needs moderate intelligence and tool-use capability.
4. A **thinker** — only triggered for the hard problems — reasons through a complex multi-step problem, writes code, builds an argument. This needs the best model available.
5. A **validator** checks whether the output meets constraints. Binary yes/no. Deterministic.

If you send all five of those roles to Claude Sonnet or GPT-4, you're using a forklift to carry a coffee mug for roles 1, 2, and 5. And you're paying Sonnet prices for every single one.

The LLM Gateway solves this by making the model assignment part of the architecture, not an afterthought.

---

## How the Gateway Works

Every call to the LLM in Getraind carries a **role**. The role determines the model, the temperature, and the token budget — all baked in at build time so there's no per-request misconfiguration.

```
Incoming request (role=ROUTER)
       │
       ▼
┌──────────────────────────────────────────────────────────┐
│  LLM Gateway                                             │
│                                                          │
│  1. Look up role → model chain + settings                │
│  2. Try Provider 1 (primary for this role)               │
│     └─ Retry once on failure (exponential jitter)        │
│  3. On provider failure → try Provider 2 (fallback)      │
│  4. On all providers exhausted → raise GatewayError      │
│                                                          │
└──────────────────────────────────────────────────────────┘
       │
       ▼
  Response (content, tokens, model, provider)
```

The caller never selects a model. It selects a role. The gateway handles everything else.

---

## The Full Role Table

| Role | Temperature | Max Tokens | Rationale |
|---|---|---|---|
| `ROUTER` | 0.0 | 32 | One keyword. Physically cannot write more. |
| `PLANNER` | 0.2 | 4096 | Low creativity, structured step output |
| `EXECUTOR` | 0.3 | 4096 | Tool use and action — slight creativity needed |
| `THINKER` | 0.4 | 8192 | Deep reasoning, ReAct, code — full budget |
| `EMBEDDER` | 0.0 | 256 | Deterministic vector generation |
| `RETRIEVER` | 0.0 | 512 | Deterministic query reformulation for RAG |
| `VALIDATOR` | 0.0 | 128 | Binary approve/reject — must be deterministic |
| `EVALUATOR` | 0.1 | 2048 | Slight warmth for nuanced rubric scoring |
| `MEMORY_MANAGER` | 0.0 | 1024 | Deterministic key extraction |
| `HITL` | 0.1 | 512 | Human-readable summary before interrupt |
| `CACHE_MANAGER` | 0.0 | 64 | Deterministic cache key generation |

The temperature and token limits are enforced in the gateway chain itself — not passed per-request. This prevents a misconfigured caller from accidentally making a ROUTER produce a 4096-token essay.

---

## Provider Strategy: NVIDIA NIM + Anthropic

In Getraind (running on modest hardware with no on-prem GPUs), I use two providers:

- **NVIDIA NIM cloud API** — primary for most roles. 100+ models available, generous rate limits at `build.nvidia.com`, fast inference.
- **Anthropic Claude API** — primary for THINKER, fallback for everything else.

| Role group | Provider 1 (primary) | Provider 2 (fallback) |
|---|---|---|
| **THINKER** | Anthropic Sonnet | NVIDIA (reasoning models) |
| **All other roles** | NVIDIA (role-specific models) | Anthropic (role-specific fallback) |
| **EMBEDDER** | NVIDIA only | *(Anthropic has no embedding API)* |

**Why THINKER reverses the order?** Deep reasoning, code generation, and ReAct chains are where model quality has the biggest impact on output correctness. Claude Sonnet consistently outperforms for these tasks. For routing, validation, and planning — NVIDIA's smaller models are more than sufficient and dramatically faster.

### The NVIDIA Anthropic Fallback Per Role

The Anthropic fallback isn't one-size-fits-all. Each role gets a model matched to its complexity:

```python
ANTHROPIC_ROLE_MODELS = {
    # Roles that benefit from Sonnet quality as fallback
    "thinker":   ["claude-sonnet-4-5", "claude-haiku-4-5"],
    "planner":   ["claude-sonnet-4-5", "claude-haiku-4-5"],
    "evaluator": ["claude-sonnet-4-5", "claude-haiku-4-5"],
    "hitl":      ["claude-sonnet-4-5", "claude-haiku-4-5"],

    # Roles where Haiku speed is the right tradeoff
    "executor":  ["claude-haiku-4-5", "claude-sonnet-4-5"],
    "retriever": ["claude-haiku-4-5", "claude-sonnet-4-5"],

    # Deterministic roles — Haiku only, no escalation needed
    "router":         ["claude-haiku-4-5"],
    "validator":      ["claude-haiku-4-5"],
    "memory_manager": ["claude-haiku-4-5"],
    "cache_manager":  ["claude-haiku-4-5"],
}
```

---

## The Enterprise Answer: NVIDIA AI Enterprise (NVAIE)

Here's what I use in Getraind vs. what I recommend for enterprise customers — and why they're different.

Getraind calls `api.nvidia.com` — the NVIDIA NIM cloud API. Prompts leave my server, go to NVIDIA's infrastructure, responses come back. This is pragmatic for my setup (no H100s) but not acceptable for a bank, a hospital, or a government agency.

The enterprise answer is **NVIDIA AI Enterprise (NVAIE)**: a software subscription that includes the exact same NIM model containers, deployed inside your own data center, running on your own Cisco UCS H100 hardware.

```
Getraind (API key approach):
  Agent → LLM Gateway → api.nvidia.com → Model inference → Response
                              ↑
                        Data leaves network

Enterprise (NVAIE approach):
  Agent → LLM Gateway → nim.internal.corp:8000 → Model inference → Response
                              ↑
                        Never leaves your building
```

**The gateway code is identical.** One configuration value changes — the base URL for the NVIDIA provider. The role table, the fallback logic, the retry behavior — all the same. This is the design principle: abstract the provider so the application layer doesn't care whether inference is local or cloud.

When I work with enterprise customers deploying on Cisco UCS clusters, this is the path I recommend:

1. Start with NVIDIA NIM cloud APIs for development and proof-of-concept
2. Size your GPU infrastructure using the math from the [GPU hardware post](/posts/2026-06-14-gpu-hardware-model-selection.html)
3. Get an NVAIE subscription and stand up on-prem NIM containers
4. Change one config value in the LLM Gateway
5. Prompts never leave your network again

---

## Retry Behavior and Failover

Each provider gets one automatic retry with exponential jitter before the gateway moves to the next provider in the chain:

```python
def _retried(runnable, attempts=2):
    return runnable.with_retry(
        stop_after_attempt      = attempts,
        wait_exponential_jitter = True,   # ~1s wait, avoids thundering herd
    )
```

`attempts=2` means one retry per provider. If NVIDIA fails twice, the gateway moves to Anthropic. If Anthropic also fails twice, `GatewayError` is raised with a clear message:

```python
raise GatewayError(
    f"All providers failed for role '{role.value}'. "
    "Check NVIDIA / Anthropic API keys and rate limits."
)
```

In production at Getraind, provider failures are rare but real. NVIDIA NIM cloud has occasional rate limit windows during peak hours. Anthropic has had brief API outages. The failover is automatic — users see no interruption, agents get responses.

---

## What This Looks Like in Practice

In production at Getraind, I track which role each request hits and which provider serves it. The breakdown is roughly:

- ~45% of requests hit `ROUTER` or `VALIDATOR` — tiny models, near-zero cost, sub-100ms latency
- ~30% hit `EXECUTOR` or `RETRIEVER` — mid-size models, moderate cost
- ~15% hit `PLANNER` — structured output, mid-size models
- ~10% hit `THINKER` — full reasoning models, the expensive tier

Without the gateway, 100% of those requests would go to the most capable (and most expensive) model. With it, only 10% do. The cost difference is real — not a percentage point or two, but an order of magnitude on the most common request types.

The latency improvement for simple tasks is equally significant. A ROUTER call on a phi-4-mini model returns in under 200ms. The same classification sent to Claude Sonnet would take 1–3 seconds and generate far more tokens than needed. For an agent that routes every user message, that latency compounds across every interaction.

---

## The Public API

The gateway exposes three methods to the rest of the platform:

```python
# Single response
response = await gateway.ainvoke(messages, role=LLMRole.THINKER)
# response.content, response.input_tokens, response.output_tokens,
# response.model, response.provider

# Token streaming (for WebSocket real-time display)
async for chunk in gateway.astream(messages, role=LLMRole.EXECUTOR):
    await ws.send_json({"type": "token", "content": chunk})

# Convenience shortcut for routing (returns one stripped lowercase word)
label = await gateway.route(messages)

# Embeddings for vector search and semantic cache
vectors = await gateway.embed("query text", input_type="query")
vectors = await gateway.embed(["doc1", "doc2"], input_type="passage")
```

Every node in every agent goes through these four methods. The gateway is the only place in the codebase that knows which model is serving which request.

> **Open-source reference implementation**: The full LLM Gateway pattern described in this post — config-driven backend selection, role-based routing, fallback logic, embeddings, and streaming — is implemented in **[DCAF (DataCenter AI Frameworks)](https://github.com/maheshpabba/genaistack)**. DCAF supports four inference backends behind the same FastAPI interface: Ollama (local), OpenVINO INT4/INT8 (CPU-only), llama.cpp/GGUF over NATS, and NVIDIA NIM on Cisco UCS X AI Pod. Switching backends is a single config value — no code changes.

---

## What Comes After This Post

The LLM Gateway is the economic engine of the platform — it's what makes enterprise-scale AI affordable without sacrificing capability where it matters. But it's only valuable if the hardware underneath it can serve models fast enough to meet latency SLAs.

The next post covers the hardware side: how to size GPUs, select models for a tiered strategy, and design for distributed inference when models are too large for a single node.

*Next: [GPU Hardware and Model Selection: The Math Nobody Shows You](/posts/2026-06-14-gpu-hardware-model-selection.html)*
