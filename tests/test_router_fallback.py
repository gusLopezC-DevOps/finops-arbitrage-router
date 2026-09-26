"""Unit tests for the router's cross-leg fallback.

Runs the real router.py that lives inside configmap-router.yaml, with
urllib.request.urlopen stubbed: no cluster, no PyYAML, no network.

    python3 tests/test_router_fallback.py
"""

import io
import json
import os
import sys
import types
import unittest
import urllib.error
from email.message import Message
from http.server import HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
CONFIGMAP = ROOT / "configmap-router.yaml"
MANAGED = {"name": "managed", "url": "http://omniroute-internal.local/v1/chat/completions",
           "fallback": True}
LOCAL = {"name": "local", "url": "http://ollama-cpu.ml-serving.svc.cluster.local:11434/api/chat",
         "fallback": False}
OLLAMA_REPLY = {
    "model": "llama3.2:1b",
    "message": {"role": "assistant", "content": "respuesta local"},
    "done": True,
    "done_reason": "stop",
    "prompt_eval_count": 11,
    "eval_count": 7,
}
OPENAI_REPLY = {
    "id": "chatcmpl-managed",
    "object": "chat.completion",
    "model": "nemotron-3-ultra",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "respuesta gestionada"},
                 "finish_reason": "stop"}],
}
BASE_ENV = {
    "ROUTER_PRIORITY": "max-performance",
    "LOCAL_ENDPOINT": "http://ollama-cpu.ml-serving.svc.cluster.local:11434",
    "LOCAL_MODEL": "llama3.2:1b",
    "FALLBACK_ENDPOINT": "http://omniroute-internal.local",
    "FALLBACK_MODEL": "auto/best-chat",
    "FALLBACK_API_KEY": "secreto",
    "CROSS_LEG_FALLBACK": "true",
    "UPSTREAM_TIMEOUT": "5",
}


def load_router(env=None):
    """Exec the router.py embedded in the ConfigMap without starting the server."""
    lines = CONFIGMAP.read_text().splitlines()
    start = lines.index("  router.py: |") + 1
    code = []
    for line in lines[start:]:
        if line.strip() and not line.startswith("    "):
            break
        code.append(line[4:] if line.startswith("    ") else "")
    source = "\n".join(code).strip() + "\n"

    module = types.ModuleType("router_under_test")
    module.__dict__["__name__"] = "router_under_test"
    inert = types.SimpleNamespace(serve_forever=lambda: None)
    with mock.patch.dict(os.environ, dict(BASE_ENV, **(env or {}))):
        with mock.patch.object(HTTPServer, "__new__", lambda *a, **k: inert):
            exec(compile(source, "router.py", "exec"), module.__dict__)
    return module


class Reply:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def upstream_error(code, detail=b'{"error":"upstream"}'):
    return urllib.error.HTTPError("http://upstream", code, "error", Message(), io.BytesIO(detail))


def post(module, body, headers=None):
    """Drive one POST through the handler and return (status, headers, body)."""
    handler = module.Handler.__new__(module.Handler)
    handler.request_version = "HTTP/1.1"
    handler.command = "POST"
    handler.path = "/v1/chat/completions"
    handler.requestline = "POST /v1/chat/completions HTTP/1.1"
    handler.client_address = ("127.0.0.1", 40000)
    handler.server = None
    message = Message()
    message["Content-Length"] = str(len(body))
    for key, value in (headers or {}).items():
        message[key] = value
    handler.headers = message
    handler.rfile = io.BytesIO(body.encode() if isinstance(body, str) else body)
    handler.wfile = io.BytesIO()
    handler.do_POST()

    raw = handler.wfile.getvalue()
    head, _, payload = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = Message()
    for line in lines[1:]:
        key, _, value = line.partition(":")
        headers[key.strip()] = value.strip()
    return int(lines[0].split()[1]), headers, json.loads(payload or b"{}")


def get(module, path):
    """Drive one GET through the handler and return (status, headers, body)."""
    handler = module.Handler.__new__(module.Handler)
    handler.request_version = "HTTP/1.1"
    handler.command = "GET"
    handler.path = path
    handler.requestline = "GET %s HTTP/1.1" % path
    handler.client_address = ("127.0.0.1", 40000)
    handler.server = None
    handler.headers = Message()
    handler.rfile = io.BytesIO(b"")
    handler.wfile = io.BytesIO()
    handler.do_GET()

    raw = handler.wfile.getvalue()
    head, _, payload = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = Message()
    for line in lines[1:]:
        key, _, value = line.partition(":")
        headers[key.strip()] = value.strip()
    return int(lines[0].split()[1]), headers, payload.decode()


def parse_exposition(text):
    """Minimal Prometheus text parser: {series: value}, HELP/TYPE counted."""
    series, declared = {}, {}
    for line in text.splitlines():
        if line.startswith("# HELP "):
            name = line.split()[2]
            declared.setdefault(name, set()).add("HELP")
        elif line.startswith("# TYPE "):
            name = line.split()[2]
            declared.setdefault(name, set()).add("TYPE")
        elif line.strip():
            key, _, value = line.rpartition(" ")
            series[key] = float(value)
    return series, declared


def stub_upstream(module, behaviours):
    """Return the list of upstream calls made, stubbed per `behaviours`."""
    calls = []
    queue = list(behaviours)

    def fake_urlopen(request, timeout=None):
        payload = json.loads(request.data)
        calls.append({"url": request.full_url, "body": payload,
                      "auth": request.headers.get("Authorization"),
                      "ua": request.headers.get("User-agent"), "timeout": timeout})
        behaviour = queue.pop(0)
        if isinstance(behaviour, Exception):
            raise behaviour
        return Reply(behaviour)

    module.urllib.request.urlopen = fake_urlopen
    return calls


class FallbackTest(unittest.TestCase):
    def test_managed_happy_path_is_untouched(self):
        module = load_router()
        calls = stub_upstream(module, [OPENAI_REPLY])
        status, headers, body = post(module, json.dumps({"messages": [{"role": "user", "content": "hola"}]}))
        self.assertEqual(status, 200)
        self.assertEqual(body, OPENAI_REPLY)
        self.assertEqual(headers.get("X-Router-Leg"), "managed")
        self.assertEqual(headers.get("X-Router-Fallback"), "false")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["url"], MANAGED["url"])
        self.assertEqual(calls[0]["body"]["model"], "auto/best-chat")
        self.assertEqual(calls[0]["auth"], "Bearer secreto")
        self.assertEqual(calls[0]["ua"], "finops-arbitrage-router/1.0")
        self.assertEqual(calls[0]["timeout"], 5.0)

    def test_provider_prefixed_model_is_forwarded_untouched(self):
        module = load_router()
        calls = stub_upstream(module, [OPENAI_REPLY])
        post(module, json.dumps({"model": "groq/openai/gpt-oss-20b", "messages": []}))
        self.assertEqual(calls[0]["body"]["model"], "groq/openai/gpt-oss-20b")

    def test_managed_5xx_falls_back_to_local_as_openai(self):
        module = load_router()
        calls = stub_upstream(module, [upstream_error(500), OLLAMA_REPLY])
        status, headers, body = post(module, json.dumps({"messages": []}))
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Router-Leg"), "local")
        self.assertEqual(headers.get("X-Router-Fallback"), "true")
        self.assertEqual(body["object"], "chat.completion")
        self.assertEqual(body["model"], "llama3.2:1b")
        self.assertEqual(body["choices"][0]["message"]["content"], "respuesta local")
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")
        self.assertEqual(body["usage"], {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["url"], LOCAL["url"])
        self.assertEqual(calls[1]["body"]["model"], "llama3.2:1b")
        self.assertIsNone(calls[1]["auth"])

    def test_managed_timeout_falls_back_to_local(self):
        module = load_router()
        calls = stub_upstream(module, [TimeoutError("timed out"), OLLAMA_REPLY])
        status, headers, _ = post(module, json.dumps({"messages": []}))
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Router-Leg"), "local")
        self.assertEqual(len(calls), 2)

    def test_managed_429_falls_back_to_local(self):
        module = load_router()
        calls = stub_upstream(module, [upstream_error(429), OLLAMA_REPLY])
        status, headers, _ = post(module, json.dumps({"messages": []}))
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Router-Leg"), "local")
        self.assertEqual(len(calls), 2)

    def test_managed_4xx_is_not_retried(self):
        module = load_router()
        calls = stub_upstream(module, [upstream_error(400, b'{"error":"Missing model"}')])
        status, headers, body = post(module, json.dumps({"messages": []}))
        self.assertEqual(status, 502)
        self.assertEqual(headers.get("X-Router-Leg"), "none")
        self.assertIn("Missing model", body["error"])
        self.assertEqual(len(calls), 1)

    def test_both_legs_failing_reports_both(self):
        module = load_router()
        calls = stub_upstream(module, [upstream_error(503, b"sinaku"), OSError("connection refused")])
        status, headers, body = post(module, json.dumps({"messages": []}))
        self.assertEqual(status, 502)
        self.assertEqual(headers.get("X-Router-Fallback"), "false")
        self.assertIn("managed leg: HTTP 503: sinaku", body["error"])
        self.assertIn("local leg: OSError: connection refused", body["error"])
        self.assertEqual(len(calls), 2)

    def test_cross_leg_fallback_can_be_disabled(self):
        module = load_router({"CROSS_LEG_FALLBACK": "false"})
        calls = stub_upstream(module, [upstream_error(500)])
        status, headers, _ = post(module, json.dumps({"messages": []}))
        self.assertEqual(status, 502)
        self.assertEqual(headers.get("X-Router-Leg"), "none")
        self.assertEqual(len(calls), 1)

    def test_local_leg_is_normalized_always(self):
        module = load_router({"ROUTER_PRIORITY": "cost-optimized"})
        calls = stub_upstream(module, [OLLAMA_REPLY])
        status, headers, body = post(module, json.dumps({"messages": []}))
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Router-Leg"), "local")
        self.assertEqual(headers.get("X-Router-Fallback"), "false")
        self.assertEqual(body["object"], "chat.completion")
        self.assertEqual(body["choices"][0]["message"]["content"], "respuesta local")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["url"], LOCAL["url"])

    def test_local_leg_never_sends_credentials(self):
        module = load_router({"ROUTER_PRIORITY": "cost-optimized"})
        calls = stub_upstream(module, [OLLAMA_REPLY, OPENAI_REPLY])
        post(module, json.dumps({"model": "groq/openai/gpt-oss-20b", "messages": []}))
        self.assertEqual(calls[0]["body"]["model"], "llama3.2:1b")
        self.assertIsNone(calls[0]["auth"])

    def test_local_leg_failure_escalates_to_managed(self):
        module = load_router({"ROUTER_PRIORITY": "cost-optimized"})
        calls = stub_upstream(module, [upstream_error(500), OPENAI_REPLY])
        status, headers, body = post(module, json.dumps({"messages": []}))
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Router-Leg"), "managed")
        self.assertEqual(headers.get("X-Router-Fallback"), "true")
        self.assertEqual(body, OPENAI_REPLY)
        self.assertEqual(calls[1]["body"]["model"], "auto/best-chat")

    def test_body_priority_field_is_honoured_then_popped(self):
        module = load_router()
        calls = stub_upstream(module, [OLLAMA_REPLY])
        post(module, json.dumps({"priority": "cost-optimized", "messages": []}))
        self.assertEqual(calls[0]["url"], LOCAL["url"])
        self.assertNotIn("priority", calls[0]["body"])

    def test_header_priority_wins_over_body(self):
        module = load_router()
        calls = stub_upstream(module, [OPENAI_REPLY])
        post(module, json.dumps({"priority": "cost-optimized", "messages": []}),
             headers={"X-Router-Priority": "max-performance"})
        self.assertEqual(calls[0]["url"], MANAGED["url"])

    def test_streaming_is_always_disabled_upstream(self):
        module = load_router()
        calls = stub_upstream(module, [OPENAI_REPLY])
        post(module, json.dumps({"messages": [], "stream": True}))
        self.assertIs(calls[0]["body"]["stream"], False)

    def test_invalid_json_is_a_400(self):
        module = load_router()
        stub_upstream(module, [])
        status, _, body = post(module, b"{no-json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid JSON")


class MetricsTest(unittest.TestCase):
    def test_metrics_endpoint_is_served_on_the_api_port(self):
        module = load_router()
        status, headers, body = get(module, "/metrics")
        self.assertEqual(status, 200)
        self.assertIn("text/plain", headers.get("Content-Type"))
        self.assertIn("version=0.0.4", headers.get("Content-Type"))
        series, _ = parse_exposition(body)
        self.assertEqual(series.get("router_up"), 1.0)

    def test_unknown_get_path_is_still_404(self):
        module = load_router()
        status, _, _ = get(module, "/nope")
        self.assertEqual(status, 404)

    def test_managed_success_counts_requests_and_tokens(self):
        module = load_router()
        reply = dict(OPENAI_REPLY, usage={"prompt_tokens": 100, "completion_tokens": 20})
        stub_upstream(module, [reply])
        post(module, json.dumps({"messages": []}))
        series, _ = parse_exposition(get(module, "/metrics")[2])
        self.assertEqual(series['router_requests_total{leg="managed",status_class="2xx"}'], 1.0)
        self.assertEqual(series['router_tokens_total{direction="prompt",leg="managed"}'], 100.0)
        self.assertEqual(series['router_tokens_total{direction="completion",leg="managed"}'], 20.0)
        self.assertNotIn('router_fallbacks_total{from_leg="managed",reason="upstream_failure",to_leg="local"}', series)

    def test_local_success_uses_ollama_token_counts(self):
        module = load_router({"ROUTER_PRIORITY": "cost-optimized"})
        stub_upstream(module, [OLLAMA_REPLY])
        post(module, json.dumps({"messages": []}))
        series, _ = parse_exposition(get(module, "/metrics")[2])
        self.assertEqual(series['router_requests_total{leg="local",status_class="2xx"}'], 1.0)
        self.assertEqual(series['router_tokens_total{direction="prompt",leg="local"}'], 11.0)
        self.assertEqual(series['router_tokens_total{direction="completion",leg="local"}'], 7.0)

    def test_cross_leg_fallback_is_counted_with_both_legs(self):
        module = load_router()
        stub_upstream(module, [upstream_error(500), OLLAMA_REPLY])
        post(module, json.dumps({"messages": []}))
        series, _ = parse_exposition(get(module, "/metrics")[2])
        self.assertEqual(series['router_fallbacks_total{from_leg="managed",reason="upstream_failure",to_leg="local"}'], 1.0)
        self.assertEqual(series['router_requests_total{leg="local",status_class="2xx"}'], 1.0)
        self.assertEqual(series['router_upstream_errors_total{kind="http_500",leg="managed"}'], 1.0)

    def test_both_legs_failing_counts_5xx_and_no_leg(self):
        module = load_router()
        stub_upstream(module, [upstream_error(503, b"x"), OSError("refused")])
        post(module, json.dumps({"messages": []}))
        series, _ = parse_exposition(get(module, "/metrics")[2])
        self.assertEqual(series['router_requests_total{leg="none",status_class="5xx"}'], 1.0)
        self.assertEqual(series['router_upstream_errors_total{kind="http_503",leg="managed"}'], 1.0)
        self.assertEqual(series['router_upstream_errors_total{kind="OSError",leg="local"}'], 1.0)

    def test_invalid_json_counts_4xx(self):
        module = load_router()
        stub_upstream(module, [])
        post(module, b"{no-json")
        series, _ = parse_exposition(get(module, "/metrics")[2])
        self.assertEqual(series['router_requests_total{leg="none",status_class="4xx"}'], 1.0)

    def test_counters_accumulate_across_requests(self):
        module = load_router()
        stub_upstream(module, [OPENAI_REPLY, OPENAI_REPLY, OPENAI_REPLY])
        for _ in range(3):
            post(module, json.dumps({"messages": []}))
        series, _ = parse_exposition(get(module, "/metrics")[2])
        self.assertEqual(series['router_requests_total{leg="managed",status_class="2xx"}'], 3.0)

    def test_duration_histogram_is_monotonic_per_leg(self):
        module = load_router()
        stub_upstream(module, [OPENAI_REPLY])
        post(module, json.dumps({"messages": []}))
        series, _ = parse_exposition(get(module, "/metrics")[2])
        buckets = [(bound, series['router_request_duration_seconds_bucket{le="%s",leg="managed"}' % bound])
                   for bound in ("0.05", "0.1", "0.25", "0.5", "1", "2.5", "5",
                                 "10", "30", "60", "120", "300")]
        counts = [count for _, count in buckets]
        self.assertEqual(counts, sorted(counts), "buckets must be monotonic")
        self.assertEqual(series['router_request_duration_seconds_count{leg="managed"}'], 1.0)
        total = series['router_request_duration_seconds_sum{leg="managed"}']
        self.assertGreater(total, 0.0)
        self.assertLess(total, 1.0, "stubbed upstream answers instantly")

    def test_exposition_declares_help_and_type_exactly_once(self):
        module = load_router()
        _, declared = parse_exposition(get(module, "/metrics")[2])
        for name, kinds in declared.items():
            self.assertEqual(kinds, {"HELP", "TYPE"}, "%s metadata is wrong" % name)
        self.assertIn("router_request_duration_seconds", declared)
        self.assertIn("router_tokens_total", declared)

    def test_metrics_never_leak_the_api_key_or_payload(self):
        module = load_router()
        stub_upstream(module, [OPENAI_REPLY])
        post(module, json.dumps({"messages": [{"role": "user", "content": "secreto-de-usuario"}]}))
        body = get(module, "/metrics")[2]
        self.assertNotIn("secreto", body)
        self.assertNotIn(BASE_ENV["FALLBACK_API_KEY"], body)
        self.assertNotIn("Bearer", body)

    def test_metrics_do_not_change_the_public_contract(self):
        module = load_router()
        calls = stub_upstream(module, [OPENAI_REPLY])
        status, headers, body = post(module, json.dumps({"messages": []}))
        self.assertEqual(status, 200)
        self.assertEqual(body, OPENAI_REPLY)
        self.assertEqual(headers.get("X-Router-Leg"), "managed")
        self.assertEqual(calls[0]["ua"], "finops-arbitrage-router/1.0")
        get(module, "/metrics")


if __name__ == "__main__":
    import warnings

    # HTTPError keeps the upstream body in a file object; unraised queued
    # errors are garbage collected at exit and the warning is noise here.
    warnings.simplefilter("ignore", ResourceWarning)
    unittest.main(verbosity=2)
