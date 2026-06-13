---
title: "The Agent Chat Runtime: From WebSocket to Streamed Response"
date: 2026-06-18
tags: Enterprise AI, Agent Runtime, WebSocket, LangGraph, HMAC, Security, Getraind
excerpt: What actually happens between the moment a user sends a message in AI Studio and the moment the first token appears on screen? Eight steps, HMAC token security, LangGraph graph execution, Redis rate limiting, memory injection, and real-time streaming — all in under 200ms to first token.
image: assets/images/blog/covers/AI-Infrastructure.png
---

# The Agent Chat Runtime: From WebSocket to Streamed Response

When a user sends a message to an agent in Getraind's AI Studio, they see tokens streaming back in real time — starting within 200ms of hitting send. Behind that experience is a pipeline with eight distinct steps, each with its own failure modes and design decisions.

This post traces exactly what happens, end to end — from browser to model and back.

---

## Architecture Overview

```
Browser
  │  HTTPS WebSocket  wss://hostname/ai-ws/{session_id}?token=...
  ▼
nginx  (proxy_pass → getraind-gtaif:5000)
  ▼
GTAIF  /ws/{session_id}
  │
  ├─ validate_ws_token()          ← HMAC signature + Redis single-use check
  ├─ redis.register_ws_session()  ← session registry for future multi-node routing
  ├─ factory.get_or_build()       ← CompiledStateGraph (cached)
  ├─ graph.astream_events()       ← LangGraph execution
  │     └─ LLMGateway.ainvoke()   ← NVIDIA → Anthropic failover chain
  └─ _translate_event() → ws.send_json()  ← token / node_start / node_complete / done
```

---

## Step 1: Token Acquisition

Before opening a WebSocket, the browser fetches a short-lived auth token from the Portal:

```
GET /api/studio/ws-token
Authorization: (session cookie)

← 200 {"token": "mpabba:1749600000:a3f9...", "expires_in": 300}
```

The Portal signs the token with HMAC-SHA256:

```
token = f"{username}:{expiry_unix}:{hmac_sha256(username:expiry, SECRET_KEY)}"
```

Token lifetime: **5 minutes**. The browser must re-fetch before reconnecting after expiry.

---

## Step 2: WebSocket Handshake and Token Validation

```
wss://hostname/ai-ws/{session_id}?token=mpabba%3A1749600000%3Aa3f9...
```

GTAIF validates the token in `validate_ws_token()`:

1. **Parse** — split on `:` to extract username, expiry timestamp, and HMAC signature
2. **Expiry check** — reject if `expiry_unix < now`
3. **HMAC verification** — recompute HMAC with the server's `SECRET_KEY` and compare. Constant-time comparison to prevent timing attacks.
4. **Single-use enforcement** — atomic Redis `SET NX EX` on the HMAC signature:

```python
key    = f"gtaif:ws_token:used:{sig}"
result = await redis.set(key, "1", nx=True, ex=remaining_ttl)
# True  → first use (OK to proceed)
# None  → already consumed (replay attack — reject)
```

If Redis is unavailable, steps 1–3 still run but replay prevention is disabled — the platform degrades gracefully but logs a warning.

On success: WebSocket accepted, `{"type": "connected", "session_id": session_id}` sent to browser.

---

## Step 3: Rate Limiting

Before processing a chat message, Redis enforces a per-user rate limit:

```python
allowed, count = await redis.check_rate_limit(username)
# Default: 30 messages per 60-second window
```

Implementation: a pipeline `INCR` + `TTL` on key `gtaif:rate:chat:{username}`. First message sets a 60-second expiry. If count exceeds the limit, the user receives:

```json
{"type": "error", "message": "Rate limit exceeded. Please wait a moment.", "code": "rate_limited"}
```

The WebSocket connection stays open — rate limit violations don't disconnect the user. Without Redis: rate limiting is disabled.

---

## Step 4: Graph Resolution

```python
factory = app.state.agent_factory
graph = factory.get_or_build(
    slug         = agent_id,
    username     = username,
    checkpointer = app.state.checkpointer,  # MongoDBSaver
)
```

Cache hit is O(1). Cache miss triggers compilation (described in the [Agent Factory post](/posts/2026-06-16-agent-factory-declarative-primitives.html)). The `MongoDBSaver` checkpointer enables crash recovery and HITL (human-in-the-loop) approval waits — the graph can be suspended and resumed across WebSocket reconnections.

---

## Step 5: Initial State Construction

The LangGraph graph executes against a typed `AgentState`. On each new chat message, the initial state is constructed:

```python
initial_state = {
    "messages":        [HumanMessage(content=message)],
    "route":           "",
    "context":         [],      # RAG results injected here by rag_retriever nodes
    "scratchpad":      [],      # thinker intermediate thoughts
    "notes":           [],      # evaluator / architect persistent notes
    "reasoning_steps": [],      # thinker ReAct step trace
    "error":           None,
    "done":            False,
    "session_id":      session_id,
    "username":        username,
    "agent_slug":      agent_id,
}
```

For the **Agent Builder Studio** (designer mode), prior session state is restored from working memory before execution — the architect's stage progress, confirmed requirements, and design notes from previous sessions are injected so the conversation resumes in context.

---

## Step 6: LangGraph Execution and Event Streaming

```python
config = {"configurable": {"thread_id": session_id}}

async for event in graph.astream_events(initial_state, config, version="v2"):
    await _translate_event(ws, event)
```

`astream_events` emits events as each node executes. The `_translate_event` function maps LangGraph events to WebSocket messages:

| LangGraph Event | WebSocket Message |
|---|---|
| `on_chain_start` (node) | `{"type": "node_start", "node_id": "router"}` |
| `on_chain_end` (node) | `{"type": "node_complete", "node_id": "router", "route": "execute"}` |
| `on_chat_model_stream` | `{"type": "token", "content": "The purchase orders..."}` |
| `on_chain_end` (reasoning) | `{"type": "reasoning", "step": "..."}` |
| `on_tool_start` | `{"type": "tool_call", "tool": "web_search", "args": {...}}` |
| `on_chain_end` (tool) | `{"type": "tool_result", "tool": "web_search", "preview": "..."}` |

The browser renders these progressively — node badges show which agent stage is active, reasoning steps appear collapsed in a details element, and tokens stream into the message bubble as they arrive.

---

## Step 7: Inside a Node — What Happens During an LLM Call

When LangGraph executes an `llm_agent` node:

```python
async def _node(state: AgentState) -> dict:
    question = _last_human_message(state)

    # 1. Retrieve and inject memory context
    memory_context = await memory_store.inject_into_context(
        username=state["username"], agent_slug=state["agent_slug"], query=question
    )
    full_prompt = f"### Relevant Memory\n{memory_context}\n\n---\n\n{node_system_prompt}"

    # 2. Build messages
    messages = [SystemMessage(content=full_prompt), *state["messages"]]

    # 3. Call LLM via gateway (handles provider selection and failover)
    response = await gateway.ainvoke(messages, role=LLMRole.EXECUTOR)

    # 4. Extract route from first word (if this node sets_route=True)
    route = response.content.split()[0].lower() if sets_route else ""

    # 5. Record interaction to episodic memory (background, non-blocking)
    asyncio.create_task(_record_episode(state, response))

    return {
        "messages":  [AIMessage(content=response.content)],
        "route":     route,
        "iteration": state["iteration"] + 1,
    }
```

The LLM Gateway call at step 3 handles all provider selection, retry logic, and failover transparently. The node only sees a response object.

---

## Step 8: Portal Tool Calls

When a `thinker` node decides to call an external system, it writes a tool spec to `state["extra"]`:

```json
{
  "tool_call": {
    "tool": "getraind-sap",
    "operation": "RFC_READ_TABLE",
    "params": {
      "table": "EKPO",
      "where": "LIFNR = '0000001000' AND ELIKZ = ''",
      "fields": ["EBELN", "EBELP", "MATNR", "MENGE", "NETPR"]
    }
  }
}
```

The `portal_task` node picks this up, calls the Portal's task API with the spec, and appends the result to `state["tool_results"]` and `state["context"]`. On the next thinker iteration, the result is visible in context as if it were retrieved knowledge.

This indirection — thinker writes a spec, portal_task executes it — means the thinker never has direct access to external systems. All tool execution goes through the Portal's access control and audit layer.

---

## Human-in-the-Loop Approval

For nodes flagged in `interrupt_before` (defined in the primitive's `spec`), LangGraph suspends execution and checkpoints the full state:

```python
# Graph suspends here, state saved to MongoDB
# Browser receives:
{"type": "approval_request", "action": "delete_records", "params": {...}, "approval_id": "uuid"}
```

The user sees an approval UI in AI Studio. When they approve or reject, the WebSocket sends:

```json
{"type": "approval_response", "approval_id": "uuid", "approved": true}
```

GTAIF resumes the graph from the checkpoint with `approved=True` in state. The agent continues. If rejected, the graph routes to a cleanup or explanation node instead.

This pattern ensures destructive operations — writes, deletes, financial transactions — always have a human decision point. The LLM proposes; the human disposes.

---

## What the Browser Sees

From the browser's perspective, the experience is:

1. User sends message → WebSocket sends `{type: "chat", message: "..."}`
2. Server sends `{type: "node_start", node_id: "router"}` — badge appears
3. Server sends `{type: "node_complete", node_id: "router", route: "execute"}` — badge updates
4. Server sends `{type: "node_start", node_id: "executor"}` — next badge
5. Server sends `{type: "token", content: "The"}`, `{type: "token", content: " purchase"}`, ... — tokens stream into bubble
6. Server sends `{type: "node_complete", node_id: "executor"}` — done
7. Server sends `{type: "done"}` — streaming ends

Time from user send to first token: typically 150–300ms on the happy path (cache hit for graph, NVIDIA NIM primary provider available).

---

## Failure Handling

Every failure mode is handled explicitly at the WebSocket layer — no unhandled exceptions reach the user:

| Failure | Response |
|---|---|
| Token invalid / expired | `close(4001)` with reason |
| Agent not found | `{"type": "error", "code": "agent_not_found"}` — connection stays open |
| Rate limit exceeded | `{"type": "error", "code": "rate_limited"}` — connection stays open |
| All LLM providers failed | `{"type": "error", "code": "llm_unavailable", "message": "..."}` |
| Tool call failed | Error written to `state["error"]`, agent routes to error handler node |
| Graph crashes | Exception caught, `{"type": "error", "message": "..."}` sent, connection stays open |

The design principle: **a failure in one conversation should never affect other users' sessions.** Every error is caught at the per-connection handler level. A graph crash for one user is a `send_json` call; it doesn't touch anyone else's WebSocket connection.

---

## Scaling to Multi-Node

Getraind currently runs as a single GTAIF worker behind nginx. The Redis session registry (`gtaif:ws:session:{session_id}` → `username`) is the foundation for future horizontal scaling.

When multiple workers run, a chat message from the browser must reach the worker that holds that session's WebSocket connection. The session registry tells a pub/sub router which worker to target. The infrastructure for this is in place; the multi-worker deployment is the next operational milestone.

For enterprise deployments on Cisco UCS clusters with OpenShift, this horizontal scaling is achievable from day one — the software is ready for it even if Getraind's personal deployment hasn't needed it yet.

*Next: [Cache and Memory Architecture: How Agents Get Faster and Smarter Over Time](/posts/2026-06-19-cache-memory-architecture.html)*
