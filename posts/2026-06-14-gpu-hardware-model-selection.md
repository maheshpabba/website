---
title: "GPU Hardware and Model Selection for Enterprise AI: The Math Nobody Shows You"
date: 2026-06-14
tags: Enterprise AI, GPU, NVIDIA, NVAIE, Model Selection, Hardware, Distributed Inference, H100
excerpt: How many GPUs do you actually need? The vendor answer is "buy more." The honest answer requires math — model memory requirements, concurrent user throughput, latency SLAs, and a tiered model strategy that can cut your GPU footprint by 60–70% compared to the naive approach.
image: assets/images/blog/covers/AI-Infrastructure.png
---

# GPU Hardware and Model Selection for Enterprise AI: The Math Nobody Shows You

Every enterprise AI conversation eventually arrives at the same question: *"How many GPUs do we need?"*

I've been asked this more times than I can count. The vendor answer is always some version of "depends on your workload — let's start with a pilot." The consultant answer is a spreadsheet with 40 variables and a wide confidence interval. Neither is useful when you're in a budget meeting trying to justify hardware spend.

This post is my answer — the actual math, the model selection tradeoffs, and the tiered strategy that consistently reduces GPU requirements by 60–70% compared to what most teams initially plan.

---

## Start With the Model Memory Requirement

Before you can size anything, you need to know how much GPU memory your model requires. This is the hard floor — you cannot run inference without first fitting the model weights into VRAM.

The rule of thumb for **FP16 (half precision)**:

```
GPU memory for weights = model parameters × 2 bytes

Examples:
  7B  parameters → ~14 GB   (fits in 1× H100 80GB with room for KV cache)
  13B parameters → ~26 GB   (fits in 1× H100 80GB)
  34B parameters → ~68 GB   (fits in 1× H100 80GB, tight)
  70B parameters → ~140 GB  (requires 2× H100 80GB — tensor parallel)
 405B parameters → ~810 GB  (requires 10–12× H100 — pipeline parallel)
```

But weights are only part of the story. During inference, every active request occupies **KV (key-value) cache** — memory proportional to the context length and batch size. For a 70B model serving 10 concurrent requests with 8K context windows, KV cache adds another 20–40 GB on top of the weight footprint.

**Practical sizing rule: multiply your weight footprint by 1.3–1.5 for KV cache overhead, then round up to the nearest GPU count.**

---

## The Tiered Model Strategy

Here's the single most impactful decision in your GPU sizing: **do not run one model for everything.**

Most teams, when they first deploy, pick their most capable model — a 70B or larger — and route everything through it. This feels safe. It is expensive and wasteful.

The correct approach matches model capability to task complexity, exactly as the LLM Gateway does at the software layer. On the hardware side, this means running multiple model tiers simultaneously:

| Tier | Model Size | Use Case | GPU Requirement |
|---|---|---|---|
| Tier 1 — Fast/Cheap | 7–8B | Routing, classification, validation, cache key generation | 1× H100 serves ~200 concurrent requests |
| Tier 2 — Balanced | 13–34B | Planning, execution, retrieval, summarization | 1× H100 serves ~50 concurrent requests |
| Tier 3 — Capable | 70B | Deep reasoning, code generation, complex analysis | 2× H100 per replica, ~20 concurrent requests |
| Tier 4 — Maximum | 405B+ | Advanced research, frontier reasoning | 10–12× H100, pipeline parallel |

When you profile actual enterprise workloads, you typically find:
- ~50% of requests are Tier 1 tasks
- ~35% are Tier 2
- ~13% are Tier 3
- ~2% or fewer need Tier 4

Compare these two hardware plans for 500 concurrent enterprise users:

**Naive plan (single 70B model for everything):**
- 500 concurrent users ÷ 20 requests/replica = 25 replicas
- 25 replicas × 2× H100 = 50× H100 80GB cards

**Tiered plan (3 tiers, traffic split by LLM Gateway):**
- Tier 1 (50% traffic = 250 users): 250 ÷ 200 = 2× H100
- Tier 2 (35% traffic = 175 users): 175 ÷ 50 = 4× H100
- Tier 3 (13% traffic = 65 users): 65 ÷ 20 = 7× H100 (rounded to pairs = 8×)
- Total: **14× H100 80GB cards**

That's a 72% reduction in GPU hardware — from 50 cards to 14 — for the same user load. The software layer (LLM Gateway with role-based routing) is doing the work, but it only delivers value when the hardware is sized to match.

---

## NVIDIA AI Enterprise (NVAIE): Running Models On-Prem

For enterprise customers, I always recommend **NVIDIA AI Enterprise (NVAIE)** over cloud APIs for production workloads. Here's why it matters and how it works.

NVAIE is a software subscription from NVIDIA that includes:
- **NVIDIA NIM** (Inference Microservices) — optimized, containerized model servers for all major open-weight models
- **NVIDIA AI Workbench** — developer environment management
- **TensorRT-LLM** — NVIDIA's inference optimization engine, pre-applied in NIM containers
- **Support and SLA** from NVIDIA for the entire AI software stack

The NIM containers are the key piece. They are the exact same models available via the NVIDIA cloud API — same model weights, same API contract — but running inside your data center on your own hardware. Your prompts never leave your network.

### NIM Container Deployment on OpenShift

```yaml
# NIM deployment for a 70B model — 2× H100 required
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nim-llama3-70b
  namespace: ai-inference
spec:
  replicas: 2          # 2 replicas → 4× H100 total
  selector:
    matchLabels:
      app: nim-llama3-70b
  template:
    spec:
      nodeSelector:
        nvidia.com/gpu.product: NVIDIA-H100-SXM5-80GB
      tolerations:
        - key: nvidia.com/gpu
          operator: Exists
          effect: NoSchedule
      containers:
        - name: nim
          image: nvcr.io/nim/meta/llama-3.1-70b-instruct:latest
          env:
            - name: NGC_API_KEY
              valueFrom:
                secretKeyRef:
                  name: ngc-credentials
                  key: api_key
          resources:
            limits:
              nvidia.com/gpu: "2"    # tensor parallel across 2 GPUs
          ports:
            - containerPort: 8000
          readinessProbe:
            httpGet:
              path: /v1/health/ready
              port: 8000
            initialDelaySeconds: 120   # model loading time
            periodSeconds: 10
```

The NIM container handles everything: model download, TensorRT-LLM optimization, multi-GPU tensor parallelism, and the OpenAI-compatible API endpoint. Your LLM Gateway points at `http://nim-llama3-70b:8000/v1` instead of `api.nvidia.com`, and the rest of the stack works identically.

---

## Sizing for Distributed Inference

When models exceed a single node's GPU capacity — 405B models, or configurations requiring 8+ GPUs — you need distributed inference across multiple servers.

**Two strategies:**

**Tensor Parallelism** — splits a single model layer across multiple GPUs. All GPUs must communicate for every forward pass. Requires ultra-low latency interconnect (NVLink within a node, or InfiniBand/RoCE v2 between nodes). NIM handles this automatically when you specify `nvidia.com/gpu: "8"` in the pod spec — it detects the topology and shards accordingly.

**Pipeline Parallelism** — assigns different model layers to different GPUs. Each GPU processes its layers and passes activations to the next. Lower interconnect bandwidth requirement than tensor parallel, but higher latency per token. Used for very large models (405B+) when you don't have NVLink between all GPUs.

For the Cisco UCS infrastructure described in the [infrastructure post](/posts/2026-05-20-ai-clusters-cisco-ucs.html), NVLink within a node handles within-node tensor parallelism at 900 GB/s. For cross-node parallelism (models spanning multiple servers), the 400GbE RoCE v2 fabric handles the gradient and activation exchange with lossless RDMA.

### When to Use Which

| Scenario | Strategy | Network Requirement |
|---|---|---|
| 70B model, 2× H100 same server | Tensor parallel (NVLink) | NVLink (in-node) |
| 70B model, 2× H100 different servers | Tensor parallel (RDMA) | 400GbE RoCE v2 |
| 405B model, 8–12× H100 | Hybrid tensor+pipeline | 400GbE RoCE v2 minimum |
| Training (all sizes) | Data parallel + tensor parallel | RoCE v2, lossless |

---

## GPU Selection: H100 SXM5 vs H100 PCIe vs A100

Not all H100s are equal. The SXM5 and PCIe variants have very different performance profiles for inference and training.

| Variant | GPU Memory | Within-Node Bandwidth | NVSwitch | Best For |
|---|---|---|---|---|
| H100 SXM5 80GB | 80 GB HBM3 | 900 GB/s (NVLink 4.0) | Yes (full all-to-all) | Training, large model inference |
| H100 NVL 94GB | 94 GB HBM3e | 900 GB/s (NVLink 4.0) | Yes | Large model inference, high memory |
| H100 PCIe 80GB | 80 GB HBM3 | 128 GB/s (PCIe Gen5) | No | Inference, lower-density deployments |
| A100 SXM4 80GB | 80 GB HBM2e | 600 GB/s (NVLink 3.0) | Yes | Training, older generation |

**Critical distinction**: The H100 PCIe does not have NVSwitch. Multiple PCIe H100s in the same server communicate through the CPU's PCIe bus, not through NVLink. For a 70B model split across two PCIe H100s, inter-GPU bandwidth drops from 900 GB/s to ~128 GB/s — a 7× reduction that directly impacts throughput.

For enterprise training and large model inference, the SXM5 form factor in a Cisco UCS X210c M7 or C245 M8 is the right choice. For cost-optimized inference of mid-size models where each GPU serves its own model independently, PCIe is acceptable.

---

## The Getraind Reality

Getraind doesn't run any of this. I use NVIDIA NIM cloud APIs and Anthropic Claude APIs because I don't have H100 hardware available to me personally. That's not a compromise I apologize for — it's the pragmatic starting point that lets you build and validate the entire software stack before committing to hardware spend.

The tiered model strategy still applies in Getraind — different roles call different NIM models via the cloud API. The cost savings are real even at cloud API pricing. But for a production enterprise deployment where data sovereignty and latency SLAs matter, on-prem NVAIE is the path.

The architecture you build during the cloud API phase is identical to what you'd run on-prem. When you're ready to bring it local, you change the base URL in the LLM Gateway configuration. That's it.

For the **training and fine-tuning side** — LoRA adapters on domain-specific data, full parameter fine-tuning, synthetic dataset generation — the GPU sizing math is different (training is memory-bandwidth bound in ways inference is not). My open-source **[AI Lab training notebooks](https://github.com/maheshpabba/aitraining)** cover this hands-on: LoRA fine-tuning on Intersight domain data, GPT-2 full fine-tune, and building a transformer from scratch. Good starting point before you commit to training hardware spend.

---

## Summary: Hardware Sizing Checklist

Before you order a single GPU:

1. **Profile your workload** — what percentage of requests are routing/validation (Tier 1) vs. reasoning (Tier 3)?
2. **Calculate model memory requirements** — weights × 2 bytes + 30–50% for KV cache
3. **Determine concurrency target** — peak simultaneous users, not average
4. **Design the tier strategy** — how many models, which sizes, what split?
5. **Size each tier independently** — concurrent users per tier ÷ requests per GPU per tier
6. **Add 20% headroom** — for bursty traffic, rolling updates, and model loading time
7. **Choose SXM5 for training and large inference** — PCIe only for cost-optimized mid-size inference

The math isn't complicated once you have the right inputs. The mistake most teams make is skipping the tier strategy and pricing everything at Tier 3 capacity. Don't do that.

> **Cisco Validated Design**: I co-authored the CVD that puts everything in this post into a validated, production-ready reference architecture: [*FlashStack for Enterprise RAG Pipeline with NVIDIA NIM, NIM Operator, and RAG Blueprint*](https://www.cisco.com/c/en/us/td/docs/unified_computing/ucs/UCS_CVDs/flashstack_rag_nim.html) — Cisco UCS X-Series, Pure Storage, NVAIE, NIM Operator, RAG Blueprint, benchmarked on OpenShift. Published May 2025.

*Next: [Designing Enterprise AI: From Business Problem to Agent Architecture](/posts/2026-06-15-enterprise-ai-design.html)*
