---
title: "The Agent Factory: How Declarative Primitives Become Running Agents"
date: 2026-06-16
tags: Enterprise AI, Agent Factory, LangGraph, Declarative AI, Getraind, AI Studio, Primitives
excerpt: Building a new AI agent shouldn't require an engineer and a two-week sprint. In Getraind, an agent is a markdown file — topology, prompts, and metadata — that the Agent Factory compiles into a running LangGraph graph on demand. Zero code, hot reload, no redeployment.
image: assets/images/blog/covers/AI-Infrastructure.png
---

# The Agent Factory: How Declarative Primitives Become Running Agents

The most expensive bottleneck in enterprise AI adoption isn't hardware. It's the engineering queue.

A business team identifies a workflow that AI could improve. They write a requirements document. It goes into the backlog. An engineer picks it up two weeks later, interprets the requirements, builds something, deploys it, hands it back. By this point the requirements have changed, the original person has moved on, and the agent that gets deployed doesn't quite fit what was actually needed.

This cycle repeats. Adoption stays low. The business team stops asking.

The Agent Factory is my answer to this problem. Its design premise is simple: **an agent should be describable in a plain text file, and that description should be sufficient to run the agent in production — without an engineer in the loop.**

---

## The Primitive File Format

Every agent in Getraind is defined in a Markdown file with YAML frontmatter. These files are called **primitives**, and they live in a version-controlled directory on disk.

A complete primitive looks like this:

```markdown
---
metadata:
  type: agent
  slug: contract-reviewer
  name: Contract Reviewer
  version: 1.0.0

spec:
  tools:
    - getraind-documents
    - getraind-legal
  memory:
    episodic: true
    semantic: true
    scope: legal-team
  guardrails:
    - prompt-injection-shield

topology:
  start_node: router

  nodes:
    - id: router
      type: llm_agent
      sets_route: true
      max_iterations: 20

    - id: reviewer
      type: thinker
      max_iterations: 10

    - id: summarizer
      type: llm_agent

  edges:
    - from: reviewer
      to: router

    - from: summarizer
      to: END

  conditional_edges:
    - from: router
      routes:
        review: reviewer
        summarize: summarizer
        done: END
---

# Node: router
You are a contract review assistant router. Read the user's message and decide:
- If they need a detailed clause-by-clause review, respond with "review".
- If they want a plain-language summary of an already-reviewed contract, respond with "summarize".
- If the task is complete, respond with "done".

# Node: reviewer
You are an expert legal contract reviewer...
[detailed system prompt for the reviewer node]

# Node: summarizer
You are a plain-language contract summarizer...
[detailed system prompt for the summarizer node]
```

That's the entire agent definition. No Python, no Kubernetes manifests, no CI/CD pipeline. Just a markdown file.

---

## What the Factory Does With It

When a user opens this agent in AI Studio, the Agent Factory goes through the following pipeline:

```
Primitive file on disk (contract-reviewer.md)
         │
         ▼
  1. PrimitivesRegistry.get("agent", "contract-reviewer", username)
     → Parses YAML frontmatter → ParsedPrimitive object
     → Extracts node prompts from # Node: sections
     → User scope checked first, falls back to platform scope
         │
         ▼
  2. AgentFactory._compile(primitive, checkpointer)
     a. StateGraph(AgentState) initialized
     b. For each node in topology.nodes:
          builder = NODE_BUILDERS[node.type]
          graph.add_node(node.id, builder(node_config, node_prompt))
     c. set_entry_point(topology.start_node)
     d. For each edge: graph.add_edge(from, to)
     e. For each conditional_edge:
          graph.add_conditional_edges(from, router_fn, {route: target, ...})
     f. graph.compile(checkpointer=checkpointer, interrupt_before=hitl_nodes)
         │
         ▼
  3. Compiled CompiledStateGraph cached in memory
     key: (username, slug)
     lock: threading.RLock (thread-safe for concurrent WebSocket connections)
         │
         ▼
  4. On next request: cache hit → return immediately (O(1))
```

The cache is the key to performance. The first request for an agent triggers compilation — which takes a few hundred milliseconds. Every subsequent request for the same agent, by the same user, returns the cached graph instantly.

---

## Hot Reload: No Redeployment Ever

When someone updates a primitive file — changing a system prompt, adding a new node, adjusting routing logic — the change is live on the next request with no redeployment.

The factory compares the file's modification timestamp against the cache entry's timestamp. If the file is newer, the cache entry is invalidated and the agent is recompiled. The old graph is replaced atomically under the `RLock` — concurrent requests either get the old graph (if they started before invalidation) or the new one (if they start after). No request fails.

This is what enables Getraind's AI Studio design loop: a business analyst edits an agent prompt in the Studio's editor, saves, and the next message to that agent uses the updated prompt. The feedback loop is immediate.

---

## Node Types and Their Behaviors

The YAML `type` field in each node maps to a builder function that creates the LangGraph node:

| Type | Builder | What It Does |
|---|---|---|
| `llm_agent` | `build_llm_agent_node()` | Single LLM call. Optionally sets `state["route"]` as first word of response. |
| `thinker` | `build_thinker_node()` | ReAct loop: reason → tool call → reason → ... → done. Writes to `state["reasoning_steps"]`. |
| `evaluator` | `build_evaluator_node()` | Scores output against a rubric. Routes `approved`, `retry`, or `done`. |
| `portal_task` | `build_portal_task_node()` | Calls an external system (SAP, filesystem, HTTP). Reads tool spec from `state["extra"]`. |
| `tool_call` | `build_tool_call_node()` | Web search (Tavily → SerpAPI → DuckDuckGo fallback chain). Cached via NodeOutputCache. |
| `rag_retriever` | `build_rag_retriever_node()` | Vector search from DocumentStore. Fills `state["context"]`. |
| `human_approval` | `build_human_approval_node()` | LangGraph `interrupt()` — suspends the graph until a human approves via WebSocket. |
| `handoff` | `build_handoff_node()` | Delegates to a sub-agent. Runs it stateless, returns result to orchestrator. |

Every `llm_agent` node injects memory context before the LLM call — episodic memory of past interactions, semantic memory of domain facts, procedural memory of how things work in this organization. This happens automatically; the node prompt author doesn't need to write memory retrieval logic.

---

## Memory Injection Is Automatic

Before every LLM call in an `llm_agent` node, the factory injects relevant memory context into the system prompt:

```python
if memory_store:
    memory_context = await memory_store.inject_into_context(
        username   = state["username"],
        agent_slug = state["agent_slug"],
        query      = last_human_message,
    )
    full_system_prompt = f"### Relevant Memory\n{memory_context}\n\n---\n\n{node_prompt}"
```

The memory system retrieves the most relevant episodic events, semantic facts, and procedural knowledge for this user, this agent, and this query — and prepends it to the system prompt. The node prompt author writes their agent logic; the memory layer handles personalization transparently.

This is why Getraind agents feel like colleagues rather than generic chatbots. The agent already knows that you were working on a specific SAP issue last Tuesday, that you prefer bullet-point summaries over prose, and that your organization's procurement process requires VP approval above $50K. None of that needs to be re-stated in every message.

---

## Multi-Agent Handoffs

When an orchestrator agent needs to delegate a subtask to a specialist, it uses a `handoff` node. The orchestrator writes its decision to `state["extra"]`:

```json
{
  "tool_call": {
    "agent_slug": "sap-assistant",
    "task": "Find all open purchase orders for vendor 1000 from the last 30 days"
  }
}
```

The handoff node calls `factory.get_or_build("sap-assistant")`, runs the sub-agent stateless (`checkpointer=None`), and writes the result back to `state["handoff_result"]`. The orchestrator's next turn sees the sub-agent's answer in context and continues.

Sub-agents run isolated and single-shot — they don't inherit the orchestrator's conversation history or memory scope. This is intentional: it enforces clean interfaces between agents and prevents context contamination.

The handoff pattern enables a critical architectural decision: **you don't have to design one agent that knows everything**. An orchestrator that's good at understanding intent + sub-agents that are experts in their domain is both more capable and easier to maintain than a monolithic agent trying to do it all.

---

## The Primitive Registry: User Scope vs. Platform Scope

Primitives are organized into two scopes:

- **Platform scope** — shared across all users. System-provided agents like the Agent Builder Studio. Managed by the platform team.
- **User scope** — owned by a specific user. Agents they've designed in AI Studio. Takes precedence over platform scope when slugs conflict.

When the factory resolves `get_or_build("sap-assistant", username="mpabba")`, it checks mpabba's personal primitives directory first. If a custom `sap-assistant.md` exists there, that version runs. If not, it falls back to the platform-provided version.

This means users can fork and customize any platform agent without affecting other users — and platform updates to the base agent don't override user customizations.

---

## From Primitive File to Production Agent: Full Timeline

1. Business analyst opens AI Studio and describes what they need
2. Agent Builder Studio guides them through the 7-stage design methodology
3. Agent Builder generates the `.md` primitive file and writes it to disk
4. Next request for that agent → Agent Factory compiles it → running in seconds
5. Analyst tests the agent in AI Studio, provides feedback
6. Agent Builder updates the primitive on disk
7. Next request → hot reload → updated agent running immediately
8. Repeat until the agent fits the workflow

No Jira tickets. No engineering sprint. No deployment pipeline. The cycle from "I want a new agent" to "it's working" is measured in minutes, not weeks.

That's the goal the Agent Factory was built to achieve.

*Next: [The Agent Builder Runtime: The AI That Designs AI](/posts/2026-06-17-agent-builder-runtime.html)*
