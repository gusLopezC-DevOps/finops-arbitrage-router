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

The router also exposes `/metrics` on the same port as the API (no extra Service,
port or NetworkPolicy needed). See [Metrics](#metrics).

## Routing policy

1. **cost-optimized** — traffic is pinned to the local Ollama CPU model
   (`llama3.2:1b`). No internet egress is required.
2. **max-performance** — when the local model cannot meet the latency SLA and a
   `fallback-endpoint` is configured, the router sends the request to the
   managed OpenAI-compatible endpoint.

## Unit economics

The router reports **tokens and which leg answered** as Prometheus metrics, which
is what you need to answer "how much of this traffic was free?". What it does
**not** report is money: the router never learns which model the managed gateway
picked, so it cannot turn tokens into a price. `configmap-sla.yaml` still
declares `cost.max_usd_per_1k_tokens` and `latency.max_p95_ms`, but `router.py`
does not read that file — treat those values as a declaration of intent, not as
an enforced budget. Wiring them up is the obvious next step, and it needs a price
table that lives outside the router.

## Metrics

`GET /metrics` on the router port, in Prometheus text exposition format, written
with the standard library only. A new dependency would have meant a new image
build, and the code ships in a ConfigMap precisely so that the router stays
dependency-free. Cardinality is bounded: every label value comes from the
router's own routing decision, never from client input, and no prompt, model
name or credential is ever exported.

| Metric | Type | Labels |
|---|---|---|
| `router_up` | gauge | — |
| `router_requests_total` | counter | `leg`, `status_class` |
| `router_tokens_total` | counter | `leg`, `direction` |
| `router_fallbacks_total` | counter | `from_leg`, `to_leg`, `reason` |
| `router_upstream_errors_total` | counter | `leg`, `kind` (`http_500`, `TimeoutError`, …) |
| `router_request_duration_seconds` | histogram | `leg` (includes failed attempts) |

```promql
# share of traffic served free, on the local model
sum(rate(router_requests_total{leg="local"}[5m]))   / sum(rate(router_requests_total[5m]))
# share that leaves for a paid endpoint
sum(rate(router_requests_total{leg="managed"}[5m])) / sum(rate(router_requests_total[5m]))
# how often a leg had to rescue the other one
sum(rate(router_fallbacks_total[5m]))               / sum(rate(router_requests_total[5m]))
# p95 latency per leg
histogram_quantile(0.95, sum by (le,leg)(rate(router_request_duration_seconds_bucket[5m])))
# token burn per leg
sum by (leg,direction)(rate(router_tokens_total[5m]))
```

The histogram deliberately observes **every** upstream attempt, including the ones
that fail, so a slow primary leg shows up in its own latency before the fallback
hides it from the client.

## Rollout

`configmap-router.yaml` is mounted as a volume, so editing `router.py` does not
by itself restart the pod: the process would keep serving the code it loaded at
boot. `scripts/gen_checksum.py` hashes both ConfigMaps and writes the result to
the `backstage.guslopez.dev/config-checksum` annotation on the Deployment, so any
router change rolls out through Argo CD with no manual restart:

```bash
python3 scripts/gen_checksum.py   # after editing configmap-router.yaml
```

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

> LAN-only by design: OmniRoute lives only on the local network. There is no
> public DNS, no TLS/cert-manager and no exposure outside the LAN — both
> `finops.local` and `omniroute-internal.local` resolve via `/etc/hosts` on
> LAN nodes (`192.168.100.77`, the Kong MetalLB IP) plus `hostAliases` in the
> router pods. Do NOT add a public Ingress/TLS for the fallback; the SLA
> (`configmap-sla.yaml`, `managed_fallback.scope`) pins this down.

Per-request priority: clients send `X-Router-Priority: max-performance`
(or a `"priority"` body field) to take the fallback leg; otherwise the
Deployment default (`cost-optimized`) applies. The fallback bearer key lives
in the SealedSecret `finops-arbitrage-router-fallback` (never in clear).

Model resolution is owned by the router: the local leg defaults to
`LOCAL_MODEL`, and the fallback leg defaults to `FALLBACK_MODEL`
(`auto/best-chat`) whenever the caller omits the model or sends a local model
name such as `llama3.2:1b`, which the managed gateway cannot resolve. A
provider-prefixed model (`groq/openai/gpt-oss-20b`) is forwarded untouched.

## Cross-leg fallback

The router never leaves a caller with a 502 just because one leg is down: when
the primary leg fails it retries **once** on the other one. Each fallback is
counted in `router_fallbacks_total`.

| Setting | Default | Meaning |
|---|---|---|
| `CROSS_LEG_FALLBACK` | `true` | Retry on the other leg. Set to `false` to fail closed |
| `UPSTREAM_TIMEOUT` | `120` | Per-leg timeout in seconds |
| `X-Router-Leg` (response) | `managed` \| `local` \| `none` | Which leg answered |
| `X-Router-Fallback` (response) | `true` \| `false` | Whether it was the backup leg |

Retryable: HTTP 408, 425, 429, any 5xx, and transport failures (timeout,
connection refused, reset). **Not** retryable: any other 4xx — a 400 or 404 is
the caller's own mistake, and retrying the other leg would only turn a clear
error into an opaque 502. When both legs fail, the 502 body names both
failures and the reason each leg was skipped.

Responses are always OpenAI-shaped, whichever leg answers: the local Ollama
reply is normalized to `choices[].message` with `usage` and `finish_reason`, so
`llm-microservice` (an OpenAI-compatible facade) can pass it through unchanged.

The local leg never sends credentials and always uses `LOCAL_MODEL`, even if
the caller asked for a provider-prefixed model that only exists upstream.

Tests: `python3 tests/test_router_fallback.py` (stdlib only, 27 cases, no
cluster needed).

> Platform note: Argo CD excludes endpoint resources (`Endpoints` and
> `EndpointSlice`) from management, so `omniroute-upstream-1` (EndpointSlice
> pinning the Service to `192.168.100.50:20128`) is applied out-of-band
> (`kubectl apply -f omniroute-upstream.yaml` for that object only). The YAML
> stays in Git as source of truth; re-apply it if the slice is ever deleted.
