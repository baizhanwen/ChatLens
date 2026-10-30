"""Runner acceptance semantics with a bounded, explicitly synthetic reader.

The reader core has independent encrypted DB/WAL tests. These checks exercise
phase verdicts and report safety without discovering any real account or keys.
"""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import wechat_selected_reader as reader_dependency


PREPARE_STUB = ModuleType("prepare_wechat_read_test")


def refuse_account_discovery():
    raise AssertionError("synthetic runner tests never discover actual accounts")


PREPARE_STUB.only_database_root = refuse_account_discovery
SPEC = importlib.util.spec_from_file_location(
    "wechat_selected_runner_under_test",
    Path(__file__).resolve().parents[1] / "scripts" / "run_wechat_selected_test.py")
runner = importlib.util.module_from_spec(SPEC)
with patch.dict(sys.modules, {"prepare_wechat_read_test": PREPARE_STUB,
                              "wechat_selected_reader": reader_dependency}):
    SPEC.loader.exec_module(runner)


def event(alias, index, **changes):
    row = {"alias": alias, "token_index": index,
           "event_id_digest": "synthetic-%s-%d" % (alias, index),
           "sender_digest": "synthetic-sender-digest", "body_exact_match": True,
           "expected_sender_match": True, "background_confirmed": True}
    row.update(changes)
    return row


FIRST_FOUR = [event("private_test", 1), event("private_test", 2),
              event("group_test", 1), event("group_test", 2)]
LAST_TWO = [event("private_test", 3), event("group_test", 3)]


class SyntheticReader:
    def __init__(self, initial=(), new=(), resumed=False):
        self._observations = copy.deepcopy(list(initial))
        self._new = copy.deepcopy(list(new))
        self.resumed = resumed
        self.restored_observations = len(self._observations)
        self.closed = False
        self.steps = 0

    @property
    def observations(self):
        return copy.deepcopy(self._observations)

    def step(self):
        self.steps += 1
        rows, self._new = self._new, []
        self._observations.extend(copy.deepcopy(rows))
        return rows

    def summary(self):
        conversations = []
        complete = True
        for alias in ("private_test", "group_test"):
            tokens = []
            for index in range(1, 4):
                matches = [row for row in self._observations
                           if row["alias"] == alias and row["token_index"] == index]
                tokens.append({"token_index": index, "matched_count": len(matches)})
                complete = complete and len(matches) == 1
            conversations.append({"alias": alias, "tokens": tokens})
        return {"status": "通过" if complete else "未验证",
                "all_expected_tokens_observed": complete,
                "baseline_completed_at": "2030-01-01T00:00:00+00:00",
                "conversations": conversations}

    def close(self):
        self.closed = True


class SelectedRunnerAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chatlens-synthetic-runner-")
        self.root = Path(self.temp.name)
        self.config_file = self.root / "synthetic_config.json"
        config = {"run_id": "synthetic-runner", "conversations": [
            {"alias": "private_test", "chat_id": "wxid_synthetic_private",
             "tokens": ["SYNTHETIC_PRIVATE_1", "SYNTHETIC_PRIVATE_2", "SYNTHETIC_PRIVATE_3"],
             "expected_sender_id": "wxid_synthetic_sender"},
            {"alias": "group_test", "chat_id": "synthetic_test@chatroom",
             "tokens": ["SYNTHETIC_GROUP_1", "SYNTHETIC_GROUP_2", "SYNTHETIC_GROUP_3"],
             "expected_sender_id": "wxid_synthetic_sender"},
        ]}
        self.config_file.write_text(json.dumps(config), encoding="utf-8")
        self.args = SimpleNamespace(config=self.config_file,
                                    key_file=self.root / "never-read-synthetic-keys.json",
                                    state_file=self.root / "synthetic-state.json",
                                    output=self.root / "public-report.json",
                                    native_probe=self.root / "never-run-native-probe",
                                    phase="live", stop_after_total=4, seconds=1)

    def tearDown(self):
        self.temp.cleanup()

    def run_with(self, fake, args=None, factory=None):
        args = args or self.args
        emitted = []
        with patch.object(runner, "SelectedReader", side_effect=factory or (lambda *a, **k: fake)) as constructor, \
                patch.object(runner, "only_database_root", return_value=self.root / "synthetic-db-root"), \
                patch.object(runner, "emit", side_effect=lambda kind, **fields: emitted.append(dict(event=kind, **fields))), \
                patch.object(runner.signal, "signal", return_value=runner.signal.SIG_DFL), \
                patch.object(runner.time, "sleep", side_effect=AssertionError("synthetic batch must finish without waiting")):
            code = runner.run(args)
        return code, emitted, constructor

    def report(self, args=None):
        return json.loads((args or self.args).output.read_text())

    def test_live_phase_four_of_six_can_pass_without_claiming_complete_summary(self):
        fake = SyntheticReader(new=FIRST_FOUR)
        code, _, _ = self.run_with(fake)
        report = self.report()
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "通过")
        self.assertEqual(report["summary"]["status"], "未验证")
        self.assertEqual(len(report["new_observations"]), 4)
        self.assertEqual(report["restored_observations"], 0)
        self.assertEqual(report["replayed_committed_event_count"], 0)
        self.assertTrue(report["phase_plan_verified"])
        self.assertFalse(report["summary"]["all_expected_tokens_observed"])
        self.assertTrue(fake.closed)

    def test_four_events_with_cross_phase_token_distribution_cannot_pass_live(self):
        wrong_phase = [event("private_test", 1), event("private_test", 2),
                       event("private_test", 3), event("group_test", 1)]
        fake = SyntheticReader(new=wrong_phase)
        code, _, _ = self.run_with(fake)
        report = self.report()
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "失败")
        self.assertEqual(len(report["cumulative_observations"]), 4)
        self.assertEqual(report["unexpected_repeated_test_token_count"], 0)
        self.assertFalse(report["phase_plan_verified"])

    def test_resume_requires_an_existing_checkpoint(self):
        self.args.phase, self.args.stop_after_total = "resume", 6
        fake = SyntheticReader(new=FIRST_FOUR + LAST_TWO, resumed=False)
        code, _, _ = self.run_with(fake)
        report = self.report()
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "待外部条件")
        self.assertEqual(report["reason"], "selected_resume_requires_existing_checkpoint")
        self.assertEqual(fake.steps, 0)
        self.assertTrue(fake.closed)

    def test_resume_restores_four_and_reports_only_two_new_unique_events(self):
        self.args.phase, self.args.stop_after_total = "resume", 6
        fake = SyntheticReader(initial=FIRST_FOUR, new=LAST_TWO, resumed=True)
        code, _, _ = self.run_with(fake)
        report = self.report()
        self.assertEqual(code, 0)
        self.assertTrue(report["resumed"])
        self.assertEqual(report["restored_observations"], 4)
        self.assertEqual(len(report["new_observations"]), 2)
        self.assertEqual(len(report["cumulative_observations"]), 6)
        self.assertEqual(report["summary"]["status"], "通过")
        self.assertTrue(report["summary"]["all_expected_tokens_observed"])
        self.assertTrue(report["phase_plan_verified"])
        self.assertEqual(report["replayed_committed_event_count"], 0)
        old_ids = {row["event_id_digest"] for row in FIRST_FOUR}
        self.assertFalse(old_ids.intersection(row["event_id_digest"] for row in report["new_observations"]))

    def test_unknown_sender_or_frontmost_observation_cannot_pass(self):
        cases = ({"expected_sender_match": None}, {"background_confirmed": False})
        for number, changes in enumerate(cases):
            with self.subTest(changes=changes):
                args = copy.copy(self.args)
                args.output = self.root / ("bad-identity-%d.json" % number)
                new = copy.deepcopy(FIRST_FOUR)
                new[0].update(changes)
                fake = SyntheticReader(new=new)
                code, _, _ = self.run_with(fake, args)
                self.assertEqual(code, 1)
                self.assertEqual(self.report(args)["status"], "失败")
                self.assertEqual(self.report(args)["reason"], "selected_sample_identity_background_or_cardinality_mismatch")

    def test_replayed_old_event_id_fails_resume_even_when_token_counts_match(self):
        self.args.phase, self.args.stop_after_total = "resume", 6
        new = copy.deepcopy(LAST_TWO)
        new[0]["event_id_digest"] = FIRST_FOUR[0]["event_id_digest"]
        fake = SyntheticReader(initial=FIRST_FOUR, new=new, resumed=True)
        code, _, _ = self.run_with(fake)
        report = self.report()
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "失败")
        self.assertEqual(report["replayed_committed_event_count"], 1)
        self.assertEqual(report["unexpected_repeated_test_token_count"], 0)

    def test_failure_never_overwrites_an_existing_report_even_if_created_during_run(self):
        sentinel = b'{"synthetic_existing_evidence":"preserve-exactly"}\n'
        self.args.output.write_bytes(sentinel)
        code, _, constructor = self.run_with(SyntheticReader(new=FIRST_FOUR))
        self.assertEqual(code, 2)
        constructor.assert_not_called()
        self.assertEqual(self.args.output.read_bytes(), sentinel)
        self.args.output.unlink()

        def fail_after_competing_writer(*unused, **unused_named):
            self.args.output.write_bytes(sentinel)
            raise RuntimeError("SYNTHETIC_PRIVATE_ERROR_DETAIL_MUST_NOT_BE_PRINTED")

        code, emitted, _ = self.run_with(None, factory=fail_after_competing_writer)
        self.assertEqual(code, 2)
        self.assertEqual(self.args.output.read_bytes(), sentinel)
        self.assertNotIn("SYNTHETIC_PRIVATE_ERROR_DETAIL_MUST_NOT_BE_PRINTED", json.dumps(emitted))
        self.assertEqual(emitted[-1]["event"], "report_write_failed")


if __name__ == "__main__":
    unittest.main()
