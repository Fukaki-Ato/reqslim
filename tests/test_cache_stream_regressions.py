from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reqslim.cache import ExactCache
from reqslim.types import LLMResponse, Usage
from reqslim.zcode.normalize import (
    canonical_messages,
    canonical_system,
    canonical_tools,
    classify,
    fingerprint,
    normalize_request,
    system_text,
)
from reqslim.zcode.stream import Assembler, replay_sse, usable


def payload() -> dict:
    return {
        "model": "synthetic-model",
        "max_tokens": 64,
        "system": [{"type": "text", "text": "synthetic system"}],
        "tools": [
            {"name": "first", "input_schema": {"type": "object"}},
            {"name": "second", "input_schema": {"type": "object"}},
        ],
        "messages": [
            {"role": "user", "content": "synthetic prompt"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "call-a", "name": "first", "input": {"n": 1}},
                    {"type": "tool_use", "id": "call-b", "name": "second", "input": {"n": 2}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call-a", "content": "result one"},
                    {"type": "tool_result", "tool_use_id": "call-b", "content": "result two"},
                ],
            },
        ],
    }


def sse_events(data: bytes) -> list[dict]:
    return [
        json.loads(line[6:])
        for line in data.decode("utf-8").splitlines()
        if line.startswith("data: ")
    ]


def assemble(events: list[dict]) -> Assembler:
    assembler = Assembler()
    for event in events:
        assembler.feed(event)
    return assembler


def finish_events() -> list[dict]:
    return [
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 3},
        },
        {"type": "message_stop"},
    ]


class CanonicalReferenceTests(unittest.TestCase):
    def test_consistent_renaming_preserves_parallel_edges(self) -> None:
        original = payload()
        renamed = copy.deepcopy(original)
        for block, name in zip(renamed["messages"][1]["content"], ["new-b", "new-a"]):
            block["id"] = name
        for block, name in zip(renamed["messages"][2]["content"], ["new-b", "new-a"]):
            block["tool_use_id"] = name
        self.assertEqual(fingerprint(original).key, fingerprint(renamed).key)
        canonical = canonical_messages(original["messages"])
        uses = canonical[1]["content"]
        results = canonical[2]["content"]
        self.assertNotEqual(uses[0]["id"], uses[1]["id"])
        self.assertEqual(uses[0]["id"], results[0]["tool_use_id"])
        self.assertEqual(uses[1]["id"], results[1]["tool_use_id"])

    def test_swapped_parallel_references_change_the_key(self) -> None:
        original = payload()
        swapped = copy.deepcopy(original)
        swapped["messages"][2]["content"][0]["tool_use_id"] = "call-b"
        swapped["messages"][2]["content"][1]["tool_use_id"] = "call-a"
        self.assertNotEqual(fingerprint(original).key, fingerprint(swapped).key)

    def test_unresolved_references_cannot_impersonate_canonical_ids(self) -> None:
        original = payload()
        canonical_id = canonical_messages(original["messages"])[1]["content"][0]["id"]
        candidates = [canonical_id, "tool_0", "toolu_reqslim_0", "resolved:0", "missing"]
        for value in candidates:
            with self.subTest(value=value):
                unresolved = copy.deepcopy(original)
                unresolved["messages"][2]["content"][0]["tool_use_id"] = value
                self.assertNotEqual(fingerprint(original).key, fingerprint(unresolved).key)
        first = copy.deepcopy(original)
        second = copy.deepcopy(original)
        first["messages"][2]["content"][0]["tool_use_id"] = "missing-a"
        second["messages"][2]["content"][0]["tool_use_id"] = "missing-b"
        self.assertNotEqual(fingerprint(first).key, fingerprint(second).key)

    def test_duplicate_and_forward_references_do_not_match_valid_graphs(self) -> None:
        original = payload()
        duplicate = copy.deepcopy(original)
        duplicate["messages"][1]["content"][1]["id"] = "call-a"
        duplicate["messages"][2]["content"][1]["tool_use_id"] = "call-a"
        self.assertNotEqual(fingerprint(original).key, fingerprint(duplicate).key)
        forward = copy.deepcopy(original)
        forward["messages"][0]["content"] = [
            {"type": "tool_result", "tool_use_id": "call-a", "content": "early"}
        ]
        dangling = copy.deepcopy(forward)
        dangling["messages"][0]["content"][0]["tool_use_id"] = "missing"
        self.assertNotEqual(fingerprint(forward).key, fingerprint(dangling).key)

    def test_normalization_is_pure_and_preserves_nontransport_fields(self) -> None:
        original = payload()
        original["messages"][1]["provider_extension"] = {"v": [1, 2]}
        original["messages"][1]["content"][0]["input"]["cache_control"] = {"ttl": 7}
        original["messages"][1]["content"][0]["cache_control"] = {"type": "ephemeral"}
        original["tools"][0]["input_schema"]["cache_control"] = {"arbitrary": True}
        before = copy.deepcopy(original)
        canonical = normalize_request(original)
        self.assertEqual(original, before)
        self.assertEqual(canonical["messages"][1]["provider_extension"], {"v": [1, 2]})
        self.assertEqual(
            canonical["messages"][1]["content"][0]["input"]["cache_control"], {"ttl": 7}
        )
        self.assertNotIn("cache_control", canonical["messages"][1]["content"][0])
        self.assertEqual(canonical["tools"][0]["input_schema"]["cache_control"], {"arbitrary": True})
        canonical["messages"][1]["provider_extension"]["v"].append(3)
        self.assertEqual(original, before)


class FingerprintEndpointTests(unittest.TestCase):
    def test_four_argument_call_and_empty_endpoint_are_compatible(self) -> None:
        original = fingerprint(payload(), "beta", "https://synthetic.invalid", "scope")
        explicit = fingerprint(payload(), "beta", "https://synthetic.invalid", "scope", "")
        self.assertEqual(original, explicit)
        self.assertEqual(original.endpoint, "")
        self.assertEqual((original.beta, original.upstream, original.scope),
                         ("beta", "https://synthetic.invalid", "scope"))

    def test_endpoint_and_query_partition_the_key_not_component_digests(self) -> None:
        endpoints = ["/v1/messages", "/v1/messages?route=a", "/v1/messages?route=b", "/messages"]
        fingerprints = [fingerprint(payload(), endpoint=endpoint) for endpoint in endpoints]
        self.assertEqual(len({item.key for item in fingerprints}), len(endpoints))
        self.assertEqual([item.endpoint for item in fingerprints], endpoints)
        self.assertEqual(len({(item.system, item.tools, item.history, item.tail)
                              for item in fingerprints}), 1)
        self.assertNotEqual(fingerprints[0].key, fingerprint(payload()).key)

    def test_endpoint_does_not_overwrite_a_gateway_body_field(self) -> None:
        first = payload()
        second = payload()
        first["endpoint"] = "body-a"
        second["endpoint"] = "body-b"
        self.assertNotEqual(fingerprint(first, endpoint="/v1/messages").key,
                            fingerprint(second, endpoint="/v1/messages").key)


class CoreValidationTests(unittest.TestCase):
    def assert_invalid_field(self, field: str, values: list) -> None:
        for value in values:
            with self.subTest(field=field, value=value):
                request = payload()
                request[field] = value
                with self.assertRaises(ValueError):
                    fingerprint(request)

    def test_invalid_top_level_request_types_raise_value_error(self) -> None:
        for value in [None, False, 0, "request", [], [payload()]]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_request(value)
                with self.assertRaises(ValueError):
                    fingerprint(value)

    def test_invalid_core_container_shapes_raise_value_error(self) -> None:
        self.assert_invalid_field("tools", [None, False, 0, "tools", {}, [None], ["tool"], [[]]])
        self.assert_invalid_field("messages", [None, False, 0, "messages", {}, [None], ["message"], [[]]])
        self.assert_invalid_field("system", [False, 0, 1.5, {}, {"type": "text", "text": "bad"}, [None], [0], [[]]])

    def test_invalid_core_scalar_types_raise_value_error(self) -> None:
        self.assert_invalid_field("model", [None, False, 0, [], {}])
        self.assert_invalid_field("stream", [None, 0, 1, "false", [], {}])
        self.assert_invalid_field("max_tokens", [None, False, "64", 1.5, [], {}])

    def test_invalid_message_and_block_shapes_raise_value_error(self) -> None:
        invalid = [
            {},
            {"role": [], "content": "text"},
            {"role": "user"},
            {"role": "user", "content": None},
            {"role": "user", "content": False},
            {"role": "user", "content": {}},
            {"role": "user", "content": ["text"]},
            {"role": "user", "content": [{}]},
            {"role": "user", "content": [{"type": []}]},
            {"role": "user", "content": [{"type": "text", "text": 1}]},
        ]
        for message in invalid:
            with self.subTest(message=message):
                with self.assertRaises(ValueError):
                    canonical_messages([message])

    def test_invalid_tool_definitions_and_tool_blocks_raise_value_error(self) -> None:
        definitions = [{}, {"name": []}, {"name": "tool", "input_schema": []},
                       {"name": "tool", "description": {}}]
        for tool in definitions:
            with self.subTest(tool=tool):
                with self.assertRaises(ValueError):
                    canonical_tools([tool])
        blocks = [
            {"type": "tool_use", "name": "tool", "input": {}},
            {"type": "tool_use", "id": [], "name": "tool", "input": {}},
            {"type": "tool_use", "id": "", "name": "tool", "input": {}},
            {"type": "tool_use", "id": "call", "name": {}, "input": {}},
            {"type": "tool_use", "id": "call", "name": "tool", "input": []},
            {"type": "tool_result", "tool_use_id": None},
            {"type": "tool_result", "tool_use_id": "call", "content": {}},
            {"type": "tool_result", "tool_use_id": "call", "content": [1]},
            {"type": "tool_result", "tool_use_id": "call", "is_error": "false"},
        ]
        for block in blocks:
            with self.subTest(block=block):
                request = payload()
                request["messages"][1]["content"] = [block]
                with self.assertRaises(ValueError):
                    fingerprint(request)

    def test_system_validation_is_shared_with_classification(self) -> None:
        for system in [0, {}, [None], [{"type": "text", "text": []}]]:
            with self.subTest(system=system):
                with self.assertRaises(ValueError):
                    canonical_system(system)
                with self.assertRaises(ValueError):
                    system_text(system)
                request = payload()
                request["system"] = system
                with self.assertRaises(ValueError):
                    classify(request)

    def test_gateway_extensions_are_not_overvalidated_or_dropped(self) -> None:
        request = payload()
        request["gateway_extension"] = [None, {"v": [1, True]}]
        request["tools"].append({"type": "vendor_tool_2026", "name": "vendor", "vendor": [1, 2]})
        request["messages"][0]["vendor"] = {"n": 1}
        request["messages"][0]["content"] = [{"type": "vendor_block", "data": [None, False]}]
        request["system"] = [{"type": "vendor_system", "data": {"v": 1}}]
        canonical = normalize_request(request)
        self.assertEqual(canonical["gateway_extension"], request["gateway_extension"])
        self.assertEqual(canonical["messages"][0]["vendor"], {"n": 1})
        self.assertEqual(canonical["system"], request["system"])
        changed = copy.deepcopy(request)
        changed["messages"][0]["vendor"]["n"] = 2
        self.assertNotEqual(fingerprint(request).key, fingerprint(changed).key)

    def test_text_system_extensions_remain_semantic(self) -> None:
        first = payload()
        second = payload()
        first["system"][0]["vendor"] = "a"
        second["system"][0]["vendor"] = "b"
        self.assertNotEqual(fingerprint(first).key, fingerprint(second).key)
        plain = payload()
        as_string = payload()
        as_string["system"] = "synthetic system"
        self.assertEqual(fingerprint(plain).key, fingerprint(as_string).key)


class ExactCacheOwnershipTests(unittest.TestCase):
    def response(self) -> LLMResponse:
        return LLMResponse(
            content="synthetic response",
            raw={"content": [{"type": "text", "text": "original"}]},
            usage=Usage(prompt_tokens=7, completion_tokens=3),
            meta={"nested": {"values": [1, {"label": "original"}]},
                  "original_response": {"content": ["original"]}},
        )

    def test_put_detaches_the_original_response_and_all_mutable_fields(self) -> None:
        cache = ExactCache(ttl=None)
        response = self.response()
        self.assertIs(cache.put("key", response), True)
        response.content = "changed"
        response.raw["content"][0]["text"] = "changed"
        response.usage.prompt_tokens = 99
        response.meta["nested"]["values"][1]["label"] = "changed"
        response.meta["original_response"]["content"].append("changed")
        hit = cache.get("key")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.content, "synthetic response")
        self.assertEqual(hit.raw["content"][0]["text"], "original")
        self.assertEqual(hit.usage.prompt_tokens, 7)
        self.assertEqual(hit.meta["nested"]["values"][1]["label"], "original")
        self.assertEqual(hit.meta["original_response"]["content"], ["original"])

    def test_each_hit_has_independent_nested_response_state(self) -> None:
        cache = ExactCache(ttl=None)
        original = self.response()
        cache.put("key", original)
        first = cache.get("key")
        first.raw["content"][0]["text"] = "mutated hit"
        first.usage.completion_tokens = 99
        first.meta["nested"]["values"].append(8)
        first.meta["original_response"]["content"].clear()
        second = cache.get("key")
        self.assertEqual(second.raw, original.raw)
        self.assertEqual(second.usage, original.usage)
        self.assertEqual(second.meta, original.meta)
        self.assertTrue(second.cached)
        self.assertEqual(second.source, "exact_cache")
        self.assertFalse(original.cached)
        self.assertEqual(original.source, "llm")
        self.assertEqual((cache.hits, cache.misses), (2, 0))

    def test_put_reports_immediate_rejection(self) -> None:
        for options in [{"maxsize": 0}, {"max_bytes": 0}, {"max_bytes": 2}, {"ttl": 0}]:
            with self.subTest(options=options):
                cache = ExactCache(**options)
                self.assertIs(cache.put("key", self.response()), False)
                self.assertEqual(cache.size(), 0)
                self.assertIsNone(cache.get("key"))

    def test_put_reports_retained_replacements_and_lru_evictions(self) -> None:
        cache = ExactCache(maxsize=2, ttl=None, max_bytes=4)
        self.assertIs(cache.put("a", LLMResponse("aa")), True)
        self.assertIs(cache.put("b", LLMResponse("bb")), True)
        self.assertIsNotNone(cache.get("a"))
        self.assertIs(cache.put("c", LLMResponse("cc")), True)
        self.assertIsNone(cache.get("b"))
        self.assertIs(cache.put("a", LLMResponse("x")), True)
        self.assertEqual(cache.get("a").content, "x")
        self.assertIs(cache.put("large", LLMResponse("oversized")), False)
        self.assertIsNone(cache.get("large"))


class StreamFidelityTests(unittest.TestCase):
    def test_citations_delta_is_accumulated_without_mutating_input(self) -> None:
        first = {"type": "char_location", "cited_text": "one", "document_index": 0,
                 "document_title": "synthetic", "start_char_index": 0, "end_char_index": 3}
        second = {"type": "vendor_citation", "data": {"values": [2]}}
        events = [
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": "", "citations": None, "vendor": [1]}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "answer"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "citations_delta", "citation": first}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "citations_delta", "citation": second}},
            {"type": "content_block_stop", "index": 0},
        ] + finish_events()
        before = copy.deepcopy(events)
        assembler = assemble(events)
        block = assembler.finish()["content"][0]
        self.assertTrue(assembler.complete)
        self.assertEqual(block["citations"], [first, second])
        self.assertEqual(block["text"], "answer")
        self.assertEqual(events, before)
        first["cited_text"] = "changed"
        self.assertEqual(block["citations"][0]["cited_text"], "one")

    def test_replay_roundtrip_preserves_citations_callers_and_other_fields(self) -> None:
        message = {
            "id": "synthetic-message", "type": "message", "role": "assistant", "model": "synthetic",
            "content": [
                {"type": "thinking", "thinking": "private", "signature": "sig-123", "vendor": {"v": 1}},
                {"type": "redacted_thinking", "data": "opaque", "vendor": [1]},
                {"type": "text", "text": "answer", "citations": [
                    {"type": "char_location", "cited_text": "source", "document_index": 0,
                     "start_char_index": 0, "end_char_index": 6}
                ], "vendor": {"v": 2}},
                {"type": "tool_use", "id": "synthetic-call", "name": "first",
                 "input": {"nested": {"values": [1, 2]}, "cache_control": "data"},
                 "caller": {"type": "code_execution_20260101", "tool_id": "caller-1"},
                 "vendor": {"v": 3}},
                {"type": "vendor_block", "data": {"values": [3]}},
            ],
            "stop_reason": "tool_use", "stop_sequence": None,
            "usage": {"input_tokens": 7, "output_tokens": 3, "vendor": {"n": 2}},
            "vendor": {"message": True},
        }
        before = copy.deepcopy(message)
        events = sse_events(replay_sse(message))
        assembler = assemble(events)
        replayed = assembler.finish()
        self.assertTrue(assembler.complete)
        self.assertTrue(usable(replayed))
        self.assertEqual(replayed, message)
        self.assertEqual(message, before)
        self.assertTrue(any(event.get("delta", {}).get("type") == "citations_delta" for event in events))

    def test_complete_tool_start_input_survives_without_json_deltas(self) -> None:
        block = {"type": "tool_use", "id": "synthetic-call", "name": "first",
                 "input": {"path": "synthetic.txt", "nested": {"values": [1, 2]}},
                 "caller": {"type": "direct"}}
        events = [{"type": "content_block_start", "index": 0, "content_block": block},
                  {"type": "content_block_stop", "index": 0}] + finish_events()
        assembler = assemble(events)
        self.assertEqual(assembler.finish()["content"], [block])
        self.assertTrue(assembler.complete)

    def test_json_deltas_still_replace_start_input_and_detect_truncation(self) -> None:
        events = [
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": "call", "name": "first", "input": {"old": 1}}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": '{"new":'}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": '[1, 2]}'}},
            {"type": "content_block_stop", "index": 0},
        ] + finish_events()
        assembler = assemble(events)
        self.assertEqual(assembler.finish()["content"][0]["input"], {"new": [1, 2]})
        self.assertTrue(assembler.complete)
        broken = assemble(events[:2] + events[3:])
        self.assertTrue(broken.malformed)
        self.assertFalse(broken.complete)
        self.assertFalse(usable(broken.finish()))
        unclosed = assemble(events[:1] + finish_events())
        self.assertFalse(unclosed.complete)

    def test_thinking_and_signature_fragments_are_preserved(self) -> None:
        events = [
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "thinking", "thinking": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "think-"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "more"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "signature_delta", "signature": "sig-"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "signature_delta", "signature": "tail"}},
            {"type": "content_block_stop", "index": 0},
        ] + finish_events()
        assembler = assemble(events)
        block = assembler.finish()["content"][0]
        self.assertEqual(block["thinking"], "think-more")
        self.assertEqual(block["signature"], "sig-tail")
        replayed = assemble(sse_events(replay_sse(assembler.finish())))
        self.assertEqual(replayed.finish()["content"], [block])
        self.assertTrue(replayed.complete)

    def test_empty_and_null_citations_and_signatures_survive_replay(self) -> None:
        for extra in [{}, {"citations": []}, {"citations": None}]:
            with self.subTest(extra=extra):
                block = {"type": "text", "text": "answer", **extra}
                message = {"content": [block], "stop_reason": "end_turn"}
                self.assertEqual(assemble(sse_events(replay_sse(message))).finish()["content"], [block])
        for extra in [{}, {"signature": ""}, {"signature": None}]:
            with self.subTest(extra=extra):
                block = {"type": "thinking", "thinking": "think", **extra}
                message = {"content": [block], "stop_reason": "end_turn"}
                self.assertEqual(assemble(sse_events(replay_sse(message))).finish()["content"], [block])

    def test_malformed_citations_delta_is_not_silently_cacheable(self) -> None:
        for citation in [None, "source", [], 1]:
            with self.subTest(citation=citation):
                events = [
                    {"type": "content_block_start", "index": 0,
                     "content_block": {"type": "text", "text": "answer"}},
                    {"type": "content_block_delta", "index": 0,
                     "delta": {"type": "citations_delta", "citation": citation}},
                ] + finish_events()
                assembler = assemble(events)
                self.assertTrue(assembler.malformed)
                self.assertFalse(assembler.complete)


if __name__ == "__main__":
    unittest.main()
