# Agent Builder Studio — Runtime Reference

This document covers how the Agent Builder Studio compiles, executes, and routes
conversations from a user's first "Hi" through to a fully written agent primitive.

---

## What It Is

The Agent Builder Studio is the platform's **meta-agent** — an AI that builds other
AI agents. When a user opens the Designer tab in AI Studio, they are talking to
`agent-builder-studio`, a LangGraph agent defined in:

```
GENAI_PRIMITIVES/Getraind/Agents/agent-builder-studio.md
```

It guides the user through a 7-stage methodology and, when all stages are complete,
designs and writes a production-ready `.md` primitive to the filesystem.

---

## Architecture Overview

```
User message
     │
     ▼
  architect  ─── executor role (large model, temp=0.3)
  (converses)     Responds naturally. Embeds JSON spec if a file read is needed.
     │
     │  static edge (always)
     ▼
   router    ─── router role (tiny model, temp=0.0, max_tokens=32)
  (classifies)   Reads the conversation + architect response. Outputs ONE word.
     │
     ├─ continue ──→ END          (wait for next user message)
     ├─ read     ──→ reader       (portal_task: filesystem.read)
     │                  └──→ architect  (static edge back)
     ├─ think    ──→ thinker      (ReAct reasoning + artifact design)
     │                  ├─ tool_call ──→ thinker_tool ──→ thinker (loop)
     │                  ├─ continue  ──→ thinker (loop)
     │                  └─ done     ──→ evaluator
     │                                     ├─ approved ──→ writer ──→ architect
     │                                     └─ retry    ──→ architect
     ├─ done     ──→ END
     └─ end      ──→ END
```

---

## Node Reference

### `architect` — Conversational Executor

| Property | Value |
|---|---|
| type | `llm_agent` |
| role | `executor` |
| Gateway | NVIDIA nemotron → Anthropic Haiku fallback |
| Temperature | 0.3 |
| Max tokens | 4 096 |
| Sets route | ✗ — routing delegated to `router` node |
| Max iterations | 50 |

**Responsibilities:**
- Greet the user and load the methodology skill file on the first turn
- Guide the user through the 7 stages (ask questions, suggest options, confirm stages)
- Respond naturally to any message — including greetings, off-topic questions, and clarifications
- Signal a file read by embedding a JSON spec in its response
- Signal artifact readiness with the phrase _"All requirements are confirmed. I am ready to generate the artifact."_

**What it does NOT do:**
- Write routing keywords (`continue`, `read`, `think`, `done`) — the router handles that
- Loop back to itself — the router decides whether to end the turn or continue

**First-turn behaviour:**

On the very first turn, the architect greets the user and includes a filesystem read
spec at the end of its response. The router detects the JSON and routes to `reader`,
which loads the skill file. The skill appears in Retrieved Context on the next turn.

```
[Architect response, turn 1]
"Hello! I'm the Getraind Agent Designer. Let me load my methodology guide..."

{"tool": "filesystem", "operation": "filesystem.read",
 "params": {"path": "/app/primitives/Getraind/Skills/agent-builder-studio.md"}}
```

---

### `router` — Intent Classifier

| Property | Value |
|---|---|
| type | `llm_agent` |
| role | `router` |
| Gateway | NVIDIA phi-4-mini → gemma-3-12b → llama-3.2-3b fallback |
| Temperature | 0.0 (greedy — deterministic) |
| Max tokens | 32 (physically blocks prose output) |
| Sets route | ✓ |
| Valid routes | `continue`, `read`, `think`, `done`, `end` |

**Responsibilities:**
- Read the full conversation history + architect's latest response
- Output exactly one routing keyword based on the rules below

**Classification rules:**

| Route | Trigger |
|---|---|
| `read` | Architect's last message contains a JSON object with `"tool": "filesystem"` |
| `think` | Architect said _"All requirements are confirmed. I am ready to generate the artifact."_ |
| `done` | Artifact written to disk AND architect confirmed it |
| `end` | User explicitly asked to stop, cancel, or quit |
| `continue` | Everything else — normal conversation, questions, file results, retries |

**Why a dedicated router?**

Without a separate router, the architect (a large executor model) had to simultaneously
produce natural conversational prose AND output a bare routing keyword as its first word.
Those constraints are incompatible — the same temperature and token budget cannot serve
both goals. A tiny router model (phi-4-mini at temp=0, 32 tokens) is perfectly suited
to classification. The architect now writes freely.

---

### `thinker` — Artifact Designer (ReAct)

| Property | Value |
|---|---|
| type | `thinker` |
| role | `executor` |
| Gateway | NVIDIA nemotron-550b → minimax-m2.7 → kimi-k2.6 (reasoning models) |
| Temperature | 0.4 |
| Max tokens | 8 192 |
| Max iterations | 10 |

**Responsibilities:**
- Extract all requirements from the full conversation history
- Choose the best template pattern (react / triage / deep_research / plan_execute / custom)
- Design each node: role, system prompt, tools, routing
- Validate routing completeness (every emitted keyword covered by a conditional_edge)
- Call filesystem tools mid-thought to inspect existing primitives for reference
- Produce the complete `.md` artifact between `ARTIFACT_START` / `ARTIFACT_END` markers

**ReAct loop:**

```
thinker reasons...
  → needs data? → tool_call → thinker_tool (portal_task) → thinker (continues)
  → ready?      → done      → evaluator
  → blocked?    → retry     → architect (requests clarification from user)
```

**Output format:**
```xml
<reasoning>
Step 1: Template chosen — deep_research because cross-system investigation
Step 2: Nodes defined — orchestrator (router), thinker (ReAct), synthesizer (executor)
Step 3: Routing validated — all routes covered
Step 4: Tools declared — getraind-sap, getraind-http, web_search
Step 5: Guardrails — prompt-injection-shield
</reasoning>
<note>SAP RFC calls must use getraind-sap, not direct HTTP</note>
<conclusion>done</conclusion>

ARTIFACT_START
---
[full YAML frontmatter + node prompts]
---
ARTIFACT_END
```

---

### `evaluator` — Quality Reviewer

| Property | Value |
|---|---|
| type | `evaluator` |
| role | `executor` |
| Max iterations | 5 |

**8-point checklist** (rejects if ANY fails):

1. YAML structure — `metadata`, `spec`, `topology` present and valid
2. `spec.tools[]` — every tool referenced in prompts is declared
3. Security — `prompt-injection-shield` guardrail present
4. Governance — destructive ops have `human_approval` in `interrupt_before[]`
5. Completeness — every `llm_agent`/`thinker`/`evaluator` node has a system prompt
6. Routing coverage — every emitted keyword appears in `conditional_edges`
7. Iteration limits — `max_iterations` set on all `llm_agent` and `thinker` nodes
8. ReAct correctness — thinker nodes have `tool_call → portal_task` edge

**Routes:** `approved → writer` | `retry → architect` | `done → END`

---

### `writer` — Artifact Persister

| Property | Value |
|---|---|
| type | `portal_task` |
| Tool | `filesystem.write` |

Reads the artifact text from the thinker's `ARTIFACT_START…ARTIFACT_END` block and
writes it to:
```
/app/primitives/Users/{username}/Agents/{slug}.md
```
Static edge back to `architect`, which confirms the write to the user.

---

### `reader` — Primitive Loader

| Property | Value |
|---|---|
| type | `portal_task` |
| Tool | `filesystem.read` |

Reads the JSON spec from the architect's last AI message (see JSON extraction below)
and injects the file contents into `state["context"]`. Static edge back to `architect`.

---

## Session Lifecycle

### Turn 1 — First Message

```
User: "Hi, can you help me build an agent?"
         ↓
architect  →  greets user + embeds filesystem read spec for skill file
         ↓
router     →  detects JSON → routes "read"
         ↓
reader     →  loads /app/primitives/Getraind/Skills/agent-builder-studio.md
         ↓
architect  →  now has skill in context → introduces itself + asks Stage 1 questions
         ↓
router     →  normal conversation → routes "continue" → END
```

Checkpoint saved. Graph ends. Waits for user's next message.

### Ongoing Turns — Requirements Gathering

```
User: "I need an agent that monitors SAP open POs and alerts via email"
         ↓
architect  →  asks 3-4 focused questions (Stage 1: Requirements)
         ↓
router     →  routes "continue" → END
```

Each turn: one architect response → one router decision → checkpoint → wait for user.
No loops. No accumulated iteration count burning against the limit.

### Turn N — All 7 Stages Complete

```
architect  →  "...Stage 6 confirmed. All requirements are confirmed.
               I am ready to generate the artifact."
         ↓
router     →  detects readiness phrase → routes "think"
         ↓
thinker    →  reasons through design (may call filesystem for reference)
         ↓
evaluator  →  validates against 8-point checklist
         ↓
writer     →  writes .md to primitives volume
         ↓
architect  →  "Your agent has been created at Users/mpabba/Agents/sap-po-monitor.md"
         ↓
router     →  routes "done" → END
```

---

## JSON Spec Extraction — Two-Strategy Parser

When `reader` (a `portal_task` node) needs to know which file to read, it looks for
a JSON tool spec in the architect's last AI message.

**Strategy 1 — legacy "skip line 1" (backward compatible):**

```python
lines = content.splitlines()
spec = json.loads("\n".join(lines[1:]).strip())
```
Works when the architect writes the routing keyword on line 1 (old design).

**Strategy 2 — full-message scan (new design):**

```python
decoder = json.JSONDecoder()
idx = 0
while idx < len(content):
    start = content.find("{", idx)
    obj, _ = decoder.raw_decode(content, start)
    if isinstance(obj, dict) and "tool" in obj:
        spec = obj   # first valid tool spec found anywhere in message
        break
    idx = start + 1
```

Finds the first valid `{"tool": ...}` object anywhere in the response — works
regardless of how much prose surrounds the JSON spec.

---

## Loop Protection

Every node in the system tracks its own visit count in `state["node_visits"]`.

```python
# Per-node visit guard — checked at the start of every node execution
if _visit_guard(state, node_id, max_visits):
    return {"done": True, "route": "done"}   # force-break the loop
```

**Default limits:**

| Node | Max visits | Rationale |
|---|---|---|
| `architect` | 30 (default) | Each user turn = 1 visit; 30 turns per session |
| `router` | 30 (default) | 1:1 with architect |
| `thinker` | 10 (`max_iterations`) | Bounded ReAct loop |
| `evaluator` | 5 (`max_iterations`) | 5 eval retries max |
| `reader` | 30 (default) | Multiple file reads allowed |
| `writer` | 30 (default) | Re-writes allowed |

Override in topology YAML:
```yaml
- id: thinker
  type: thinker
  max_iterations: 10
  max_visits: 15    # hard ceiling even if max_iterations is hit repeatedly
```

The `continue → architect` loop of the old design is eliminated. Each architect
invocation is one user turn; `continue → END` terminates the turn cleanly.

---

## State Isolation

Nodes may only update fields appropriate to their role. The framework enforces this
via `_safe_updates()`:

```python
_PROTECTED_STATE_FIELDS = frozenset({"session_id", "username", "agent_slug"})

def _safe_updates(node_id, updates):
    blocked = {k for k in updates if k in _PROTECTED_STATE_FIELDS}
    if blocked:
        log.warning("node.state_isolation_violation", node=node_id, blocked_keys=...)
        return {k: v for k, v in updates.items() if k not in _PROTECTED_STATE_FIELDS}
    return updates
```

Session metadata (`session_id`, `username`, `agent_slug`) can never be overwritten
by a node — they are set once at invocation time by `ws.py`.

---

## Dynamic Tool Allocation

A planner/orchestrator can restrict which tools are available to downstream nodes:

```python
# Orchestrator writes this before routing to an executor
state["tool_allocation"] = {
    "allowed": ["getraind-sap", "filesystem"],
    "denied":  ["getraind-shell"],
}
```

Resolution order (first match wins):

1. `state["tool_allocation"]["allowed"]` — runtime override set by orchestrator
2. `node_cfg["allowed_tools"]` — static declaration in topology YAML
3. `None` — all tools declared in `spec.tools[]` are available

---

## LLM Gateway — Role Assignments

The gateway selects model + settings based on the node's `role` field:

| Role | Temperature | Max tokens | Primary NVIDIA models |
|---|---|---|---|
| `executor` | 0.3 | 4 096 | nemotron-120b → llama-70b → mistral-large |
| `router` | 0.0 | 32 | phi-4-mini → gemma-3-12b → llama-3.2-3b |
| `thinker` | 0.4 | 8 192 | nemotron-550b → minimax-m2.7 → kimi-k2.6 |
| `evaluator` | 0.1 | 2 048 | nemotron-49b → llama-70b |
| `validator` | 0.0 | 128 | phi-4-mini → llama-3.2-3b |
| `retriever` | 0.0 | 512 | phi-4-mini → llama-3.2-3b |
| `memory_manager` | 0.0 | 1 024 | llama-3.2-3b → phi-4-mini |

All roles fall back to Anthropic Haiku if NVIDIA is unavailable.
`thinker` falls back to Anthropic Sonnet (best reasoning available).

---

## Extending the Graph

### Adding a new node type to future agent designs

The following node types are available to any agent topology (including ones built by
the studio):

| Type key | Builder function | Routes |
|---|---|---|
| `llm_agent` | `build_llm_agent_node` | Any keyword in `valid_routes` |
| `thinker` | `build_thinker_node` | `tool_call`, `continue`, `done`, `retry` |
| `evaluator` | `build_evaluator_node` | `approved`, `retry`, `done` |
| `validator` | `build_validator_node` | `approved`, `rejected` |
| `memory_manager` | `build_memory_manager_node` | `done` (declarative) |
| `cache` | `build_cache_node` | `cache_hit`, `cache_miss` (read) / `done` (write) |
| `portal_task` | `build_portal_task_node` | N/A (static edge) |
| `rag_retriever` | `build_rag_node` | N/A (static edge) |
| `human_approval` | `build_approval_node` | `approved`, `rejected` |
| `handoff` | `build_handoff_node` | N/A (static edge) |

### Cache topology pattern

```yaml
nodes:
  - id: cache_check  # type: cache, config: {mode: read, ttl: 3600}
  - id: executor     # type: llm_agent, role: executor
  - id: cache_write  # type: cache, config: {mode: write, ttl: 3600}

edges:
  - from: cache_write
    to: END

conditional_edges:
  - from: cache_check
    options:
      - condition: cache_hit   # served from cache — skip executor
        to: END
      - condition: cache_miss  # run the expensive path
        to: executor
  - from: executor
    options:
      - condition: done
        to: cache_write        # persist result for future requests
```

### Validator topology pattern

```yaml
nodes:
  - id: input_validator  # type: validator — checks user input against criteria
  - id: executor         # type: llm_agent — only runs on valid input

conditional_edges:
  - from: input_validator
    options:
      - condition: approved
        to: executor
      - condition: rejected
        to: END            # or a "rejection_handler" node
```

---

## The 7-Stage Methodology

The architect follows this gate-based process. Each stage must be explicitly confirmed
before moving to the next.

| Stage | Name | What is gathered |
|---|---|---|
| 1 | Requirements | Purpose, users, inputs, outputs, success criteria |
| 2 | Design | Pattern (react/triage/etc.), LLM roles, node count, tool selection |
| 3 | Code | YAML topology, per-node system prompts, routing completeness |
| 4 | Review | 8-point evaluator checklist run mentally before calling thinker |
| 5 | Testing | Scenario matrix, edge cases, expected routes per scenario |
| 6 | Evaluation | Correctness, safety, performance thresholds |
| 7 | Output | Artifact written to `Users/{username}/Agents/{slug}.md` |

The router will not route `think` until the architect explicitly signals readiness.
The thinker produces the artifact. The evaluator validates. The writer persists.

---

## Configuration Reference

### `agent-builder-studio.md` — Key Topology Fields

```yaml
topology:
  start_node: architect

  nodes:
    - id: architect
      type: llm_agent
      role: executor        # large model — conversational quality
      max_iterations: 50    # max LLM calls before force-stopping
      # max_visits: 30      # optional hard ceiling on re-entries (default 30)

    - id: router
      type: llm_agent
      role: router          # tiny model — classification only
      sets_route: true
      valid_routes: [continue, read, think, done, end]
      # max_visits: 30

    - id: thinker
      type: thinker
      role: executor
      max_iterations: 10    # ReAct loop iterations

    - id: evaluator
      type: evaluator
      role: executor
      max_iterations: 5     # retry budget for re-evaluation

  edges:
    - from: architect       # static — always classify after architect responds
      to: router

  conditional_edges:
    - from: router
      options:
        - condition: continue   to: END       # end turn, await user
        - condition: read       to: reader    # trigger file read
        - condition: think      to: thinker   # start design phase
        - condition: done       to: END
        - condition: end        to: END
```

### Node-level overrides

Any node config field can be set in the YAML topology:

| Field | Type | Default | Description |
|---|---|---|---|
| `max_iterations` | int | 15 | Max LLM invocations before `done` forced |
| `max_visits` | int | 30 | Max times this node can be entered per session |
| `valid_routes` | list[str] | all routes | Restrict which route keywords are accepted |
| `allowed_tools` | list[str] | all spec tools | Static tool restriction for this node |

---

## Observability

Every architect and thinker turn fires `_record_llm_episode()` asynchronously, which
writes to the memory store for future context injection. Tool calls write to
`tool_results` in state and emit `{"type": "tool_result"}` WebSocket events.

Node visit counts are visible in state:
```json
{
  "node_visits": {
    "architect": 3,
    "router":    3,
    "reader":    1,
    "thinker":   1,
    "evaluator": 1,
    "writer":    1
  }
}
```

Routing decisions are logged at DEBUG level:
```
node.routed  node=router  route=read
node.routed  node=router  route=think
node.routed  node=router  route=done
```

Loop protection triggers at ERROR level with a clear message:
```
node.visit_limit_exceeded  node=router  visits=30  limit=30
  note="Forcing route=done to break the loop"
```
