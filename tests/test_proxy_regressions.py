from __future__ import annotations

import copy
import http.client
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from reqslim.zcode import __main__ as cli
from reqslim.zcode.proxy import ZCodeProxy
from reqslim.zcode.stream import Assembler
from reqslim.zcode.tls import _openssl, ensure_tls


def payload(stream=False):
    return {"model": "test-model", "max_tokens": 4096, "stream": stream,
            "messages": [{"role": "user", "content": "test"}]}


def message(text="answer"):
    return {"id": "msg_test", "type": "message", "role": "assistant",
            "model": "test-model", "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 3}}


def events_for(value):
    head = {**value, "content": [], "stop_reason": None,
            "usage": {**value["usage"], "output_tokens": 0}}
    events = [{"type": "message_start", "message": head}]
    for index, block in enumerate(value["content"]):
        start = copy.deepcopy(block)
        if block["type"] == "text":
            start["text"] = ""
            start.pop("citations", None)
        elif block["type"] == "tool_use":
            start["input"] = {}
        events.append({"type": "content_block_start", "index": index, "content_block": start})
        if block["type"] == "text":
            events.append({"type": "content_block_delta", "index": index,
                           "delta": {"type": "text_delta", "text": block["text"]}})
            for citation in block.get("citations", []):
                events.append({"type": "content_block_delta", "index": index,
                               "delta": {"type": "citations_delta", "citation": citation}})
        elif block["type"] == "tool_use":
            events.append({"type": "content_block_delta", "index": index,
                           "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}})
        events.append({"type": "content_block_stop", "index": index})
    events.extend([
        {"type": "message_delta", "delta": {"stop_reason": value["stop_reason"]},
         "usage": {"output_tokens": value["usage"]["output_tokens"]}},
        {"type": "message_stop"},
    ])
    return events


def encode_events(events, multiline=False):
    pieces = []
    for event in events:
        encoded = json.dumps(event, indent=2 if multiline else None)
        pieces.append("event: " + event["type"] + "\n")
        pieces.extend("data: " + line + "\n" for line in encoded.splitlines())
        pieces.append("\n")
    return "".join(pieces).encode()


def assemble(body):
    result = Assembler()
    data = []
    for line in body.decode().splitlines():
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif not line and data:
            result.feed(json.loads("\n".join(data)))
            data.clear()
    return result.finish()


def request(port, path="/v1/messages", value=None, method="POST", headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        body = json.dumps(payload() if value is None else value).encode()
        connection.request(method, path, body=body if method == "POST" else None,
                           headers={"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


class Gateway:
    def __init__(self, name="A", *, response=None, raw_sse=None, gate=None):
        self.calls = []
        self.errors = []
        self.entered = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                return

            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def respond(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                value = json.loads(body) if body else {}
                outer.calls.append({"path": self.path, "authorization": self.headers.get("Authorization")})
                outer.entered.set()
                if gate is not None and not gate.wait(5):
                    raise TimeoutError("test gate was not released")
                if self.path.split("?", 1)[0].endswith("/count_tokens"):
                    result = {"input_tokens": 42}
                elif self.path.startswith("/api/paas/"):
                    result = {"gateway": name}
                else:
                    result = response(value, self.path) if response else message(name)
                if value.get("stream"):
                    encoded = raw_sse if raw_sse is not None else encode_events(events_for(result))
                    content_type = "text/event-stream"
                else:
                    encoded = json.dumps(result).encode()
                    content_type = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.handle_error = lambda request, address: self.errors.append(sys.exc_info()[1])
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/api/anthropic"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


class ProxyRegressions(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def gateway(self, *args, **kwargs):
        gateway = Gateway(*args, **kwargs)
        self.addCleanup(lambda: self.assertEqual(gateway.errors, []))
        self.addCleanup(gateway.close)
        return gateway

    def proxy(self, gateway, **kwargs):
        proxy = ZCodeProxy(gateway.url, **kwargs)
        server = proxy.serve("127.0.0.1", 0)
        self.addCleanup(proxy.close)
        return proxy, server.server_address[1]

    def config(self, port, upstream, **options):
        source = self.root / "source.json"
        source.write_text(json.dumps({"config": {"providerConfigRules": {"providerRules": [
            {"providerId": "test", "config": {"api": {"type": "anthropic-messages", "baseUrl": upstream}}}
        ]}}}), encoding="utf-8")
        path = self.root / "autostart.json"
        data = {"listeners": [[port, upstream]], "record": None,
                "zcode_table": str(source), "config_dir": str(self.root), **options}
        path.write_text(json.dumps(data), encoding="utf-8")
        return path, source

    def test_handshake_uses_the_current_listener_gateway(self):
        first, second = self.gateway("A"), self.gateway("B")
        proxy = ZCodeProxy(first.url)
        self.addCleanup(proxy.close)
        servers = proxy.serve_many("127.0.0.1", [(0, first.url), (0, second.url)])
        for method in ("GET", "POST"):
            status, body = request(servers[1].server_address[1], "/api/paas/c1f3a7e2/v2/client",
                                   {"nonce": "synthetic"}, method=method,
                                   headers={"Authorization": "Bearer synthetic"})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"gateway": "B"})
        self.assertEqual(first.calls, [])
        self.assertEqual(len(second.calls), 2)
        self.assertTrue(all(call["authorization"] == "Bearer synthetic" for call in second.calls))

    def test_query_is_forwarded_and_partitions_raw_and_normalized_cache(self):
        upstream = self.gateway(response=lambda value, path: message(path))
        proxy, port = self.proxy(upstream)
        for query in ("beta=true", "beta=false"):
            status, body = request(port, "/v1/messages?" + query)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["content"][0]["text"], "/api/anthropic/v1/messages?" + query)
        reordered = payload()
        reordered["metadata"] = {"user_id": "synthetic"}
        request(port, "/v1/messages?beta=true", reordered)
        request(port, "/v1/messages?beta=false")
        self.assertEqual(len(upstream.calls), 2)
        self.assertEqual(proxy.stats()["hits"], {"normalized": 1, "raw": 1})
        self.assertEqual(proxy.stats()["duplicates"], 2)

    def test_messages_and_count_tokens_do_not_share_cache_or_flight(self):
        upstream = self.gateway()
        proxy, port = self.proxy(upstream)
        request(port)
        status, body = request(port, "/v1/messages/count_tokens")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"input_tokens": 42})
        self.assertEqual(len(upstream.calls), 2)
        self.assertEqual(proxy.stats()["duplicates"], 0)

    def test_parallel_tool_results_are_not_wrongly_replayed(self):
        def answer(value, path):
            calls = {block["id"]: block["input"]["path"] for block in value["messages"][-2]["content"]}
            text = ";".join(calls[block["tool_use_id"]] + "=" + block["content"]
                            for block in value["messages"][-1]["content"])
            return message(text)

        upstream = self.gateway(response=answer)
        proxy, port = self.proxy(upstream)
        first = payload()
        first["tools"] = [{"name": "read_file", "input_schema": {"type": "object"}}]
        first["messages"].extend([
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "u1", "name": "read_file", "input": {"path": "a.txt"}},
                {"type": "tool_use", "id": "u2", "name": "read_file", "input": {"path": "b.txt"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "u1", "content": "A"},
                {"type": "tool_result", "tool_use_id": "u2", "content": "B"},
            ]},
        ])
        second = copy.deepcopy(first)
        second["messages"][-1]["content"][0]["tool_use_id"] = "u2"
        second["messages"][-1]["content"][1]["tool_use_id"] = "u1"
        _, original = request(port, value=first)
        _, swapped = request(port, value=second)
        self.assertEqual(json.loads(original)["content"][0]["text"], "a.txt=A;b.txt=B")
        self.assertEqual(json.loads(swapped)["content"][0]["text"], "b.txt=A;a.txt=B")
        self.assertEqual(len(upstream.calls), 2)

    def test_multiline_sse_finishes_and_is_cacheable(self):
        expected = message("multiline")
        upstream = self.gateway(raw_sse=encode_events(events_for(expected), multiline=True))
        proxy, port = self.proxy(upstream)
        for _ in range(2):
            status, body = request(port, value=payload(True))
            self.assertEqual(status, 200)
            self.assertNotIn(b"event: error", body)
            self.assertEqual(assemble(body)["content"], expected["content"])
        self.assertEqual(len(upstream.calls), 1)
        self.assertTrue(all(event.status == 200 for event in proxy.recorder.events))

    def test_stream_cache_preserves_citations_and_tool_caller(self):
        citation = {"type": "char_location", "cited_text": "fact", "document_index": 0,
                    "document_title": "source", "start_char_index": 0, "end_char_index": 4}
        expected = message()
        expected["content"] = [
            {"type": "text", "text": "fact", "citations": [citation]},
            {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.txt"},
             "caller": {"type": "direct"}},
        ]
        expected["stop_reason"] = "tool_use"
        upstream = self.gateway(raw_sse=encode_events(events_for(expected)))
        proxy, port = self.proxy(upstream)
        for _ in range(2):
            status, body = request(port, value=payload(True))
            self.assertEqual(status, 200)
            self.assertEqual(assemble(body)["content"], expected["content"])
        self.assertEqual(len(upstream.calls), 1)

    @unittest.skipIf(_openssl() is None, "OpenSSL is unavailable")
    def test_idle_tcp_does_not_block_tls_health(self):
        tls = ensure_tls(self.root)
        proxy = ZCodeProxy("http://127.0.0.1:1", timeout=1)
        self.addCleanup(proxy.close)
        server = proxy.serve("127.0.0.1", 0, tls=(str(tls.cert), str(tls.key)))
        port = server.server_address[1]
        entered = threading.Event()
        original = server.finish_request

        def finish(request, address):
            entered.set()
            return original(request, address)

        server.finish_request = finish
        idle = socket.create_connection(("127.0.0.1", port), timeout=2)
        try:
            self.assertTrue(entered.wait(3))
            self.assertIsNotNone(cli.health_on_port(port, timeout=2, scheme="https"))
        finally:
            idle.close()

    def test_evicted_and_disabled_capacity_responses_still_coalesce(self):
        for options in ({"cache_bytes": 128}, {"max_cache": 0}, {"cache_enabled": False}):
            with self.subTest(options=options):
                gate, follower = threading.Event(), threading.Event()
                upstream = self.gateway(response=lambda value, path: message("x" * 2048), gate=gate)
                proxy, port = self.proxy(upstream, **options)
                original = proxy._acquire

                def acquire(key):
                    result = original(key)
                    if not result[0]:
                        follower.set()
                    return result

                proxy._acquire = acquire
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(request, port)
                    self.assertTrue(upstream.entered.wait(3))
                    second = pool.submit(request, port)
                    try:
                        self.assertTrue(follower.wait(3))
                    finally:
                        gate.set()
                    self.assertEqual(first.result()[0], 200)
                    self.assertEqual(second.result()[0], 200)
                self.assertEqual(len(upstream.calls), 1)
                self.assertEqual(proxy.cache.size(), 0)
                self.assertEqual(proxy.stats()["hits"], {"coalesced": 1})

    def _assert_publish_race_does_not_duplicate(self, options):
        gate, paused, released = threading.Event(), threading.Event(), threading.Event()
        upstream = self.gateway(gate=gate)
        proxy, port = self.proxy(upstream, **options)
        original_acquire, original_release = proxy._acquire, proxy._release
        acquisitions = 0
        lock = threading.Lock()

        def acquire(key):
            nonlocal acquisitions
            with lock:
                acquisitions += 1
                number = acquisitions
            if number == 2:
                paused.set()
                if not released.wait(5):
                    raise TimeoutError("leader did not release")
            return original_acquire(key)

        def release(key):
            original_release(key)
            released.set()

        proxy._acquire, proxy._release = acquire, release
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(request, port)
            self.assertTrue(upstream.entered.wait(3))
            second = pool.submit(request, port)
            try:
                self.assertTrue(paused.wait(3))
            finally:
                gate.set()
            self.assertEqual(first.result()[0], 200)
            self.assertEqual(second.result()[0], 200)
        self.assertEqual(len(upstream.calls), 1)
        self.assertEqual(proxy.stats()["hits"], {"coalesced": 1})
        return proxy, port, upstream

    def test_cache_publish_between_lookup_and_acquire_does_not_duplicate(self):
        for options in ({}, {"max_cache": 0}, {"cache_enabled": False},
                        {"cache_bytes": 128}, {"max_cache_bytes": 1}):
            with self.subTest(options=options):
                self._assert_publish_race_does_not_duplicate(options)

    def test_noncacheable_sequential_requests_do_not_reuse_old_handoff(self):
        for options in ({"max_cache": 0}, {"cache_enabled": False},
                        {"cache_bytes": 128}, {"max_cache_bytes": 1}):
            with self.subTest(options=options):
                answers = iter(("first", "second"))
                upstream = self.gateway(response=lambda value, path: message(next(answers)))
                proxy, port = self.proxy(upstream, **options)
                _, first = request(port)
                _, second = request(port)
                self.assertEqual(json.loads(first)["content"][0]["text"], "first")
                self.assertEqual(json.loads(second)["content"][0]["text"], "second")
                self.assertEqual(len(upstream.calls), 2)
                self.assertEqual(proxy.stats()["hits"], {})

    def test_handoff_overlap_does_not_depend_on_clock_resolution(self):
        with patch("reqslim.zcode.proxy.time.monotonic", return_value=1.0):
            proxy, port, upstream = self._assert_publish_race_does_not_duplicate({"max_cache": 0})
            self.assertEqual(request(port)[0], 200)
            self.assertEqual(len(upstream.calls), 2)
            self.assertEqual(proxy.stats()["hits"], {"coalesced": 1})

    def test_bad_request_shapes_return_400_without_upstream_calls(self):
        upstream = self.gateway()
        proxy, port = self.proxy(upstream)
        for field in ("tools", "messages", "system"):
            with self.subTest(field=field):
                value = payload()
                value[field] = 1
                status, body = request(port, value=value)
                self.assertEqual(status, 400)
                self.assertIn("error", json.loads(body))
        self.assertEqual(upstream.calls, [])

    def test_watcher_retries_rediscovery_after_a_failed_attempt(self):
        upstream = self.gateway()
        _, source = self.config(8788, upstream.url)
        watcher = cli.TableWatcher(self.root / "missing.json", {upstream.url: "http://127.0.0.1:8788"}, self.root / "proxied.json")
        with patch.object(cli, "find_table", side_effect=[None, source]) as find:
            for _ in range(cli.REDISCOVER_AFTER * 2):
                watcher.tick()
            self.assertEqual(find.call_count, 2)
            self.assertEqual(watcher.source, source)
        self.assertTrue(watcher.tick())
        self.assertTrue((self.root / "proxied.json").is_file())

    def test_autostart_requires_matching_options_and_watchers(self):
        upstream = self.gateway()
        proxy, port = self.proxy(upstream)
        config, source = self.config(port, upstream.url)
        base = ["--autostart", "--autostart-config", str(config), "--no-record"]
        for flags in ([], ["--no-cache"], ["--no-coalesce"]):
            with self.subTest(flags=flags), patch.object(cli, "stop_running_proxy", return_value=False) as stop:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main(base + flags), 1)
                stop.assert_called_once()
        proxy.watchers = {"table": str(source.resolve()), "config_dir": str(self.root.resolve()),
                          "poll_seconds": cli.DEFAULT_POLL_SECONDS, "host": "127.0.0.1",
                          "tls_cert": None, "tls_key": None}
        with patch.object(cli, "stop_running_proxy") as stop, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(base), 0)
            stop.assert_not_called()

    def test_explicit_default_values_override_saved_autostart_options(self):
        upstream = self.gateway()
        proxy, port = self.proxy(upstream, ttl=30)
        config, source = self.config(port, upstream.url, ttl=30)
        proxy.watchers = {"table": str(source.resolve()), "config_dir": str(self.root.resolve()),
                          "poll_seconds": cli.DEFAULT_POLL_SECONDS, "host": "127.0.0.1",
                          "tls_cert": None, "tls_key": None}
        with patch.object(cli, "stop_running_proxy", return_value=False) as stop:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli.main(["--autostart", "--autostart-config", str(config), "--no-record", "--ttl", "3600"])
            self.assertEqual(code, 1)
            stop.assert_called_once()

    def test_autostart_readiness_requires_all_runtime_settings(self):
        upstream = self.gateway()
        record = str(self.root / "requests.jsonl")
        proxy, port = self.proxy(upstream, ttl=30, max_cache=12, cache_enabled=False,
                                 coalesce=False, coalesce_wait=2, failure_ttl=0,
                                 record_path=record)
        config, source = self.config(port, upstream.url, record=record, ttl=30, max_cache=12,
                                     cache=False, coalesce=False, coalesce_wait=2, failure_ttl=0)
        proxy.watchers = {"table": str(source.resolve()), "config_dir": str(self.root.resolve()),
                          "poll_seconds": cli.DEFAULT_POLL_SECONDS, "host": "127.0.0.1",
                          "tls_cert": None, "tls_key": None}
        self.assertTrue(cli.autostart_ready(str(config)))
        changes = (
            (proxy, "cache_enabled", True), (proxy, "coalesce", True),
            (proxy, "coalesce_wait", 3), (proxy, "failure_ttl", 1),
            (proxy, "record_path", str(self.root / "other.jsonl")),
            (proxy, "watchers", None), (proxy, "ready", False),
            (proxy.cache, "ttl", 31), (proxy.cache, "maxsize", 13),
        )
        for owner, name, value in changes:
            with self.subTest(name=name), patch.object(owner, name, value):
                self.assertFalse(cli.autostart_ready(str(config)))
        with patch("reqslim.zcode.proxy.__version__", "old-version"):
            self.assertFalse(cli.autostart_ready(str(config)))

    def test_bind_failure_does_not_rewrite_provider_tables(self):
        config, _ = self.config(8788, "http://127.0.0.1:1")
        with patch.object(cli, "health_on_port", return_value=None), \
                patch.object(ZCodeProxy, "serve_many", side_effect=OSError("test bind failure")), \
                patch.object(cli.TableWatcher, "tick") as table_tick, \
                patch.object(cli.CacheWatcher, "tick") as cache_tick, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = cli.main(["--autostart", "--autostart-config", str(config), "--no-record"])
        self.assertEqual(code, 1)
        table_tick.assert_not_called()
        cache_tick.assert_not_called()

    def test_cli_rejects_nonfinite_and_negative_runtime_options(self):
        for arguments in (["--ttl", "nan"], ["--failure-ttl", "inf"],
                          ["--coalesce-wait", "-1"], ["--max-cache", "-1"]):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                cli.main(arguments)

    def test_concurrent_close_waits_for_listener_teardown(self):
        entered, release, second_started, second_done = (threading.Event() for _ in range(4))
        proxy = ZCodeProxy("http://127.0.0.1:1")

        class Server:
            closed = False

            def shutdown(self):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("close was not released")

            def server_close(self):
                self.closed = True

        server = Server()
        proxy._servers = [server]

        def close_again():
            second_started.set()
            proxy.close()
            second_done.set()

        first = threading.Thread(target=proxy.close)
        second = threading.Thread(target=close_again)
        first.start()
        try:
            self.assertTrue(entered.wait(3))
            second.start()
            self.assertTrue(second_started.wait(3))
            self.assertFalse(second_done.wait(0.05))
        finally:
            release.set()
            first.join(5)
            if second.ident is not None:
                second.join(5)
        self.assertTrue(server.closed)
        self.assertTrue(second_done.is_set())

    def test_managed_shutdown_leaves_teardown_to_the_owner(self):
        proxy = ZCodeProxy("http://127.0.0.1:1")
        proxy.managed_shutdown = True
        with patch("reqslim.zcode.proxy.threading.Thread") as worker:
            proxy.request_shutdown()
            worker.assert_not_called()
        self.assertTrue(proxy.shutdown_event.is_set())

    def test_autostart_subprocess_applies_settings_and_stops_cleanly(self):
        upstream = self.gateway()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        config, _ = self.config(port, upstream.url, cache=False, coalesce=False,
                                ttl=30, max_cache=12, failure_ttl=0, poll_seconds=0.1)
        home = self.root / "home"
        cache = home / ".zcode" / "v2" / "runtime" / "provider" / "test" / "1" / "endpoint-test" / "zcode-builtin.json"
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({"config": {"providerConfigRules": {"providerRules": [
            {"providerId": "test", "config": {"api": {"type": "anthropic-messages", "baseUrl": upstream.url}}}
        ]}}}), encoding="utf-8")
        self.addCleanup(lambda: cache.chmod(0o600))
        code = (
            "import sys;sys.path.insert(0," + repr(str(ROOT)) + ");"
            "from reqslim.zcode.__main__ import main;"
            "raise SystemExit(main(['--autostart','--autostart-config',"
            + repr(str(config)) + ",'--no-record']))"
        )
        environment = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
                       "PYTHONIOENCODING": "utf-8"}
        process = subprocess.Popen([sys.executable, "-I", "-B", "-c", code],
                                   cwd=str(self.root), env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace")

        def stop_process():
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)

        self.addCleanup(stop_process)
        deadline = time.monotonic() + 15
        health = None
        while time.monotonic() < deadline:
            health = cli.health_on_port(port, timeout=0.2)
            if health is not None and health.get("ready"):
                break
            if process.poll() is not None:
                output, errors = process.communicate()
                self.fail(output + errors)
            time.sleep(0.05)
        self.assertIsNotNone(health)
        self.assertTrue(health["ready"])
        self.assertEqual(health["pid"], process.pid)
        settings = health["settings"]
        self.assertFalse(settings["cache"])
        self.assertFalse(settings["coalesce"])
        self.assertEqual(settings["ttl"], 30)
        self.assertEqual(settings["max_cache"], 12)
        self.assertEqual(settings["failure_ttl"], 0)
        stored = json.loads(cache.read_text(encoding="utf-8"))
        self.assertEqual(stored["config"]["providerConfigRules"]["providerRules"][0]["config"]["api"]["baseUrl"],
                         f"http://127.0.0.1:{port}")
        request(port)
        request(port)
        self.assertEqual(len(upstream.calls), 2)
        self.assertTrue(cli.stop_running_proxy([port], timeout=10))
        output, errors = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertNotIn("Traceback", output + errors)


if __name__ == "__main__":
    unittest.main()
