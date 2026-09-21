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

## OmniRoute fallback (max-performance leg)

The managed fallback is OmniRoute (`192.168.100.50:20128`), registered in Kong
as the internal hostname `omniroute-internal.local` (`omniroute-upstream.yaml`:
Service + Ingress class `kong`). The router reaches it only through Kong, so
access logs, observability and zero-trust keep applying; the egress
NetworkPolicy allows just `Kong-LB:80` (no public internet).

Per-request priority: clients send `X-Router-Priority: max-performance`
(or a `"priority"` body field) to take the fallback leg; otherwise the
Deployment default (`cost-optimized`) applies. The fallback bearer key lives
in the SealedSecret `finops-arbitrage-router-fallback` (never in clear).

> Platform note: Argo CD excludes endpoint resources (`Endpoints` and
> `EndpointSlice`) from management, so `omniroute-upstream-1` (EndpointSlice
> pinning the Service to `192.168.100.50:20128`) is applied out-of-band
> (`kubectl apply -f omniroute-upstream.yaml` for that object only). The YAML
> stays in Git as source of truth; re-apply it if the slice is ever deleted.
