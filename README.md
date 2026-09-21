# FinOps Arbitrage Router

Declarative cost/latency routing for LLM traffic. Instead of requesting a GPU,
a team declares an **SLA** and the platform chooses the cheapest execution path
that satisfies it.

## What this template creates

| Resource | Purpose |
|---|---|
| `ConfigMap/<name>-sla` | Declared cost (`USD/1k tokens`) and latency (`p95 ms`) SLA + routing policy |
| `ConfigMap/<name>-router` | The router code (`router.py`), stdlib-only |
| `Deployment/<name>` + `Service/<name>` | The arbitrage router (probes, resources and labels per the golden path) |
| `Ingress/<name>` | Kong entrypoint, with the platform DLP plugins (`pii-redaction`, `ai-prompt-guard`) |
| `NetworkPolicy/<name>-egress` | Zero-trust egress: DNS, local Ollama, and internet only for the managed fallback |

## Routing policy

1. **cost-optimized** — traffic is pinned to the local Ollama CPU model
   (`llama3.2:1b`). No internet egress is required.
2. **max-performance** — when the local model cannot meet the latency SLA and a
   `fallback-endpoint` is configured, the router sends the request to the
   managed OpenAI-compatible endpoint.

## Unit economics

Cost is attributed per request using the model, token counts and the declared
SLA. Dashboards live in Grafana (OpenCost + Langfuse token usage), which lets
FinOps charge back to the owning team's cost centre.

## Roadmap (not installed in this cluster)

- **GPU spot arbitrage**: when queue depth (KEDA) exceeds a threshold, ask
  Crossplane to provision a spot GPU node and route to it. This requires the
  Crossplane cloud provider (AWS/GCP) and KEDA, which are **not** part of the
  current platform. The `gpu_spot` block in the SLA ConfigMap is intentionally
  `enabled: false` until those dependencies exist.
