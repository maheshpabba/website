---
title: "Designing Enterprise AI: From Business Problem to Agent Architecture"
date: 2026-06-15
tags: Enterprise AI, AI Design, Agent Architecture, Business Problems, Getraind, AI Studio
excerpt: Before you buy a GPU or write a line of agent code, there are four questions that determine whether your enterprise AI succeeds or fails. Most teams skip them. Here's how I think through the design process — from identifying the right problems to mapping them to agent topologies.
image: assets/images/blog/covers/AI-Infrastructure.png
---

# Designing Enterprise AI: From Business Problem to Agent Architecture

The most common failure mode I see in enterprise AI isn't a technology failure. It's a design failure. Teams stand up infrastructure, build agents, deploy them to users — and then watch adoption stall at 10% because the agents don't actually fit the workflows they were meant to help with.

The root cause is almost always the same: the design process started with the technology and worked backward to the problem, instead of the other way around.

This post is about doing it the right way — starting with business problems, mapping them to the right agent patterns, and designing a system that users actually need.

---

## The Four Questions That Must Come First

Before any infrastructure decision, any model selection, any agent code — four questions:

### 1. What specific business problem are we solving?

Not "we want AI." Specific. Measurable. With a before/after you can actually observe.

Good examples:
- "Support engineers spend 35 minutes per ticket searching for similar past issues. We want that to be 5 minutes."
- "Contract review takes legal 3 days per contract. We want first-pass review in under 2 hours."
- "Procurement classification is done manually by two FTEs. 80% of it follows rules that could be automated."

Bad examples:
- "We want to leverage AI across the organization."
- "We need an AI assistant for our teams."
- "We want to be an AI-first company."

The specific version tells you what data the agent needs, what the latency requirement is, what success looks like, and who the users are. The vague version gives you none of that.

### 2. Who are the users and what does their workflow actually look like?

An enterprise AI agent is not a general chatbot. It's a tool shaped to a specific role, embedded in a specific workflow.

A support engineer's workflow: gets a ticket, searches the knowledge base, checks Slack history, maybe asks a colleague, writes a response. The AI fits at the search and draft-response steps — not as a general assistant that also answers questions about the weather.

A procurement analyst's workflow: receives a purchase request, validates it against policy, classifies it by category, routes it for approval. The AI fits at classification and policy validation — probably not at routing, which follows deterministic business rules.

The design question is not "what can AI do?" It's "where in this specific workflow does AI reduce friction without introducing new friction?"

### 3. What data does the agent need — and what must it never see?

Data access is the hardest problem in enterprise AI. Get it wrong and you have two failure modes, both bad:

- **Too restrictive**: The agent can't answer questions that would obviously help users, because it doesn't have access to the relevant data. Adoption dies.
- **Too permissive**: The agent reveals information across organizational boundaries — an HR query that returns another employee's performance review, a finance query that exposes data the requester isn't authorized to see. Trust collapses.

For every agent design, I map this explicitly:

| Data source | Read access | Write access | Scope restriction |
|---|---|---|---|
| Ticket history | Yes | No | User's team only |
| Knowledge base | Yes | No | All users |
| Employee records | No | No | HR agents only |
| Contract database | Yes | No | Legal + authorized roles |

This table isn't just documentation. It becomes the access control configuration for the agent's tool definitions and the memory permission boundaries.

### 4. How many users, at what concurrency?

This is infrastructure sizing input. But it also tells you something about the agent design.

An agent designed for 5 power users can use long-running deep reasoning chains and tolerate 30-second response times. An agent deployed to 5,000 users needs sub-5-second response time for the most common interactions, which usually means the common path should use Tier 1 or Tier 2 models, with Tier 3 reserved for genuinely complex requests.

Concurrency drives both hardware sizing (covered in the [GPU hardware post](/posts/2026-06-14-gpu-hardware-model-selection.html)) and agent topology design.

---

## Matching Problems to Agent Topologies

Once you've answered the four questions, you're ready to choose an agent topology — the pattern of nodes and edges that defines how the agent thinks and acts.

In Getraind's Agent Factory, every agent is defined as a primitive — a declarative markdown file with a YAML topology section. The topology describes nodes (what roles exist), edges (how they connect), and conditional edges (how routing decisions work).

Here are the patterns I use most frequently:

### Pattern 1: Router + Executor (Simple Task Agent)

**Best for**: Agents with a small number of well-defined actions. Support triage, procurement classification, document routing.

```
User message
     │
     ▼
  router          ← classifies intent into one of N actions
     │
     ├─ action_a → executor_a → router (loop)
     ├─ action_b → executor_b → router (loop)
     └─ done     → END
```

The router (tiny model, temperature 0.0, 32 tokens max) reads the user's message and outputs one keyword. The executor for that action runs, returns a result, and routes back to the router for follow-up or completion.

**When it fails**: When the action space is too large or ambiguous for a tiny router model. If you have more than 7–8 distinct actions, consider splitting into sub-agents with a higher-level orchestrator.

### Pattern 2: Planner + Executor (Multi-Step Task Agent)

**Best for**: Tasks that require breaking a goal into sequential steps before executing. Research workflows, data gathering pipelines, report generation.

```
User message
     │
     ▼
  planner         ← generates ordered step list, writes to state["plan"]
     │
     ▼
  executor        ← runs current step, advances state["current_step"]
     │
     ├─ more steps → executor (loop until plan complete)
     └─ done       → synthesizer → END
```

The planner runs once per user request. The executor loops through steps. A synthesizer (optional) combines results into a final response.

**Key design decision**: Should the planner generate the full plan upfront, or one step at a time? Full plans work better for deterministic pipelines (report generation). One-step-at-a-time works better for exploratory tasks where each result changes what to do next.

### Pattern 3: ReAct Thinker (Complex Reasoning Agent)

**Best for**: Problems that require iterative reasoning with tool use — data investigation, code debugging, research with web search and document retrieval.

```
User message
     │
     ▼
  thinker         ← reasons about the problem, decides on next action
     │
     ├─ tool_call → tool_runner → thinker (loop with result in context)
     ├─ continue  → thinker (more thinking needed)
     └─ done      → evaluator → END
```

The thinker uses a high-capability model (Tier 3) in a ReAct loop — reasoning about what to do, calling tools to get information, reasoning about the results. This is the most expensive topology per request. Reserve it for tasks that genuinely require it.

**Iteration limit is mandatory**: Without a `max_iterations` bound, a thinker loop can run indefinitely. Set it at 10–20 depending on task complexity.

### Pattern 4: Orchestrator + Sub-Agents (Multi-Domain Agent)

**Best for**: Tasks that span multiple enterprise systems or domains, where each domain is complex enough to warrant a specialized sub-agent.

```
User message
     │
     ▼
  orchestrator    ← determines which domain this task belongs to
     │
     ├─ → sap_agent        (SAP data and transactions)
     ├─ → hr_agent         (HR policy and employee data)
     ├─ → legal_agent      (contract review and compliance)
     └─ done → END
```

Each sub-agent is itself a complete primitive — it has its own topology, its own tool access, its own memory scope. The orchestrator delegates and receives results. Sub-agents run stateless and single-shot (no checkpoint), which means the orchestrator waits for completion before continuing.

**When to use this vs. a single large agent**: Use sub-agents when different domains require fundamentally different tool access, security contexts, or model requirements. Don't use them just to break up a complex prompt — a single well-designed agent is usually simpler and more debuggable.

---

## The Agent Design Process in Getraind AI Studio

In Getraind, agents are designed through the **Agent Builder Studio** — a meta-agent that guides you through a 7-stage methodology before generating the primitive file. I'll cover this in depth in the [Agent Builder Runtime post](/posts/2026-06-17-agent-builder-runtime.html).

But the methodology itself mirrors the process described in this post:

1. **Problem definition** — what are we solving, for whom?
2. **Workflow mapping** — where does AI fit in the current workflow?
3. **Data inventory** — what does the agent need to access, what is off-limits?
4. **Topology selection** — which pattern fits the problem?
5. **Node specification** — what does each node do, what model role does it use?
6. **Tool declaration** — what external systems does the agent call?
7. **Security review** — guardrails, human-in-the-loop requirements, scope restrictions

The Agent Builder walks through all seven stages in conversation. When all stages are confirmed, it generates the primitive file automatically. The Agent Factory picks it up and the agent is running within minutes.

---

## What Good Agent Design Produces

A well-designed enterprise agent has three properties that you can test:

**It fails gracefully.** When the agent can't answer a question — because the data isn't available, because the request is out of scope, because a tool call failed — it says so clearly and suggests what the user should do instead. It does not hallucinate an answer to avoid saying "I don't know."

**It respects boundaries.** The agent cannot access data its user isn't authorized to see, regardless of how the question is framed. This is enforced at the tool permission layer, not by prompting alone. Prompts can be manipulated; access controls cannot.

**It gets faster over time.** The cache and memory layers mean repeated or similar queries return faster than new ones. An agent that handles 100 similar support tickets has the common answers cached and the relevant domain knowledge in semantic memory. The 101st ticket is answered faster and better than the 1st.

---

## Before the Next Post

The design methodology produces a primitive specification — a description of topology, node roles, tool access, and prompts. Turning that specification into a running agent is the job of the Agent Factory.

*Next: [The Agent Factory: How Declarative Primitives Become Running Agents](/posts/2026-06-16-agent-factory-declarative-primitives.html)*
