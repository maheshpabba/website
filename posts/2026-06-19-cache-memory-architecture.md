---
title: "Cache and Memory Architecture: How Enterprise AI Gets Faster and Smarter Over Time"
date: 2026-06-19
tags: Enterprise AI, Cache, Memory, Episodic Memory, Semantic Cache, LangGraph, MongoDB, Getraind
excerpt: Cache answers "have I computed this before?" Memory answers "what do I know that helps me now?" They're different systems with different lifecycles — and combining them is what makes an enterprise AI platform dramatically more capable and economical than a stateless chatbot.
image: assets/images/blog/covers/AI-Infrastructure.png
---

# Cache and Memory Architecture: How Enterprise AI Gets Faster and Smarter Over Time

Most enterprise AI deployments treat every request as if it's the first. No memory of past interactions. No cached results from identical or similar queries. Every prompt goes to the model cold, every tool call executes fresh, every session starts from zero.

This is both expensive and limiting. Expensive because you're paying for LLM inference on questions you've already answered. Limiting because an AI that forgets everything between sessions can never become more useful over time.

The cache and memory architecture in Getraind addresses both problems — with systems that serve different purposes and work at different time horizons.

---

## The Core Distinction

It's easy to conflate cache and memory because both store data to avoid repeating work. They're fundamentally different:

| | Cache | Memory |
|---|---|---|
| **Purpose** | Avoid redundant computation | Accumulate knowledge over time |
| **Key question** | "Have I computed this exact thing before?" | "What do I know that will help me now?" |
| **Lifecycle** | Discard when stale or full | Retain and grow indefinitely |
| **Scope** | Per-request, per-node | Per-agent, per-user, enterprise-wide |
| **Written by** | Gateway and node wrappers automatically | Node functions and agent tool calls |
| **Read by** | Gateway and node wrappers transparently | Agent nodes at decision time |

Cache is transparent — callers don't know whether a response came from cache or a live LLM call. Memory is explicit — agents retrieve and reason about it consciously.

---

## The Cache System: Three Layers

Three cache layers sit in front of expensive operations. They stack: a request that misses all three reaches the LLM or tool. A request that hits any layer returns immediately.

### Layer 1: ExactCache

**What it stores**: LLM responses, keyed on SHA-256 of the exact prompt messages + model name.

**When it hits**: Byte-for-byte identical system prompt + conversation history + user message + same model. Common for:
- Repeated router calls — a user who says "help me" consistently routes to the same keyword
- Re-run of identical agent build inputs
- Cron-triggered agents that run the same analysis repeatedly

**Document schema (MongoDB `ExactCache` collection)**:
```json
{
  "prompt_hash":  "abc123…",
  "llm_string":   "claude-sonnet-4-5",
  "return_val":   "{\"content\": \"execute\", \"usage\": {…}}",
  "hits":         42,
  "created_at":   "2026-06-11T09:00:00Z",
  "last_hit_at":  "2026-06-12T14:30:00Z"
}
```

**TTL**: None. Entries persist indefinitely. Use `db.ExactCache.deleteMany({})` to flush when a prompt template changes significantly.

---

### Layer 2: SemanticCache

**What it stores**: LLM responses keyed on the *semantic meaning* of the prompt, not the literal text.

**When it hits**: Prompt embedding cosine similarity ≥ 0.92 against a cached prompt.

```
"What is the capital of France?"
"Tell me the capital of France"
"France's capital city is?"
→ Same cached answer, one LLM call
```

**How it works**:
1. Incoming prompt → embed with NVIDIA `nv-embedqa-e5-v5` (1024 dimensions)
2. `$vectorSearch` against `SemanticCache` collection (MongoDB Atlas Vector Search or MongoDB 7 Enterprise)
3. If top match ≥ 0.92 cosine similarity → return cached response
4. If miss → call LLM → embed result prompt → store both in SemanticCache

**Similarity threshold at 0.92**: This is deliberately conservative. At 0.85 you get false positives — semantically adjacent but meaningfully different prompts returning wrong cached responses. At 0.92, genuine paraphrases hit the cache while distinct questions miss. Tune lower only if your prompt space is highly repetitive.

**Fallback**: On MongoDB Community (no Atlas Search), SemanticCache silently degrades to a miss — the request reaches the LLM. No error, just no cache benefit.

---

### Layer 3: NodeOutputCache

**What it stores**: The *output of expensive LangGraph nodes* — web searches, SAP queries, document fetches, code execution results.

**Key structure**: `(agent_id, node_id, SHA-256(inputs))`

**Why this is different from LLM caching**: LLM caches answer "have I asked this question before?" Node output cache answers "have I fetched this data before?" For an SAP query that retrieves open purchase orders for a specific vendor, the cache means the second user who asks the same question in the same 30-minute window doesn't trigger another SAP RFC call.

**TTL by node type**:

| Node Type | TTL | Rationale |
|---|---|---|
| `fetch_docs` (RAG retrieval) | 1 hour | Documents change infrequently |
| `web_search` | 15 minutes | Web results go stale quickly |
| `sap_query` | 30 minutes | SAP data changes during business hours |
| `code_execution` | 24 hours | Deterministic; safe to cache longer |
| `human_approval` | Never | Always requires fresh human decision |

**MongoDB TTL index**: An index on `expires_at` ensures MongoDB deletes stale entries automatically — no manual cleanup job needed.

---

## Cache Performance in Practice

In production at Getraind, across typical enterprise query patterns:

- **ExactCache** hits 15–25% of router calls (users repeat similar intent patterns)
- **SemanticCache** catches another 20–35% of LLM calls (paraphrased queries, repeated analysis tasks)
- **NodeOutputCache** absorbs 30–50% of tool calls (shared lookups across users)

Combined, 40–65% of requests never reach a live LLM or external system. The latency improvement is proportional — a cache hit returns in milliseconds vs. 500ms–3s for a live inference call.

---

## The Memory System: Four Layers

Where cache discards data when it's no longer needed, memory accumulates knowledge permanently. The four memory layers serve different time horizons and scopes.

```
One agent turn
     │
     ├──▶ EpisodicMemory  ← "this event happened"       (automatic, every turn)
     │
     ├──▶ SemanticMemory  ← "this fact is true"         (via memory_remember tool)
     │
     ├──▶ ProceduralMemory ← "this pattern works"       (auto after agent builds)
     │
     └──▶ WorkingMemory   ← "what I'm working on now"   (RAM + MongoDB checkpoint)
```

### Episodic Memory

**What it stores**: A record of what happened in past conversations — which user, which agent, which message, which response, when.

**Scope**: Per-user, per-agent-slug.

**Written by**: Every `llm_agent` node, automatically after each LLM call (background task, non-blocking).

**Retrieved by**: Memory injection at the start of every LLM node call.

**Practical effect**: The agent remembers that last Tuesday you were debugging the SAP connector for vendor 1000 and that you eventually found the issue was a missing EKKO field. When you open a new conversation about vendor 1000, that context is already there.

---

### Semantic Memory

**What it stores**: Facts — claims that are durably true about the user, their domain, or their organization. Stored as vector embeddings for similarity-based retrieval.

**Written by**: Explicit `memory_remember` tool calls from agents (when the agent decides something is worth retaining as a fact), or background extraction by the `MEMORY_MANAGER` role.

**Retrieved by**: Vector search at the start of each LLM node call — top-k most relevant facts for this query are injected.

**Examples**:
- "mpabba prefers responses with bullet points over long prose"
- "The Getraind procurement policy requires VP approval for purchases over $50K"
- "The SAP system for EU operations uses client 300, not 100"

These facts persist indefinitely. They make every subsequent interaction more accurate without the user having to repeat context.

---

### Procedural Memory

**What it stores**: How things get done — workflows, patterns, sequences of steps that have been validated to work.

**Written by**: Automatically after the Agent Builder generates a successful primitive. The methodology steps, design decisions, and patterns used are summarized and stored.

**Retrieved by**: The Agent Builder thinker node, when designing a new agent — it searches procedural memory for relevant patterns from previous successful builds.

**Practical effect**: The Agent Builder gets better at its job the more agents it designs. Patterns that worked for procurement agents inform the next procurement-adjacent agent design. Anti-patterns from failed designs are noted and avoided.

---

### Working Memory

**What it stores**: The current in-progress state of an active task — not completed yet, not ready for long-term memory.

**Implementation**: The LangGraph checkpoint state in MongoDB (`langgraph_checkpoints`), plus an explicit working memory collection for designer-mode state.

**Session restoration**: When the Agent Builder Studio user returns after closing their browser mid-session, working memory provides their current stage, confirmed requirements, and accumulated design notes. The conversation resumes exactly where it left off.

---

## Memory Injection: How Context Gets Into Prompts

Before every LLM call in an `llm_agent` node, the memory store assembles relevant context from all applicable memory layers and prepends it to the system prompt:

```python
memory_context = await memory_store.inject_into_context(
    username   = state["username"],
    agent_slug = state["agent_slug"],
    query      = last_human_message,
)

full_prompt = f"""### Relevant Memory
{memory_context}

---

{original_node_prompt}"""
```

The memory context typically includes:
- 3–5 most recent episodic events (what happened in recent conversations)
- Top-3 most semantically similar facts from semantic memory
- Relevant procedural patterns (for builder agents)

This injection is what makes agents feel like colleagues. The agent doesn't just know what you're asking now — it knows who you are, what you've been working on, and how things work in your organization.

---

## LangGraph Checkpoints: Not Cache, But Critical Infrastructure

One more piece worth understanding: the LangGraph checkpointer (`MongoDBSaver`) is not part of the cache or memory systems, but it's adjacent infrastructure that enables two critical features.

**Crash recovery**: Every time a LangGraph node completes, its output is written to MongoDB. If the GTAIF process crashes mid-agent-execution, the next request resumes from the last completed node — not from the beginning.

**HITL approval waits**: When a `human_approval` node suspends the graph with `interrupt()`, the full state is checkpointed. The graph can be suspended for minutes or hours while the user reviews and approves an action. When they respond, the graph resumes from exactly where it paused.

Without the checkpointer, neither of these is possible. With it, the platform is resilient to both process failures and human latency.

---

## The Combined Effect

Cache + memory together create an AI platform that improves with use:

- **Day 1**: Every request goes to the LLM. Agents have no knowledge of the organization or the user.
- **Week 1**: Common queries are cached. Agents know the basics about each user's preferences and role.
- **Month 1**: Cache hit rates are in the 40–65% range. Agents have accumulated substantial domain knowledge. Response quality for organizational-specific questions is significantly higher than a generic LLM.
- **Year 1**: Episodic memory spans months of interactions. Procedural memory captures the organization's established agent patterns. The platform compounds — each interaction makes it more useful.

This is what separates an enterprise AI platform from a chatbot with a good API key. The chatbot is the same on day 365 as it was on day 1. A platform with cache and memory gets better every day it runs.

*Next: [Redis in Enterprise AI: Rate Limiting, Token Security, and Session Management](/posts/2026-06-20-redis-enterprise-ai.html)*
