#!/usr/bin/env python3
"""Finite selected-conversation test; emits sanitized evidence only."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import time

from prepare_wechat_read_test import only_database_root
from wechat_selected_reader import SelectedReader, CheckError, utc_now


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--native-probe", type=Path, default=Path(__file__).resolve().parent / "native/bin/macos-probe")
    parser.add_argument("--phase", choices=["live", "resume"], required=True)
    parser.add_argument("--stop-after-total", type=int, required=True)
    parser.add_argument("--seconds", type=int, default=300)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 300 or not 1 <= args.stop_after_total <= 512:
        parser.error("seconds must be 1..300 and stop-after-total 1..512")
    return args


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        json.dump(report, target, ensure_ascii=False, indent=2)
        target.write("\n")


def run(args):
    reader = None
    handlers = {}
    stopping = {"requested": False}
    started = time.monotonic()
    report = {"recorded_at": utc_now(), "phase": args.phase,
              "scope": "two_selected_conversations_controlled_new_text_only",
              "status": "未验证", "reason": "controlled_test_not_finished",
              "stop_after_total": args.stop_after_total, "resumed": False,
              "restored_observations": 0, "new_observations": [],
              "replayed_committed_event_count": None, "summary": None,
              "errors": []}

    def request_stop(signum, frame):
        stopping["requested"] = True

    try:
        if args.output.exists():
            raise CheckError("selected_public_report_already_exists", "待外部条件")
        config = json.loads(args.config.read_text(encoding="utf-8"))
        conversations = config.get("conversations", [])
        aliases = {row.get("alias") for row in conversations if isinstance(row, dict)}
        if len(conversations) != 2 or aliases != {"private_test", "group_test"} or any(len(row.get("tokens", [])) != 3 for row in conversations):
            raise CheckError("selected_two_phase_test_configuration_invalid", "待外部条件")
        live_pairs = {(alias, index) for alias in aliases for index in (1, 2)}
        paused_pairs = {(alias, 3) for alias in aliases}
        expected_total = 4 if args.phase == "live" else 6
        if args.stop_after_total != expected_total:
            raise CheckError("selected_two_phase_target_invalid", "待外部条件")
        reader = SelectedReader(only_database_root(), args.key_file, config,
                                args.state_file, args.native_probe)
        initial = reader.observations
        initial_ids = {row["event_id_digest"] for row in initial}
        report["resumed"] = bool(reader.resumed)
        report["restored_observations"] = int(reader.restored_observations)
        if args.phase == "resume" and not reader.resumed:
            raise CheckError("selected_resume_requires_existing_checkpoint", "待外部条件")
        if args.phase == "live" and initial:
            raise CheckError("selected_live_phase_already_has_results", "待外部条件")
        if args.phase == "resume" and (len(initial) != 4 or
                {(row["alias"], row["token_index"]) for row in initial} != live_pairs or
                not all(row["body_exact_match"] is True and row["expected_sender_match"] is True and
                        row["background_confirmed"] is True for row in initial)):
            raise CheckError("selected_resume_requires_verified_live_phase", "待外部条件")
        if args.stop_after_total <= len(initial):
            raise CheckError("selected_phase_target_has_no_new_sample", "待外部条件")
        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.signal(signum, request_stop)
        emit("ready", phase=args.phase, resumed=report["resumed"],
             restored_observations=len(initial), stop_after_total=args.stop_after_total,
             baseline_completed_at=reader.summary()["baseline_completed_at"])
        deadline = time.monotonic() + args.seconds
        while not stopping["requested"] and time.monotonic() < deadline:
            observations = reader.step()
            report["new_observations"].extend(observations)
            for observation in observations:
                emit("controlled_message_observed", alias=observation["alias"],
                     token_index=observation["token_index"],
                     body_exact_match=observation["body_exact_match"],
                     expected_sender_match=observation["expected_sender_match"],
                     background_confirmed=observation["background_confirmed"])
            if len(reader.observations) >= args.stop_after_total:
                break
            time.sleep(1)
        summary = reader.summary()
        report["summary"] = summary
        report["cumulative_observations"] = reader.observations
        replayed = sum(row["event_id_digest"] in initial_ids for row in report["new_observations"])
        report["replayed_committed_event_count"] = replayed
        duplicates = sum(max(token["matched_count"] - 1, 0)
                         for chat in summary["conversations"] for token in chat["tokens"])
        report["unexpected_repeated_test_token_count"] = duplicates
        observed = reader.observations
        target_met = len(observed) == args.stop_after_total
        identities_ok = all(row["body_exact_match"] is True and
                            row["expected_sender_match"] is True and
                            row["background_confirmed"] is True for row in observed)
        observed_pairs = {(row["alias"], row["token_index"]) for row in observed}
        new_pairs = {(row["alias"], row["token_index"]) for row in report["new_observations"]}
        plan_ok = (observed_pairs == live_pairs and new_pairs == live_pairs) if args.phase == "live" else (
            observed_pairs == live_pairs | paused_pairs and new_pairs == paused_pairs and
            len(report["new_observations"]) == 2 and summary["all_expected_tokens_observed"] is True)
        report["phase_plan_verified"] = plan_ok
        if target_met and identities_ok and plan_ok and duplicates == 0 and replayed == 0:
            report.update(status="通过", reason="phase_controlled_samples_verified")
        elif observed and (not identities_ok or duplicates or replayed or len(observed) > args.stop_after_total or (target_met and not plan_ok)):
            report.update(status="失败", reason="selected_sample_identity_background_or_cardinality_mismatch")
    except CheckError as failure:
        report.update(status=failure.status, reason=failure.reason)
        report["errors"].append({"type": "CheckError", "reason": failure.reason})
    except Exception as failure:
        report.update(status="失败", reason="selected_test_operation_failed")
        report["errors"].append({"type": type(failure).__name__, "reason": "selected_test_operation_failed"})
    finally:
        if reader is not None:
            reader.close()
        for signum, previous in handlers.items():
            signal.signal(signum, previous)
    report["finished_at"] = utc_now()
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    try:
        write_report(args.output, report)
    except Exception as failure:
        emit("report_write_failed", error_type=type(failure).__name__)
        return 2
    emit("complete", phase=args.phase, status=report["status"], reason=report["reason"],
         new_observation_count=len(report["new_observations"]),
         restored_observations=report["restored_observations"])
    return 0 if report["status"] == "通过" else 1


if __name__ == "__main__":
    sys.exit(run(arguments()))
