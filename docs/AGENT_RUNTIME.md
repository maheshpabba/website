# Agent Runtime — End-to-End Chat Flow

This document traces exactly what happens from the moment a user sends a message in AI Studio to the moment the response appears on screen.

---

## Architecture Overview

```
Browser
  │  HTTPS WebSocket  wss://localhost/ai-ws/{session_id}?token=...
  ▼
nginx  (proxy_pass → getraind-gtaif:5000)
  ▼
GTAIF  /ws/{session_id}
  │
  ├─ validate_ws_token()         ← HMAC + Redis single-use check
  ├─ redis.register_ws_session() ← session registry for future pub/sub
  ├─ factory.get_or_build()      ← CompiledStateGraph (cached)
  ├─ graph.astream_events()      ← LangGraph execution
  │     └─ LLMGateway.ainvoke()  ← NVIDIA → Anthropic → Google → OpenAI → NATS/Ollama
  └─ _translate_event() → ws.send_json()  ← token / node_start / node_complete / done
```

---

## Step 1 — Token Acquisition (Browser → Portal)

Before opening a WebSocket, the browser fetches a short-lived auth token:

```
GET /api/studio/ws-token
Authorization: (portal session cookie)

← 200 { "token": "mpabba:1749600000:a3f9...", "expires_in": 300 }
```

The Portal signs the token with its `SECRET_KEY` using HMAC-SHA256:
```
token = f"{username}:{expiry_unix}:{hmac_sha256(username:expiry, SECRET_KEY)}"
```

Token lifetime: **5 minutes**. The browser must reconnect (re-fetch token) if the connection drops after 5 minutes.

---

## Step 2 — WebSocket Handshake (`routers/ws.py`)

```
wss://localhost/ai-ws/{session_id}?token=mpabba%3A1749600000%3Aa3f9...
```

GTAIF receives the connection at `websocket_endpoint()`:

```python
# 1. Get Redis client from app state (may be None if Redis is down)
redis = getattr(websocket.app.state, "redis", None)

# 2. Validate token — HMAC signature + expiry + single-use enforcement
username = await validate_ws_token(token, redis=redis)
# raises ValueError → close(4001) if invalid

# 3. Accept and send "connected" event
await websocket.accept()
await websocket.send_json({"type": "connected", "session_id": session_id})

# 4. Register in Redis for cross-worker routing (future multi-node support)
await redis.register_ws_session(session_id, username)
```

---

## Step 3 — Chat Message Dispatch

The browser sends:
```json
{
  "type":       "chat",
  "session_id": "07269e85-...",
  "agent_id":   "sap-assistant",
  "chat_type":  "aiinaction",
  "message":    "Show me open purchase orders for vendor 1000",
  "agent_config": {}
}
```

For **Designer Studio** (`chat_type: "designer"`), `agent_id` is forced to `"agent-builder-studio"` regardless of what the browser sent.

**Rate limiting** (if Redis is available):
```python
allowed, count = await redis.check_rate_limit(username)
# 30 messages / 60 seconds per user
# If exceeded → {"type": "error", "code": "rate_limited"} and continue (no disconnect)
```

---

## Step 4 — Graph Resolution (`_handle_chat`)

```python
# 1. Get factory and checkpointer from app.state
factory      = app.state.agent_factory
checkpointer = app.state.checkpointer   # MongoDBSaver (langgraph_checkpoints)

# 2. Resolve compiled graph — cache hit is O(1)
graph = factory.get_or_build(
    slug         = agent_id,
    username     = username,
    checkpointer = checkpointer,
)
# ValueError if agent not found → sends {"type": "error"} to browser
```

---

## Step 5 — Initial State Construction

```python
initial_state = {
    "messages":        [HumanMessage(content=message)],
    "route":           "",
    "context":         [],        # RAG / tool results injected here
    "scratchpad":      [],        # thinker intermediate thoughts
    "notes":           [],        # evaluator / architect persistent notes
    "reasoning_steps": [],        # thinker ReAct steps
    "builder_stage":   "requirements" if designer else "",
    "plan":            [],
    "current_step":    0,
    "tool_results":    [],
    "error":           None,
    "approval_pending": False,
    "approval_granted": None,
    "iteration":       0,
    "done":            False,
    "session_id":      session_id,
    "username":        username,
    "agent_slug":      agent_id,
    "extra":           {},
}
```

### Designer Mode: Session Restoration

If `chat_type == "designer"` and the user had a previous session:

```python
snapshot = await memory.working.restore(username, "agent-builder-studio")
if snapshot:
    initial_state["notes"]         = snapshot["notes"]
    initial_state["builder_stage"] = snapshot["builder_stage"]
    initial_state["plan"]          = snapshot["plan"]
    initial_state["current_step"]  = snapshot["current_step"]
    # → sends {"type": "memory_restored", "stage": "design", "note_count": 12}
```

The architect node sees its previous work and resumes mid-conversation.

---

## Step 6 — LangGraph Execution

```python
config = {
    "configurable": {
        "thread_id": session_id,   # MongoDBSaver uses this to checkpoint state
    },
    "callbacks": [hook],           # GTAIFCallbackHandler for metrics
}

async for event in graph.astream_events(initial_state, config, version="v2"):
    await process_stream_event(event, hook)   # metrics: TTFT, tokens, cost
    await _translate_event(ws, event)         # send WebSocket messages
```

### What `astream_events` emits

| LangGraph event | Meaning | WebSocket message sent |
|---|---|---|
| `on_chain_start` (node) | Node began executing | `{"type": "node_start", "node_id": "router"}` |
| `on_chain_end` (node) | Node finished | `{"type": "node_complete", "node_id": "router", "route": "execute"}` |
| `on_chat_model_stream` | Token streamed from LLM | `{"type": "token", "content": "The purchase orders..."}` |
| `on_chain_end` (reasoning) | Thinker step completed | `{"type": "reasoning", "step": "..."}` |
| `on_chain_end` (tool_result) | portal_task result | `{"type": "tool_result", "tool": "getraind-sap", "result_preview": "..."}` |
| `on_tool_start` | LangChain tool invoked | `{"type": "tool_call", "tool": "...", "args": {...}}` |

---

## Step 7 — Inside a Node: `llm_agent`

When LangGraph executes an `llm_agent` node:

```python
async def _node(state: AgentState) -> dict:
    # 1. Pull last user message
    question = _last_human(state)

    # 2. Inject enterprise memory context
    if _memory_store:
        mem_ctx = await _memory_store.inject_into_context(
            username=state["username"], agent_slug=state["agent_slug"], query=question
        )
    # mem_ctx prepended to system prompt as "### Relevant Memory\n{facts}\n\n---\n"

    # 3. Build messages list
    messages = [
        SystemMessage(content=full_system_prompt),   # node prompt + memory
        *state["messages"],                          # conversation history
    ]

    # 4. Call LLM via gateway
    response = await _gateway.ainvoke(messages, role=LLMRole.EXECUTOR)

    # 5. If sets_route=True → first word becomes state["route"]
    route = response.content.split()[0].lower() if sets_route else ""

    # 6. Record episodic memory (background task, non-blocking)
    asyncio.create_task(_record_llm_episode(state, response, agent_slug))

    return {
        "messages":  [AIMessage(content=response.content)],
        "route":     route,
        "iteration": state["iteration"] + 1,
    }
```

---

## Step 8 — Portal Tool Calls (`portal_task` node)

When a `thinker` node decides to call a tool, it writes to `state["extra"]`:

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

The `portal_task` node (`build_portal_task_node`) picks this up:

```python
tool_spec = state["extra"].get("tool_call", {})
tool_name = tool_spec["tool"]
operation = tool_spec["operation"]
params    = tool_spec.get("params", {})

# 1. Look up the tool function
tool_fn = PORTAL_TOOL_REGISTRY[tool_name]  # e.g., getraind_sap_tool

# 2. Call it
result = await tool_fn(operation, params, state)
# → tool_fn calls Portal internal API:
#   POST http://portal:8000/api/jobs/tasks/{operation}/run
#   Header: X-Internal-Key: {PORTAL_INTERNAL_KEY}
#   Body:   {"params": params, "username": username}
```

### Portal Tool Routing (inside `factory/tools.py`)

```python
async def _getraind_portal_tool(tool_slug, operation, params, state):
    url = f"{settings.portal_url}/api/jobs/tasks/{operation}/run"
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            json={"params": params, "username": state["username"]},
            headers={"X-Internal-Key": settings.portal_internal_key},
        ) as resp:
            return await resp.json()
```

The Portal validates the `X-Internal-Key` header on `/api/internal/*` routes, then executes the actual SAP RFC call (or shell command, or HTTP request, or Python script) and returns the result as JSON.

### Filesystem Tool (artifact writing)

For the `filesystem` tool, GTAIF writes **directly** to the primitives volume without going through the Portal:

```python
async def _filesystem_tool(operation, params, state):
    username = state["username"]
    base     = Path(settings.primitives_path) / "Users" / username

    if operation == "write":
        path    = base / params["path"]
        content = params["content"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return {"status": "ok", "path": str(path)}

    elif operation == "read":
        path = base / params["path"]
        return {"content": path.read_text()}

    elif operation == "list":
        path = base / params.get("path", "")
        return {"files": [str(f.relative_to(base)) for f in path.rglob("*.md")]}
```

The Agent Builder Studio uses `filesystem.write` to persist the generated agent `.md` file to `GENAI_PRIMITIVES/Users/{username}/Agents/{slug}.md`. The watchdog observer picks this up and the new agent is immediately available — no restart needed.

---

## Step 9 — RAG Access (`rag_retriever` node)

When an agent topology includes a `rag_retriever` node:

```python
async def _node(state: AgentState) -> dict:
    query = _last_human(state)

    # Embed the query via LLMGateway (NVIDIA → Google → OpenAI → Ollama/nomic-embed-text)
    embedding = await _gateway.embed(query)

    # Vector search in DocumentStore (MongoDB $vectorSearch)
    results = await _doc_store.search(
        embedding = embedding,
        username  = state["username"],
        agent_id  = state["agent_slug"],
        top_k     = 5,
    )

    # Append retrieved chunks to state["context"]
    snippets = [r["content"] for r in results]
    return {"context": state["context"] + snippets}
```

The next `llm_agent` node sees `state["context"]` and can format it into its prompt:

```python
context_block = "\n\n".join(state["context"])
system_prompt = f"{node_prompt}\n\n### Retrieved Context\n{context_block}"
```

---

## Step 10 — Human-in-the-Loop (`human_approval`)

When an `interrupt_before=["hitl_sap_writes"]` node is reached, LangGraph suspends:

```python
# Inside the HITL node:
interrupt({"approval_request": {"tool": tool_name, "args": params}})
```

The WebSocket receives:
```json
{"type": "approval_request", "tool": "getraind-sap", "args": {...}}
```

The browser shows an approval dialog. The user clicks Approve or Reject:
```json
{"type": "approval", "approved": true}
```

GTAIF calls `graph.ainvoke({"approval_granted": true}, config)` to resume the graph from the checkpoint stored by MongoDBSaver.

---

## Step 11 — Completion

When the graph reaches `END`:

```python
metrics = await hook.finalize()
# metrics = {
#   "tokens_in":    412,
#   "tokens_out":   89,
#   "cost_usd":     0.0014,
#   "latency_ms":   1823,
#   "ttft_ms":      340,
#   "provider":     "anthropic",
#   "model":        "claude-haiku-4-5-20250929",
#   "llm_calls":    3,
# }

await ws.send_json({"type": "done", "metrics": metrics})
```

The `done` event triggers the UI to re-enable the input field and display the metrics panel.

---

## State Persistence Between Turns

`MongoDBSaver` writes the full `AgentState` to `langgraph_checkpoints` after every node. The `thread_id = session_id` is the checkpoint key.

When the user sends a second message in the same session:
- `astream_events` is called again with the new `HumanMessage` appended to `state["messages"]`
- LangGraph loads the previous checkpoint from MongoDB automatically (the `checkpointer` was compiled into the graph)
- The graph resumes from `start_node` with full history

This means conversations are **stateful across browser refreshes** for the lifetime of the session in MongoDB.
