"""Namespace identities survive gateway-only function aliases and history replay."""

import copy
import json
import unittest
from unittest.mock import patch

import proxy_server as proxy
from tests import test_native_responses as native_tests
from tests.test_routes import _fake_post


class NamespaceRequestTests(unittest.TestCase):
    setUp = native_tests.NativeRouteTests.setUp
    post = native_tests.NativeRouteTests.post
    reset_upstream_calls = native_tests.NativeRouteTests.reset_upstream_calls
    assert_no_upstream_calls = native_tests.NativeRouteTests.assert_no_upstream_calls
    def body(self, namespace="mcp__codex_worktrees"):
        return {"model": "gpt-6-astra", "input": [
            {"type": "function_call", "id": "fc_orig", "call_id": "call_orig",
             "namespace": namespace, "name": "list_worktrees", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_orig", "output": "synthetic"}],
            "tools": [{"type": "namespace", "name": namespace, "description": "Worktree management",
                       "tools": [{"type": "function", "name": "list_worktrees", "description": "List",
                                  "strict": True, "parameters": {"type": "object", "properties": {},
                                                                  "additionalProperties": False}}]}],
            "tool_choice": {"type": "function", "namespace": namespace, "name": "list_worktrees"}}

    def test_namespace_flattening_preserves_schema_history_choice_and_input(self):
        body = self.body()
        original = copy.deepcopy(body)
        response = self.post(body)
        self.assertEqual(response.status_code, 200)
        sent = self.native.post.call_args.kwargs["json"]
        tool = sent["tools"][0]
        self.assertEqual(tool["type"], "function")
        self.assertLessEqual(len(tool["name"]), 64)
        self.assertRegex(tool["name"], r"^[A-Za-z0-9_-]+$")
        self.assertEqual(tool["parameters"], original["tools"][0]["tools"][0]["parameters"])
        self.assertTrue(tool["strict"])
        self.assertIn("Worktree management", tool["description"])
        self.assertIn("mcp__codex_worktrees.list_worktrees", tool["description"])
        self.assertIn("List", tool["description"])
        self.assertEqual(sent["input"][0], {"type": "function_call", "id": "fc_orig", "call_id": "call_orig",
                                          "name": tool["name"], "arguments": "{}"})
        self.assertEqual(sent["input"][1], original["input"][1])
        self.assertEqual(sent["tool_choice"], {"type": "function", "name": tool["name"]})
        self.assertEqual(body, original)
        self.post(body)
        self.assertEqual(self.native.post.call_args.kwargs["json"]["tools"][0]["name"], tool["name"])

    def test_descriptionless_functions_expose_distinct_qualified_identities(self):
        body = {"model": "gpt-6-astra", "input": "Read a file", "tools": [
            {"type": "namespace", "name": "file_ops", "tools": [
                {"type": "function", "name": name, "strict": False,
                 "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}
                for name in ("read_file", "delete_file")]}]}
        original = copy.deepcopy(body)
        adapter = proxy._NativeToolAdapter(body)
        prepared = proxy.prepare_native_responses(body, {}, adapter)
        self.assertEqual(body, original)
        self.assertEqual(self.post(body).status_code, 200)
        forwarded = self.native.post.call_args.kwargs["json"]["tools"]
        self.assertEqual(forwarded, prepared["tools"])
        self.assertNotEqual(forwarded[0]["name"], forwarded[1]["name"])
        for tool, name in zip(forwarded, ("read_file", "delete_file")):
            self.assertIn("file_ops." + name, tool["description"])
            self.assertEqual(tool["parameters"], original["tools"][0]["tools"][0]["parameters"])
            self.assertIs(tool["strict"], False)

    def test_same_leaf_names_across_namespaces_are_distinct_and_history_only_maps(self):
        body = self.body("a")
        second = copy.deepcopy(body["tools"][0])
        second["name"] = "b"
        body["tools"].append(second)
        self.assertEqual(self.post(body).status_code, 200)
        aliases = [tool["name"] for tool in self.native.post.call_args.kwargs["json"]["tools"]]
        self.assertEqual(len(set(aliases)), 2)
        historical = self.body("a")
        historical["tools"] = []
        historical.pop("tool_choice")
        self.assertEqual(self.post(historical).status_code, 200)
        self.assertEqual(self.native.post.call_args.kwargs["json"]["input"][0]["name"], aliases[0])

    def test_malformed_duplicate_and_nested_namespace_reject_before_inference(self):
        for mutation in ("duplicate", "nested", "custom", "invalid_name", "duplicate_child"):
            with self.subTest(mutation=mutation):
                body = self.body()
                if mutation == "duplicate":
                    body["tools"].append(copy.deepcopy(body["tools"][0]))
                elif mutation == "duplicate_child":
                    body["tools"][0]["tools"] *= 2
                elif mutation in {"nested", "custom"}:
                    body["tools"][0]["tools"][0]["type"] = "namespace" if mutation == "nested" else "custom"
                else:
                    body["tools"][0]["name"] = []
                self.reset_upstream_calls()
                response = self.post(body)
                self.assertEqual(response.status_code, 400)
                self.assertIn("tools", response.get_json()["error"]["param"])
                self.assert_no_upstream_calls()

    def test_alias_collision_with_ordinary_function_is_explicit(self):
        body = self.body()
        self.assertEqual(self.post(body).status_code, 200)
        alias = self.native.post.call_args.kwargs["json"]["tools"][0]["name"]
        body["tools"].append({"type": "function", "name": alias, "parameters": {}})
        self.reset_upstream_calls()
        self.assertEqual(self.post(body).status_code, 400)
        self.assert_no_upstream_calls()

    def test_json_function_identity_and_response_metadata_restored(self):
        body = self.body()
        def upstream(url, **kwargs):
            alias = kwargs["json"]["tools"][0]["name"]
            response = native_tests.native_body()
            response.update(tools=kwargs["json"]["tools"], tool_choice=kwargs["json"]["tool_choice"], future_field={"name": alias})
            response["output"] = [{"type": "function_call", "id": "fc_native", "call_id": "call_native",
                                   "name": alias, "arguments": "{}", "status": "completed"},
                                  {"type": "reasoning", "encrypted_content": "opaque"}]
            return _fake_post(response)
        self.native.post.side_effect = upstream
        result = self.post(body)
        self.assertEqual(result.status_code, 200)
        response = result.get_json()
        self.assertEqual(response["output"][0], {"type": "function_call", "id": "fc_native", "call_id": "call_native",
            "namespace": "mcp__codex_worktrees", "name": "list_worktrees", "arguments": "{}", "status": "completed"})
        self.assertEqual(response["tools"], body["tools"])
        self.assertEqual(response["tool_choice"], body["tool_choice"])
        self.assertEqual(response["output"][1]["encrypted_content"], "opaque")
        self.assertNotEqual(response["future_field"]["name"], "list_worktrees")

    def test_interleaved_streams_restore_their_own_namespace_and_terminal_metadata(self):
        def upstream(url, **kwargs):
            alias = kwargs["json"]["tools"][0]["name"]
            item = {"type": "function_call", "id": "fc", "call_id": "call", "name": alias,
                    "arguments": "{}", "status": "completed"}
            response = native_tests.native_body()
            response.update(output=[item], tools=kwargs["json"]["tools"])
            events = [{"type": "response.output_item.added", "item": {**item, "status": "in_progress"}, "future": True},
                      {"type": "response.output_item.done", "item": item},
                      {"type": "response.completed", "response": response}]
            raw = b"".join((f"id: stable\nevent: {event['type']}\ndata: " + json.dumps({**event, "sequence_number": index}) + "\n\n").encode()
                           for index, event in enumerate(events))
            result = _fake_post(None)
            result.iter_content.return_value = iter([raw[:70], raw[70:]])
            return result
        self.native.post.side_effect = upstream
        streams = []
        for namespace in ("alpha", "beta"):
            body = self.body(namespace)
            adapter = proxy._NativeToolAdapter(body)
            prepared = proxy.prepare_native_responses(body, {}, adapter)
            owner = proxy._NativeResponseOwner(upstream("synthetic", json=prepared))
            stream = proxy.generate_native_responses_streaming(owner, tool_adapter=adapter)
            streams.append((stream, next(stream)))
        for namespace, (stream, first) in zip(("alpha", "beta"), streams):
            delivered = first + "".join(stream)
            events = [json.loads(frame.split("data: ", 1)[1]) for frame in delivered.strip().split("\n\n")]
            self.assertEqual(events[0]["item"]["namespace"], namespace)
            self.assertEqual(events[1]["item"]["name"], "list_worktrees")
            self.assertTrue(events[0]["future"])
            self.assertEqual([event["sequence_number"] for event in events], [0, 1, 2])
            self.assertEqual(events[-1]["response"]["output"][0]["namespace"], namespace)
            self.assertEqual(events[-1]["response"]["tools"], self.body(namespace)["tools"])
            self.assertIn("id: stable", delivered)
            self.assertNotIn("ns_", delivered)
            stream.close()

    def test_restoration_does_not_traverse_unknown_event_or_reasoning_data(self):
        adapter = proxy._NativeToolAdapter(self.body())
        alias = next(iter(adapter.by_alias))
        nested = {"item": {"type": "function_call", "name": alias}, "tools": [{"name": "user-data"}],
                  "response": {"output": [{"type": "function_call", "name": alias}]}}
        event = {"type": "future.event", **copy.deepcopy(nested)}
        original = copy.deepcopy(event)
        adapter.restore(event)
        self.assertEqual(event, original)
        response = native_tests.native_body()
        response["output"] = [{"type": "reasoning", "encrypted_content": "opaque", **copy.deepcopy(nested)}]
        original = copy.deepcopy(response)
        adapter.restore(response)
        self.assertEqual(response, original)

    def test_malformed_descriptions_and_plain_identities_rejected_before_inference(self):
        for mode in ("description", "history", "choice"):
            with self.subTest(mode=mode):
                body = self.body()
                if mode == "description":
                    body["tools"][0]["tools"][0]["description"] = 17
                    param = "tools[0].tools[0].description"
                elif mode == "history":
                    body["input"][0].pop("namespace")
                    body["input"][0]["name"] = []
                    param = "input[0].name"
                else:
                    body["tool_choice"] = {"type": "function", "name": []}
                    param = "tool_choice.name"
                self.reset_upstream_calls()
                response = self.post(body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["error"]["param"], param)
                self.assert_no_upstream_calls()

    def test_hash_alias_collision_is_rejected(self):
        body = self.body("a")
        second = copy.deepcopy(body["tools"][0])
        second["name"] = "b"
        body["tools"].append(second)
        self.reset_upstream_calls()
        with patch.object(proxy.hashlib, "sha256") as digest:
            digest.return_value.hexdigest.return_value = "0" * 64
            self.assertEqual(self.post(body).status_code, 400)
        self.assert_no_upstream_calls()


if __name__ == "__main__":
    unittest.main()
