import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from weasley_trace_replay import compare, replay
from weasley_trace_replay.tau2 import AdapterError, adapt_episode, minimize_episode, read_episode

FIXTURE = Path(__file__).resolve().parents[1] / "examples/tau2/task46-trial0.json"
CALL = "call_jSwZRs774js2PBliHRee5crf"


class Tau2AdapterTests(unittest.TestCase):
    def setUp(self):
        self.row = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_native_identity_outcome_and_exact_output_bytes_are_mapped(self):
        result = adapt_episode(self.row)
        report = replay(result["events"])
        self.assertEqual(result["adapter"], "tau2-single-call-transfer-v1")
        self.assertEqual(report["run"], self.row["id"])
        self.assertEqual(report["tools"][CALL], dict(actor="assistant", step="conversation",
                         tool="transfer_to_human_agents", status="ok",
                         digest="bccdc18e7b6ac085553ef044cadec50c310dffd4a86277e0c5c3940aa0e9ce04"))
        self.row["messages"][3].update(content="Transfer successful\n\u00e9", error=True)
        changed = replay(adapt_episode(self.row)["events"])
        self.assertEqual(changed["tools"][CALL]["status"], "error")
        self.assertEqual(changed["tools"][CALL]["digest"],
                         hashlib.sha256("Transfer successful\n\u00e9".encode("utf-8")).hexdigest())

    def test_incomplete_assistant_usage_is_unreported_and_user_usage_is_excluded(self):
        converted = adapt_episode(self.row)
        self.assertEqual(converted["provenance"]["usage"], {
            "assistant_observed_turns": [2, 4], "assistant_unreported_turns": [0],
            "source_assistant_complete": False, "emitted": False,
            "replay_v1_aggregate": "unreported"})
        self.assertFalse(any(event["type"] in ("usage", "checkpoint") for event in converted["events"]))
        self.assertEqual(replay(converted["events"])["usage"], {"input_tokens": 0, "output_tokens": 0})

    def test_metadata_projection_is_detached_and_keeps_trace_equivalent(self):
        native = copy.deepcopy(self.row)
        native.update(timestamp="historical", duration=1.0, reward_info={"ignored": "not a success oracle"})
        native["messages"][2].update(raw_data={"uninterpreted": "duplicate"}, timestamp="historical", cost=0.5)
        full = adapt_episode(native)
        minimal = minimize_episode(native)
        self.assertEqual(minimal, self.row)
        self.assertEqual(full["events"], adapt_episode(minimal)["events"])
        self.assertNotEqual(full["provenance"]["input_sha256"], adapt_episode(minimal)["provenance"]["input_sha256"])
        omitted = {entry["path"] for entry in full["losses"]}
        self.assertTrue({"/reward_info", "/duration", "/messages/2/raw_data",
                         "/messages/2/tool_calls/0/arguments", "/messages/4/content"} <= omitted)
        minimal["messages"][2]["tool_calls"][0]["arguments"].clear()
        self.assertTrue(native["messages"][2]["tool_calls"][0]["arguments"])

    def test_changed_output_is_drift_but_arguments_and_final_text_are_not_replay_observations(self):
        baseline = adapt_episode(self.row)
        original_report = replay(baseline["events"])
        output = copy.deepcopy(self.row)
        output["messages"][3]["content"] += " changed"
        drift = compare(original_report, replay(adapt_episode(output)["events"]))
        self.assertFalse(drift["equivalent"])
        self.assertEqual([entry["path"] for entry in drift["changes"]], [f"/tools/{CALL}/digest"])
        for index in (2, 4):
            edited = copy.deepcopy(self.row)
            if index == 2:
                edited["messages"][2]["tool_calls"][0]["arguments"]["summary"] = "different argument"
            else:
                edited["messages"][4]["content"] = "Different final assistant text."
            converted = adapt_episode(edited)
            self.assertTrue(compare(original_report, replay(converted["events"]))["equivalent"])
            self.assertNotEqual(converted["provenance"]["input_sha256"], baseline["provenance"]["input_sha256"])

    def test_missing_duplicate_orphan_and_wrong_owner_calls_are_rejected(self):
        mutations = [
            lambda r: r["messages"][2].update(tool_calls=None),
            lambda r: r["messages"][2]["tool_calls"].append(copy.deepcopy(r["messages"][2]["tool_calls"][0])),
            lambda r: r["messages"].pop(3),
            lambda r: r["messages"].insert(4, copy.deepcopy(r["messages"][3])),
            lambda r: r["messages"][3].update(id="orphan"),
            lambda r: r["messages"][3].update(requestor="user"),
            lambda r: r["messages"][2]["tool_calls"][0].update(requestor="user"),
            lambda r: r["messages"][1].update(tool_calls=copy.deepcopy(r["messages"][2]["tool_calls"])),
        ]
        for mutate in mutations:
            invalid = copy.deepcopy(self.row)
            mutate(invalid)
            with self.subTest(mutation=mutations.index(mutate)), self.assertRaises(AdapterError):
                adapt_episode(invalid)

    def test_outcome_is_required_exact_boolean_and_output_is_required_text(self):
        for field, values in (("error", [None, 0, 1, "false", []]), ("content", [None, {}, 7])):
            for value in values:
                invalid = copy.deepcopy(self.row)
                invalid["messages"][3][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(AdapterError):
                    adapt_episode(invalid)
        del self.row["messages"][3]["error"]
        with self.assertRaises(AdapterError):
            adapt_episode(self.row)

    def test_only_exact_supported_user_terminal_and_order_are_accepted(self):
        for index in (0, 1, 4):
            for marker in ("###STOP###", "###TRANSFER###", "###OUT-OF-SCOPE###"):
                invalid = copy.deepcopy(self.row)
                invalid["messages"][index]["content"] = "text " + marker
                with self.subTest(index=index, marker=marker), self.assertRaises(AdapterError):
                    adapt_episode(invalid)
        for terminal in ("agent_stop", "timeout", "max_steps", "unknown", None):
            with self.subTest(terminal=terminal), self.assertRaises(AdapterError):
                adapt_episode(dict(self.row, termination_reason=terminal))
        for value in ("###STOP###", " ###TRANSFER###", "###TRANSFER###\n", "not done"):
            invalid = copy.deepcopy(self.row)
            invalid["messages"][5]["content"] = value
            with self.assertRaises(AdapterError):
                adapt_episode(invalid)
        for value in (True, 3, 6):
            invalid = copy.deepcopy(self.row)
            invalid["messages"][5]["turn_idx"] = value
            with self.assertRaises(AdapterError):
                adapt_episode(invalid)

    def test_unknown_fields_invalid_identifiers_and_nonfinite_metadata_fail_closed(self):
        mutations = [lambda r: r.update(extra="private"),
                     lambda r: r["messages"][0].update(audio="unsupported"),
                     lambda r: r.update(id="123-not-v1"),
                     lambda r: r.update(trial=True),
                     lambda r: r["messages"][2]["tool_calls"][0].update(id="bad/id"),
                     lambda r: r["messages"][2]["tool_calls"][0].update(name="bad name"),
                     lambda r: r["messages"][2]["usage"].update(prompt_tokens=True),
                     lambda r: r.update(duration=float("nan"))]
        for mutate in mutations:
            invalid = copy.deepcopy(self.row)
            mutate(invalid)
            with self.assertRaises(AdapterError):
                adapt_episode(invalid)

    def test_direct_api_rejects_coerced_json_values_in_all_source_subtrees(self):
        class String(str): pass
        class Integer(int): pass
        class Array(list): pass
        class Object(dict): pass
        invalid_values = [{1: "private"}, {None: "private"}, {True: "private"},
                          (1, 2), b"private", {"nested": (1, 2)},
                          String("private"), Integer(1), Array([1]), Object(a=1)]
        for value in invalid_values:
            for location in ("metadata", "arguments", "raw_data"):
                row = copy.deepcopy(self.row)
                if location == "metadata": row["reward_info"] = value
                elif location == "arguments": row["messages"][2]["tool_calls"][0]["arguments"] = {"nested": value}
                else: row["messages"][0]["raw_data"] = value
                for operation in (adapt_episode, minimize_episode):
                    with self.subTest(value=type(value).__name__, location=location, operation=operation.__name__):
                        with self.assertRaises(AdapterError) as caught:
                            operation(row)
                        self.assertNotIn("private", str(caught.exception))

    def test_shared_plain_json_values_are_valid_and_canonical_hash_is_unchanged(self):
        shared = {"unicode": "é", "values": [None, True, False, 0, -1, 1.25]}
        row = copy.deepcopy(self.row)
        row["reward_info"] = {"one": shared, "two": shared}
        expected = hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":"),
                                            ensure_ascii=True, allow_nan=False).encode("utf-8")).hexdigest()
        self.assertEqual(adapt_episode(row)["provenance"]["input_sha256"], expected)
        self.assertEqual(minimize_episode(row), self.row)

    def test_cyclic_and_deep_source_values_raise_payload_free_adapter_error(self):
        cycle = []; cycle.append(cycle)
        deep = None
        for _ in range(2000): deep = [deep]
        for value in (cycle, deep):
            row = copy.deepcopy(self.row); row["reward_info"] = value
            for operation in (adapt_episode, minimize_episode):
                with self.assertRaises(AdapterError): operation(row)

    def test_bounded_reader_rejects_duplicate_keys_torn_json_and_invalid_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "episode.json"
            raw = FIXTURE.read_bytes()
            path.write_bytes(raw)
            self.assertEqual(read_episode(path, max_bytes=len(raw)), self.row)
            with self.assertRaises(AdapterError):
                read_episode(path, max_bytes=len(raw)-1)
            for raw in (b'\xff', b'{', b'[]', b'{"id":1,"id":2}', b'{"cost":NaN}', b'{"cost":1e400}'):
                path.write_bytes(raw)
                with self.assertRaises(AdapterError):
                    read_episode(path)


if __name__ == "__main__":
    unittest.main()
