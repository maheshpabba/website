# Cache & Memory Architecture

GTAIF uses two conceptually separate systems — **Cache** and **Memory** — that are easy to confuse but serve entirely different purposes.

---

## The Core Distinction

| | Cache | Memory |
|---|---|---|
| **Purpose** | Avoid redundant computation | Accumulate knowledge over time |
| **Key question** | "Have I computed this exact thing before?" | "What do I know about the world / this agent?" |
| **Data lifecycle** | Discard when stale or full | Retain and grow indefinitely |
| **Scope** | Per-request / per-node | Per-agent, per-user, or enterprise-wide |
| **Written by** | Gateway & node wrappers automatically | Node functions + agent tool calls |
| **Read by** | Gateway & node wrappers transparently | Agent graph nodes at decision time |

---

## CACHE SYSTEM

Three cache layers sit in front of expensive operations. They are completely transparent to agent logic — hits are returned before the LLM or tool is ever called.

```
Incoming LLM call
       │
       ▼
┌──────────────────┐  hit   ┌─────────────┐
│   ExactCache     │───────▶│  Return     │
│  (SHA-256 match) │        │  cached     │
└────────┬─────────┘        │  response   │
         │ miss             └─────────────┘
         ▼
┌──────────────────┐  hit ≥ 0.92
│  SemanticCache   │───────▶ (same as above)
│ (vector cosine)  │
└────────┬─────────┘
         │ miss
         ▼
┌──────────────────┐
│   LLM Provider   │  (Anthropic / NVIDIA NIM)
│  (real API call) │
└──────────────────┘
         │
         ▼
    Store in both caches for next time


Incoming node execution (fetch_docs, web_search, SAP query, …)
       │
       ▼
┌──────────────────┐  hit + not expired
│  NodeOutputCache │───────▶  Return cached output
│ (agent+node+hash)│
└────────┬─────────┘
         │ miss / expired
         ▼
    Execute node, store result with TTL
```

---

### Cache Layer 1 — ExactCache

**Collection:** `ExactCache`  
**File:** `GTAIF/db/exact_cache.py`

Caches LLM responses keyed on a SHA-256 hash of the exact prompt messages + model name. Two calls with byte-for-byte identical messages return the same response with zero LLM cost.

**Document schema:**
```json
{
  "prompt_hash":  "abc123…",
  "llm_string":   "claude-sonnet-4-5",
  "return_val":   "{\"content\": \"…\", \"usage\": {…}}",
  "hits":         42,
  "created_at":   "2026-06-11T09:00:00Z",
  "last_hit_at":  "2026-06-11T12:30:00Z"
}
```

**Key index:** `{ prompt_hash: 1, llm_string: 1 }` (unique compound)

**TTL:** None — entries persist until manually cleared. Use `db.ExactCache.deleteMany({})` to flush.

**When it hits:** Identical system prompt + identical conversation history + identical user message + same model. Common for:
- Repeated router calls (classify intent → same routing keyword)
- Re-run of the same agent build on identical inputs

---

### Cache Layer 2 — SemanticCache

**Collection:** `SemanticCache`  
**File:** `GTAIF/db/semantic_cache.py`  
**Requires:** MongoDB Atlas M10+ or MongoDB 7 Enterprise ($vectorSearch)

Embeds the incoming prompt with `nomic-embed-text` (768 dimensions, via NATS→Ollama) and runs `$vectorSearch` against all previously cached prompt embeddings. If cosine similarity ≥ **0.92**, the cached response is returned.

> "What is the capital of France?" and "Tell me the capital of France" → same cached answer, one LLM call.

**Document schema:**
```json
{
  "embedding":   [0.123, -0.456, …],
  "prompt":      "What is the capital of France?",
  "llm_string":  "claude-sonnet-4-5",
  "return_val":  "{…}",
  "hits":        7,
  "created_at":  "2026-06-11T09:00:00Z"
}
```

**Atlas Vector Search index:** `semantic_cache_vector_index`  
```json
{
  "path": "embedding",
  "numDimensions": 768,
  "similarity": "cosine",
  "filters": ["llm_string"]
}
```

**Fallback:** On community MongoDB (no Atlas Search), SemanticCache silently falls through to the LLM — no error, just no hit.

**Score threshold:** 0.92 (configurable). Lower = more aggressive caching, higher = stricter matching.

---

### Cache Layer 3 — NodeOutputCache

**Collection:** `NodeOutputCache`  
**File:** `GTAIF/db/node_cache.py`

Cross-run cache for expensive LangGraph node outputs — fetch_docs, web_search, SAP queries, etc. Unlike `langgraph_writes` (which is scoped to one run), NodeOutputCache spans runs: if user A's agent called `fetch_docs` with the same document IDs two hours ago, user B's agent skips the Mongo/SAP roundtrip.

**Key:** `(agent_id, node_id, SHA-256(inputs))`

**Document schema:**
```json
{
  "node_id":      "fetch_docs",
  "agent_id":     "sap-monitor-bot",
  "inputs_hash":  "def789…",
  "output":       { "chunks": […] },
  "hits":         3,
  "created_at":   "2026-06-11T09:00:00Z",
  "expires_at":   "2026-06-11T10:00:00Z"
}
```

**TTL index:** MongoDB TTL index on `expires_at` — MongoDB deletes documents automatically when the timestamp passes.

**Recommended TTLs by node type:**

| Node type | TTL | Rationale |
|---|---|---|
| `fetch_docs` (RAG) | 1 hour | Documents change infrequently |
| `web_search` | 15 minutes | Web results go stale quickly |
| `sap_query` | 30 minutes | SAP data changes during the business day |
| `code_execution` | 24 hours | Deterministic; safe to cache longer |
| `llm_call` | — | Use ExactCache / SemanticCache instead |
| `human_approval` | **Never** | Always requires a human |

---

### LangGraph Checkpointer (not cache, but checkpoint)

**Collections:** `langgraph_checkpoints`, `langgraph_writes`  
**File:** `GTAIF/db/checkpointer.py`  
**Managed by:** LangGraph `MongoDBSaver` (built-in)

These are not a cache — they are LangGraph's internal state persistence mechanism. Every time a LangGraph node completes, `MongoDBSaver` writes the full `AgentState` to `langgraph_checkpoints` and the node's raw output to `langgraph_writes`.

**What this enables:**
- **Crash recovery:** Resume from the last completed node after a process crash
- **HITL approval waits:** Graph is suspended at `interrupt()` with state checkpointed; resumes when the user approves via WebSocket
- **Session resumption:** User closes the browser, returns tomorrow — `thread_id` (= `session_id`) identifies the prior state

**Thread ID:** In GTAIF, `thread_id` maps to `session_id` which is `{username}:draft:{slug}` for the designer agent.

---

## MEMORY SYSTEM

Memory is about accumulation of knowledge across time. Where cache answers "did I compute this before?", memory answers "what do I know that will help me now?". Memory grows with every agent interaction and is shared enterprise-wide.

```
One agent turn
       │
       ├──▶ EpisodicMemory  ← "this event happened"   (automatic, every turn)
       │
       ├──▶ SemanticMemory  ← "this fact is true"     (via memory_remember tool)
       │
       ├──▶ ProceduralMemory ← "this pattern works"   (auto after builds)
       │
       └──▶ WorkingMemory   ← "what I'm doing now"    (RAM + checkpoints)
```

---

### Memory Layer 1 — Working Memory

**Storage:** RAM (`AgentState` dict) + `langgraph_checkpoints` (MongoDB) + `WorkingMemorySnapshots` (MongoDB)  
**Scope:** Single agent × single session

Working memory is everything the agent holds in mind during a conversation. It lives in the `AgentState` TypedDict in RAM and is checkpointed to MongoDB by `MongoDBSaver` after every node.

Key fields in `AgentState`:

| Field | Type | Purpose |
|---|---|---|
| `messages` | `list[BaseMessage]` | Full conversation (Human + AI messages) |
| `scratchpad` | `list[str]` | Thinker node intermediate reasoning |
| `notes` | `list[str]` | Architect persistent notes across turns |
| `reasoning_steps` | `list[str]` | ReAct reasoning chain |
| `tool_results` | `list[dict]` | Portal task / tool call outputs |
| `context` | `list[str]` | RAG chunks retrieved from Documents |
| `route` | `str` | Current routing decision |
| `builder_stage` | `str` | Designer: `requirements` / `design` / `code` / … |
| `plan` | `list[str]` | Agent builder plan steps |

**WorkingMemorySnapshots collection:** When the builder progresses through stages, key state fields (notes, stage, plan, current_step) are snapshotted here. This is how "resume session" works when a user closes the browser mid-build — the snapshot is loaded on the next WebSocket connect, and the full `AgentState` is reconstructed from `langgraph_checkpoints`.

**WorkingMemorySnapshots schema:**
```json
{
  "username":    "alice",
  "agent_slug":  "sap-monitor-bot",
  "notes":       ["requirement 1", "requirement 2"],
  "builder_stage": "design",
  "plan":        ["step 1", "step 2"],
  "captured_at": "2026-06-11T09:00:00Z"
}
```

---

### Memory Layer 2 — Episodic Memory

**Collection:** `EpisodicMemory`  
**File:** `GTAIF/db/memory.py` → `EpisodicMemoryStore`  
**Scope:** Enterprise-wide  
**Vector index:** `episodic_memory_vector_index` (filters: `agent_slug`, `username`, `event_type`)

A timestamped log of every significant event across all agents and users. Every LLM turn, tool call, agent build, evaluation, and explicit discovery is written here automatically — no agent action required.

**Event types:** `agent_turn`, `tool_call`, `agent_built`, `discovery`, `evaluation`, `reasoning`

**Document schema:**
```json
{
  "episode_id":    "uuid",
  "event_type":    "agent_turn",
  "agent_slug":    "sap-monitor-bot",
  "username":      "alice",
  "session_id":    "alice:draft:sap-monitor-bot",
  "builder_stage": "requirements",
  "timestamp":     "2026-06-11T09:15:00Z",
  "summary":       "User asked agent to monitor SAP RFC connections",
  "content": {
    "user_message":  "…",
    "ai_response":   "…",
    "tool_name":     null,
    "notes_added":   []
  },
  "embedding":     [0.12, -0.34, …]
}
```

**Retrieval:** Via `$vectorSearch` — agents can recall "what happened last time someone tried this" across all users and sessions.

---

### Memory Layer 3 — Semantic Memory

**Collection:** `SemanticMemory`  
**File:** `GTAIF/db/memory.py` → `SemanticMemoryStore`  
**Scope:** Enterprise-wide  
**Vector index:** `semantic_memory_vector_index` (filters: `scope`, `fact_type`)

A factual knowledge base. When an agent discovers that "vendor 1000 is Siemens AG" or "the production SAP system is at host sap-prd-01", this fact is stored here and immediately available to all other agents — no re-discovery needed.

**Written by:** Agents via the `memory_remember` tool call:
```json
{
  "tool": "memory",
  "operation": "memory.remember",
  "params": {
    "fact": "vendor 1000 is Siemens AG",
    "tags": ["entity", "vendor", "sap"]
  }
}
```

**Document schema:**
```json
{
  "fact_id":    "uuid",
  "fact_type":  "entity",
  "scope":      "enterprise",
  "content":    "vendor 1000 is Siemens AG",
  "tags":       ["entity", "vendor", "sap"],
  "source": {
    "agent_slug":  "sap-monitor-bot",
    "username":    "alice",
    "session_id":  "…"
  },
  "confidence":  1.0,
  "created_at":  "2026-06-11T09:20:00Z",
  "embedding":   [0.12, -0.34, …]
}
```

---

### Memory Layer 4 — Procedural Memory

**Collection:** `ProceduralMemory`  
**File:** `GTAIF/db/memory.py` → `ProceduralMemoryStore`  
**Scope:** Enterprise-wide  
**Vector index:** `procedural_memory_vector_index` (filters: `task_type`, `scope`)

Stores proven workflow patterns extracted automatically after successful agent builds. When the designer agent is building a new SAP agent, it retrieves procedural memories: "SAP agents need `getraind-sap` + `hitl-sap-writes` skills. Always add an HITL node before write operations."

**Written by:** Auto-extracted after every successful agent build by the factory pipeline.

**Document schema:**
```json
{
  "pattern_id":   "uuid",
  "task_type":    "sap_agent_build",
  "scope":        "enterprise",
  "description":  "SAP agent pattern: RFC + HITL",
  "steps":        ["add getraind-sap skill", "add hitl-sap-writes skill", "add HITL node before writes"],
  "skills_used":  ["getraind-sap", "hitl-sap-writes"],
  "success_rate": 0.94,
  "usage_count":  17,
  "created_at":   "2026-06-01T08:00:00Z",
  "embedding":    [0.12, -0.34, …]
}
```

---

## Collection → Purpose Map

This is the authoritative mapping of every MongoDB collection in `gtai_db`:

| Collection | System | Type | Scope | Written by | Read by |
|---|---|---|---|---|---|
| `ExactCache` | **Cache** | Exact LLM cache | Global | Gateway (auto) | Gateway (auto) |
| `SemanticCache` | **Cache** | Semantic LLM cache | Global | Gateway (auto) | Gateway (auto) |
| `NodeOutputCache` | **Cache** | Node output cache | Per-agent | Node wrappers | Node wrappers |
| `langgraph_checkpoints` | **Checkpoint** | Graph state snapshots | Per-session | MongoDBSaver | MongoDBSaver |
| `langgraph_writes` | **Checkpoint** | Pending node writes | Per-session | MongoDBSaver | MongoDBSaver |
| `WorkingMemorySnapshots` | **Memory** | Builder session restore | Per-agent | ws.py on stage change | ws.py on reconnect |
| `EpisodicMemory` | **Memory** | Event log | Enterprise | Node functions (auto) | Retriever + agents |
| `SemanticMemory` | **Memory** | Factual knowledge | Enterprise | `memory_remember` tool | Retriever before turns |
| `ProceduralMemory` | **Memory** | Workflow patterns | Enterprise | Factory after builds | Designer agent |
| `Agents` | **Operational** | Agent runtime state | Per-user | studio.py + ws.py | Hub tab + designer |
| `ChatHistory` | **Operational** | Conversation messages | Per-session | MongoDBChatHistory | History endpoint |
| `Documents` | **Operational** | RAG chunks + embeddings | Per-agent | rag/ingest.py | rag/retriever.py |
| `UserDocuments` | **Operational** | File upload tracking | Per-user | documents router | documents router |
| `Traces` | **Operational** | Execution traces | Per-run | GTAIFCallbackHandler | Studio traces view |
| `MCPConnections` | **Operational** | MCP server registrations | Per-user | studio.py | factory.py |
| `Settings` | **Operational** | User preferences | Per-user | studio.py | ws.py (injected to context) |

---

## Per-Agent Data Flow

Here is how data flows for a single agent across both systems:

```
User types a message
        │
        ▼
  ws.py receives message
        │
        ├──▶ Load WorkingMemory from langgraph_checkpoints (MongoDBSaver)
        │    Restore: messages, notes, builder_stage, plan, …
        │
        ├──▶ Retriever reads SemanticMemory + EpisodicMemory
        │    (inject relevant facts and past events into context)
        │
        ├──▶ Retriever reads Documents (RAG chunks for this agent)
        │
        ▼
  LangGraph graph.astream_events(…)
        │
        ├──▶  router node calls LLM
        │          │
        │          ▼
        │     ExactCache lookup   ──hit──▶  skip LLM call
        │          │ miss
        │          ▼
        │     SemanticCache lookup ─hit──▶  skip LLM call
        │          │ miss
        │          ▼
        │     Anthropic / NVIDIA NIM  ──▶  ExactCache.update + SemanticCache.update
        │
        ├──▶  fetch_docs / web_search node
        │          │
        │          ▼
        │     NodeOutputCache lookup  ─hit──▶  skip node execution
        │          │ miss
        │          ▼
        │     Execute node  ──▶  NodeOutputCache.store (with TTL)
        │
        ├──▶  MongoDBSaver checkpoints every node output
        │          └──▶  langgraph_checkpoints + langgraph_writes
        │
        ▼
  Turn complete
        │
        ├──▶  EpisodicMemory.record(event_type="agent_turn", …)  [auto]
        ├──▶  AgentStore.update_metrics(turn_metrics)             [cumulative]
        └──▶  WorkingMemorySnapshots.save(…)                     [on stage change]
```

---

## Cache Invalidation

| Cache | How to flush |
|---|---|
| `ExactCache` | `db.ExactCache.deleteMany({})` or by `llm_string` |
| `SemanticCache` | `db.SemanticCache.deleteMany({})` |
| `NodeOutputCache` | Automatic via MongoDB TTL index on `expires_at`; or `db.NodeOutputCache.deleteMany({})` |
| `langgraph_checkpoints` | `db.langgraph_checkpoints.deleteMany({ "config.configurable.thread_id": session_id })` |
| `langgraph_writes` | Same as above; LangGraph manages both together |

Memory is **never** automatically invalidated — it is designed to grow over time. Old episodic events naturally drop in relevance as newer events are added (vector search returns top-N by recency + similarity).

---

## Embedding Model

All vector operations (SemanticCache, Documents, EpisodicMemory, SemanticMemory, ProceduralMemory) use the **LLM Gateway's `embed()` method** which calls **NVIDIA NIM**:

- **Primary model:** `nvidia/nv-embedqa-e5-v5` — high-quality query/passage embeddings
- **Fallback models:** `snowflake/arctic-embed-l`, `nvidia/llama-nemotron-embed-1b-v2`
- **Dimensions:** 1024
- **Similarity metric:** cosine
- **Input types:** `"query"` (for search queries), `"passage"` (for documents being stored)

Embedding calls go through `app.state.gateway.embed()` — the same NVIDIA NIM gateway used for all LLM calls. If NVIDIA is unavailable or the API key is missing, `embed()` returns zero vectors and all vector-search features degrade gracefully to no-op (ExactCache and text search still work).

> **Note:** The MongoDB vector search indexes must be configured with `numDimensions: 1024`. If you previously ran with the old Ollama/768-dim setup, you will need to drop and recreate all vector indexes and re-embed existing data.

