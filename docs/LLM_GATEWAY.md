# LLM Gateway

`GTAIF/gateway/router.py` — `LLMGateway`

---

## The Problem We Solved

In the early iteration of the AI platform, each agent was hardwired to a single LLM provider. When that provider had an outage, rate-limited us, or ran out of credits, the entire agent stopped working. Switching providers meant code changes and redeployment. Costs were unpredictable because we defaulted to the most capable (and most expensive) model for every task — a simple routing decision used the same model as deep reasoning.

We needed three things:

1. **Automatic failover** — if Provider A fails, move to Provider B without any human action
2. **Role-based model selection** — cheap fast models for routing; expensive capable models for deep thinking
3. **Predictable failure** — surface a clear error when all providers are exhausted, never silently return garbage

---

## Current Provider Strategy

**Two providers only: NVIDIA NIM and Anthropic.**

Google, OpenAI, and NATS/Ollama have been removed from the active chain. The comment in `_build_providers()` explains how to add them back when needed.

### Priority by role

| Role group | Provider 1 (primary) | Provider 2 (fallback) |
|---|---|---|
| **THINKER** | Anthropic (Sonnet 4.5 → Haiku 4.5) | NVIDIA (thinker model set) |
| **All other roles** | NVIDIA (role-specific model set) | Anthropic (role-specific model set) |
| **EMBEDDER** | NVIDIA only | *(Anthropic has no embedding API)* |

### Why this order?

**THINKER reversal**: Deep reasoning, ReAct chains, and code generation are where model quality matters most. Claude Sonnet consistently outperforms NVIDIA's available models on these tasks. NVIDIA is an excellent fallback but shouldn't be primary for reasoning-heavy work.

**NVIDIA first for everything else**: NVIDIA NIM offers 100+ free models at `build.nvidia.com` with generous rate limits. Fast, free, and high quality for routing / planning / execution tasks.

**Anthropic as role-aware fallback**: Each role has its own Anthropic model list in `ANTHROPIC_ROLE_MODELS`. Roles that need quality (EVALUATOR, PLANNER, HITL) fall back to Sonnet; roles that need speed and determinism (ROUTER, VALIDATOR, CACHE_MANAGER) fall back to Haiku. Model lists live in `gateway/providers/anthropic_provider.py`.

---

## All Roles

```python
class LLMRole(str, Enum):
    # Core
    ROUTER         = "router"          # classify/route a message (1 keyword output)
    PLANNER        = "planner"         # break task into steps
    THINKER        = "thinker"         # deep reasoning, ReAct, code
    EXECUTOR       = "executor"        # act on a plan step
    EMBEDDER       = "embedder"        # dense vector embeddings

    # Specialised
    RETRIEVER      = "retriever"       # query reformulation for RAG
    VALIDATOR      = "validator"       # binary approve/reject constraint check
    EVALUATOR      = "evaluator"       # quality evaluation with rubric
    MEMORY_MANAGER = "memory_manager"  # memory consolidation / key extraction
    HITL           = "hitl"            # human-readable summary before interrupt
    CACHE_MANAGER  = "cache_manager"   # cache key generation
```

All specialised roles follow the **NVIDIA first → Anthropic fallback** pattern. The specific Anthropic model used as the fallback varies by role — see the Anthropic Sub-Chain section below.

---

## Role Settings

Temperature and token budget are baked into the chain at build time, not passed per-request. This prevents accidental misconfigurations where a ROUTER might generate a 4096-token essay.

| Role | Temperature | Max tokens | Rationale |
|---|---|---|---|
| `router` | 0.0 | 32 | One keyword only — physically cannot write more |
| `planner` | 0.2 | 4096 | Low creativity, structured output |
| `executor` | 0.3 | 4096 | Slightly creative for tool use |
| `thinker` | 0.4 | 8192 | Needs room for reasoning chains |
| `embedder` | 0.0 | 256 | Deterministic, short |
| `retriever` | 0.0 | 512 | Deterministic query rephrase |
| `validator` | 0.0 | 128 | Binary approve/reject — must be deterministic |
| `evaluator` | 0.1 | 2048 | Slight warmth for nuanced rubric scoring |
| `memory_manager` | 0.0 | 1024 | Deterministic key extraction |
| `hitl` | 0.1 | 512 | Human-readable summary |
| `cache_manager` | 0.0 | 64 | Deterministic key generation |

---

## Retry Behaviour

```python
def _retried(runnable, attempts=2):
    return runnable.with_retry(
        stop_after_attempt      = attempts,
        wait_exponential_jitter = True,
    )
```

`attempts=2` means **one retry** per provider with exponential jitter (~1s wait). The retry is applied to each NVIDIA model in the sub-chain and to each Anthropic call. Once all retries for a provider are exhausted, the loop moves to the next provider.

If **all providers fail**, a `GatewayError` is raised with a clear message pointing to the relevant log entries. Callers are expected to catch this and write to `state["error"]`.

```python
raise GatewayError(
    f"All providers failed for role '{r.value}'. "
    "Check NVIDIA / Anthropic API keys and rate limits."
)
```

---

## NVIDIA Sub-Chain

NVIDIA NIM exposes many models for each role. They are tried in priority order using LangChain's `.with_fallbacks()`. The model lists live in `gateway/providers/nvidia_provider.py` (`NVIDIA_ROLE_MODELS`).

```python
def _nvidia_chain(self, role_key, temperature, max_tokens):
    models = NVIDIA_ROLE_MODELS[role_key]  # e.g. ["meta/llama-3.1-8b-instruct", ...]
    chains = [_nvidia(m, temperature, max_tokens) for m in models]
    return chains[0].with_fallbacks(chains[1:])   # LangChain handles ordering
```

---

## Anthropic Sub-Chain

The Anthropic provider mirrors the NVIDIA structure: a dict of role → model list and a chain builder function. Both live in `gateway/providers/anthropic_provider.py`.

### `ANTHROPIC_ROLE_MODELS`

```python
ANTHROPIC_ROLE_MODELS: dict[str, list[str]] = {
    # Anthropic is PRIMARY for THINKER — Sonnet leads
    "thinker":        ["claude-sonnet-4-5-20250929", "claude-haiku-4-5-20251001"],

    # Fallback roles that benefit from Sonnet quality
    "planner":        ["claude-sonnet-4-5-20250929", "claude-haiku-4-5-20251001"],
    "evaluator":      ["claude-sonnet-4-5-20250929", "claude-haiku-4-5-20251001"],
    "hitl":           ["claude-sonnet-4-5-20250929", "claude-haiku-4-5-20251001"],

    # Fallback roles where Haiku is fast enough
    "executor":       ["claude-haiku-4-5-20251001", "claude-sonnet-4-5-20250929"],
    "retriever":      ["claude-haiku-4-5-20251001", "claude-sonnet-4-5-20250929"],

    # Deterministic single-model roles — Haiku only
    "router":         ["claude-haiku-4-5-20251001"],
    "validator":      ["claude-haiku-4-5-20251001"],
    "memory_manager": ["claude-haiku-4-5-20251001"],
    "cache_manager":  ["claude-haiku-4-5-20251001"],
}
```

### `build_anthropic_chain()`

```python
def build_anthropic_chain(role_key, api_key, max_tokens, temperature):
    models = ANTHROPIC_ROLE_MODELS.get(role_key, ["claude-haiku-4-5-20251001"])
    chains = [_make_runnable(m, api_key, max_tokens, temperature) for m in models]
    return chains[0].with_fallbacks(chains[1:])   # same pattern as NVIDIA
```

`_build_providers()` in `router.py` calls this:

```python
# THINKER (Anthropic primary)
providers.append(("anthropic", build_anthropic_chain("thinker", ant, max_tok, temp)))

# All other roles (Anthropic fallback)
providers.append(("anthropic", build_anthropic_chain(role.value, ant, max_tok, temp)))
```

### SSL bypass

`ChatAnthropic` (LangChain) inherits Python's default SSL verification, which fails behind the corporate SSL-inspection proxy inside the container. The provider wraps each model in a `RunnableLambda` that calls the Anthropic SDK directly with a shared `httpx.AsyncClient(verify=False, timeout=120)`. This is invisible to the gateway's `ainvoke` / `astream` loops.

### Refreshing the model list

```bash
python -c "
import anthropic, httpx
c = anthropic.Anthropic(api_key='YOUR_KEY', http_client=httpx.Client(verify=False))
for m in c.models.list(): print(m.id)
"
```

Update `ANTHROPIC_ROLE_MODELS` in `anthropic_provider.py` after running this. No changes to `router.py` needed.

---

## Public API

### `ainvoke()` — single response

```python
response = await gateway.ainvoke(messages, role=LLMRole.THINKER)
# response.content       — str
# response.input_tokens  — int
# response.output_tokens — int
# response.model         — str (model name from provider)
# response.provider      — str ("anthropic", "nvidia")
```

### `astream()` — token streaming

```python
async for chunk in gateway.astream(messages, role=LLMRole.EXECUTOR):
    print(chunk, end="", flush=True)
```

`astream()` tries providers in the same priority order as `ainvoke()`. On provider failure mid-stream it advances to the next provider (starting fresh, not resuming).

### `route()` — convenience shortcut

```python
label = await gateway.route(messages)   # returns stripped lowercase string
```

Uses `LLMRole.ROUTER` (temperature=0, max_tokens=32).

### `embed()` — dense vectors

```python
vectors = await gateway.embed("some text", input_type="query")
# returns list[list[float]], each inner list is 1024-dimensional
```

Or batch:
```python
vectors = await gateway.embed(["text one", "text two"], input_type="passage")
```

`input_type` is passed through to NVIDIA NIM (`"query"` for search queries, `"passage"` for documents being indexed).

If NVIDIA is unavailable, `embed()` logs an error and returns **zero vectors** (never crashes callers). MongoDB vector indexes use **1024 dimensions** to match NVIDIA NIM output.

---

## Embedding Vector Dimensions

```
NVIDIA nvidia/nv-embedqa-e5-v5 → 1024 dimensions
```

MongoDB vector indexes (`SemanticCache`, `Documents`, `EpisodicMemory`, `SemanticMemory`, `ProceduralMemory`) are all sized to 1024. If migrating from an old deployment that used `nomic-embed-text` (768 dims), those collections must be dropped and repopulated before queries will work.

---

## Chain Construction (Lazy)

Provider lists are built once per role per process lifetime:

```python
def _get_providers(self, role):
    if role not in self._providers:
        self._providers[role] = self._build_providers(role)
    return self._providers[role]
```

`self._providers` is a `dict[LLMRole, list[tuple[str, Any]]]`. Each entry is a `(label, chain)` pair. The `ainvoke` / `astream` loops iterate this list and try each chain in order.

---

## NATS/Ollama — Not Currently Active

NATS/Ollama (the on-premise GPU fallback via GTLLM) has been **removed from the active provider chain** while GTLLM integration is stabilised. The `nats_client` attribute is retained on `LLMGateway` for API compatibility with `main.py`. To re-enable:

1. Add a `NATSProvider` runnable to `gateway/providers/`
2. Append it to the `providers` list in `_build_providers()` with a `if self._nats_client:` guard
3. That's it — the `ainvoke` / `astream` loop picks it up automatically

---

## Adding a New Provider (e.g. OpenAI)

```python
# In _build_providers():
if settings.openai_api_key:
    from langchain_openai import ChatOpenAI
    openai_chain = _retried(ChatOpenAI(
        model       = "gpt-4.1-mini",
        api_key     = settings.openai_api_key,
        max_tokens  = max_tok,
        temperature = temp,
    ))
    providers.append(("openai", openai_chain))
```

Position in the list determines priority. Append after Haiku to make it a tertiary fallback, or insert before to promote it.
