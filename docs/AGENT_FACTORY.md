# Agent Factory

`GTAIF/factory/factory.py` — `AgentFactory`

The Agent Factory is the bridge between a **primitive definition file** (a `.md` file on disk) and a **running LangGraph graph**. It owns compilation, caching, and hot-reload invalidation.

---

## Overview

```
GENAI_PRIMITIVES/
  Getraind/Agents/sap-assistant.md   ←  primitive file (YAML frontmatter + node prompts)
           ↓  parsed by PrimitivesRegistry
  ParsedPrimitive(slug, topology, node_prompts, spec)
           ↓  compiled by AgentFactory._compile()
  LangGraph CompiledStateGraph
           ↓  cached in-memory by (username, slug)
  graph.astream_events(state, config, version="v2")
           ↓  streamed to WebSocket → UI
```

---

## Startup Wiring (`main.py`)

```python
registry = init_registry(settings.primitives_path)
factory  = AgentFactory(
    registry          = registry,
    gateway           = gateway,           # LLMGateway — LLM calls
    node_cache        = app.state.node_cache,
    doc_store         = None,              # RAG DocumentStore (wired later)
    memory_store      = app.state.memory, # MemoryStore — 4-layer memory
    traces_collection = db["Traces"],     # Motor collection for observability
)
app.state.agent_factory = factory
```

`AgentFactory.__init__` immediately calls `configure_node_resources(...)` which injects `gateway`, `node_cache`, `doc_store`, `memory_store`, and **`agent_factory` (self)** into module-level globals in `factory/nodes.py`. This is intentional — node builder functions are module-level closures and cannot receive these resources via constructor injection. The `agent_factory` reference enables `handoff` nodes to build and invoke sub-agent graphs without a circular import.

---

## The Primitive File Format

Every agent is defined in a Markdown file with YAML frontmatter:

```markdown
---
metadata:
  type: agent
  slug: sap-assistant
  name: SAP Assistant
  version: 1.0.0

topology:
  start_node: router

  nodes:
    - id: router
      type: llm_agent
      sets_route: true
      max_iterations: 20

    - id: sap_executor
      type: portal_task

  edges:
    - from: sap_executor
      to: router

  conditional_edges:
    - from: router
      routes:
        execute: sap_executor
        done:    END
---

# Node: router
You are an SAP assistant router. Read the user's request and decide:
- If an SAP action is needed, respond with the first word "execute".
- If you have enough information to answer, respond with the first word "done".

[Your routing logic here...]

# Node: sap_executor
[System prompt for the SAP executor node — injected at runtime]
```

The `# Node: {id}` sections become the **system prompt** for each node. They are extracted by `parse_primitive()` in `primitives/parser.py` and stored in `ParsedPrimitive.node_prompts`.

---

## Build Pipeline: `get_or_build()`

```
ws.py calls factory.get_or_build(slug, username, checkpointer)
  │
  ├─ Cache hit?  → return cached CompiledStateGraph immediately
  │
  └─ Cache miss:
       1. registry.get("agent", slug, username)  →  ParsedPrimitive
          (user scope first, falls back to platform scope)
       2. _compile(primitive, checkpointer)
            a. StateGraph(AgentState) created
            b. For each node in topology.nodes:
                 builder = NODE_BUILDERS[node_type]
                 graph.add_node(node_id, builder(node_cfg, prompt))
            c. graph.set_entry_point(topology.start_node)
            d. For each edge in topology.edges:
                 graph.add_edge(from, to)
            e. For each conditional_edges entry:
                 graph.add_conditional_edges(
                     from,
                     router_fn,   ← reads state["route"]
                     {route_key: target_node, ...}
                 )
            f. graph.compile(checkpointer=checkpointer,
                             interrupt_before=hitl_nodes)
       3. Compiled graph stored in _cache[(username, slug)]
       4. Return graph
```

**Thread safety**: the cache is protected by `threading.RLock`. Multiple concurrent WebSocket connections can call `get_or_build` simultaneously; only one compilation per key will happen.

---

## Node Types and Their Builders

| `type` in YAML | Builder function | Purpose |
|---|---|---|
| `llm_agent` | `build_llm_agent_node()` | LLM call; optionally sets `state["route"]` |
| `thinker` | `build_thinker_node()` | Chain-of-thought; ReAct tool-calling; writes `reasoning_steps` |
| `evaluator` | `build_evaluator_node()` | Scores output; routes `approved` / `retry` / `done` |
| `portal_task` | `build_portal_task_node()` | Calls Portal task API or filesystem; appends to `tool_results` |
| `tool_call` | `build_tool_call_node()` | Web search (Tavily → SerpAPI → DDG); optional NodeOutputCache |
| `rag_retriever` | `build_rag_retriever_node()` | Vector search from DocumentStore; fills `state["context"]` |
| `human_approval` | `build_human_approval_node()` | LangGraph `interrupt()` — suspends for human review |
| `handoff` | `build_handoff_node()` | Delegates task to a sub-agent; result returned to orchestrator |

---

## Multi-Agent Handoffs

When one agent needs to delegate a subtask to a specialist agent, it uses a `handoff` node. The calling agent is the **orchestrator**; the called agent is the **sub-agent**.

### Flow

```
Orchestrator llm_agent
  │  writes state["extra"] = {"agent_slug": "sap-assistant", "task": "Find open POs for vendor 1000"}
  │  sets state["route"]   = "handoff"
  ▼
handoff node (build_handoff_node)
  │  calls factory.get_or_build(slug="sap-assistant", checkpointer=None)
  │  creates minimal initial state with HumanMessage(task)
  │  awaits sub_graph.ainvoke(sub_initial)          ← stateless, no checkpoint
  │  extracts last AIMessage from sub-agent
  │  writes state["handoff_result"] = <last AI response>
  │  appends to state["context"]    = ["[Sub-agent sap-assistant]\n<response>"]
  │  appends to state["handoff_stack"] = [{from, to, task, result, ts}]
  │  clears state["handoff_to"] and state["handoff_input"]
  ▼
Orchestrator llm_agent (next turn)
  │  sees sub-agent result in state["context"]
  │  continues conversation or routes done
```

### Sub-agent execution model

Sub-agents run **stateless and single-shot** — `checkpointer=None`. This means:
- No checkpoint history is written for the sub-agent run
- The sub-agent starts fresh with only the delegated task as its first message
- The orchestrator waits for `ainvoke()` to complete before continuing (not streaming)
- If the sub-agent fails, `handoff_result` contains the error string; the orchestrator sees it in context and can recover

### Primitive topology pattern

```yaml
nodes:
  - id: orchestrator
    type: llm_agent
    sets_route: true
    max_iterations: 20

  - id: sub_agent_runner      # handoff node — no system prompt needed
    type: handoff

edges:
  - from: sub_agent_runner
    to: orchestrator          # always static edge back to orchestrator

conditional_edges:
  - from: orchestrator
    options:
      - condition: handoff
        to: sub_agent_runner
      - condition: continue
        to: orchestrator
      - condition: done
        to: END
```

### AgentState handoff fields

| Field | Reducer | Purpose |
|---|---|---|
| `handoff_to` | replace | Target agent slug — cleared after handoff completes |
| `handoff_input` | replace | Task text passed to sub-agent — cleared after handoff completes |
| `handoff_result` | replace | Sub-agent's last AI response — readable by orchestrator on next turn |
| `handoff_stack` | `_append_list` | Audit trail — one entry per handoff; never cleared |

Each `handoff_stack` entry: `{"from_agent": str, "to_agent": str, "task": str, "result": str, "ts": str}`.

### Nested handoffs

Sub-agents can themselves contain `handoff` nodes — Agent A → Agent B → Agent C is valid. Each level contributes its own entry to its own `handoff_stack`. The orchestrator at each level only sees its immediate sub-agent's result in `handoff_result`; the deeper call stack is recorded in the sub-agent's own state (not propagated up, since sub-agents run in isolated `ainvoke` calls).

---

### Routing Convention

Any node with `sets_route: true` in its config must output its routing decision as the **first word** of the LLM response. The factory installs a conditional edge with a router function that reads `state["route"]`:

```python
def _make_router(routes: dict[str, str]):
    def _fn(state: AgentState) -> str:
        return state.get("route", "continue")
    return _fn
```

**Critical**: LangGraph raises `ValueError` if a node has **both** a static edge and a conditional edge. The factory validates this at build time.

---

## Memory Injection (llm_agent nodes)

Before every LLM call, `build_llm_agent_node` injects enterprise memory context:

```python
if _memory_store:
    memory_context = await _memory_store.inject_into_context(
        username   = state["username"],
        agent_slug = state["agent_slug"],
        query      = last_human_message,
    )
    # memory_context → prepended to system prompt as:
    # "### Relevant Memory\n{facts}\n\n---\n\n{original_system_prompt}"
```

After the LLM responds, an episodic memory event is fired as a background task:

```python
asyncio.create_task(_record_llm_episode(state, response, agent_slug))
```

---

## Hot-Reload (Watchdog)

When a `.md` file changes in `GENAI_PRIMITIVES/`:

```
watchdog Observer fires _MDChangeHandler.on_modified()
  → registry._reload_file(path)
  → ParsedPrimitive re-parsed
  → registry._on_agent_change(slug, username) called
  → factory.invalidate(slug, username)
  → _cache entry deleted

Next WebSocket message triggers get_or_build()
  → cache miss → re-compile with new topology/prompts
  → new graph cached
```

No container restart needed. Changes are live within seconds.

---

## Observability

`make_hook()` creates a fresh `GTAIFCallbackHandler` per run:

```python
hook = factory.make_hook(
    session_id        = session_id,
    username          = username,
    agent_slug        = agent_id,
    traces_collection = db["Traces"],
)
config["callbacks"] = [hook]
```

The hook tracks: first-token latency, total tokens (in/out), LLM cost estimates (via `PLATFORM_PRICING`), provider used, and total wall-clock time. At run end, `hook.finalize()` writes a trace document to MongoDB and returns the metrics dict sent to the UI as the `done` message.

---

## Cache Key Design

```python
key = f"{username or '__platform__'}:{slug}"
```

- Platform primitives (`scope=platform`, `username=None`) use `__platform__:slug`
- User overrides use `alice:my-custom-agent`
- User lookup falls back to platform: if `alice:sap-assistant` is not found, `__platform__:sap-assistant` is returned
