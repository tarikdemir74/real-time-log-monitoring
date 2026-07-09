"""
Stream vs. simulated-batch comparison - answers RQ3 ("how does stream
processing compare to batch processing in terms of detection latency and
responsiveness?").

This does NOT run a second production pipeline. It:
  1. Drives the same controlled TrafficSimulator scenarios as
     run_evaluation.py (imported, not duplicated) so the *live, unchanged*
     streaming LogProcessor processes them exactly as it always does.
  2. Queries the resulting `anomalies` rows for stream-mode latency, using
     the existing `processed_at - detected_at` fields (no new columns).
  3. Invokes `src/LogProcessor/batch_simulator.py` (inside the logprocessor
     container, via `docker compose exec`) to replay the *same* underlying
     `logs_raw`/`logs_agg` data for the same scenario window, at a fixed
     batch interval - reusing the same detection modules, never duplicating
     their logic.
  4. Reports both side by side: detection counts, precision/recall/F1
     (using the same expected-count methodology as run_evaluation.py), and
     latency/responsiveness.

Usage:
    python3 ml/evaluation/run_batch_comparison.py
    python3 ml/evaluation/run_batch_comparison.py --interval-minutes 5
    python3 ml/evaluation/run_batch_comparison.py --scenarios normal mixed

Requires: Python 3.9+ stdlib only on the host, Docker Compose stack already
running (`docker compose up -d` from docker/), executed from anywhere in the
repo. The logprocessor container's own Python environment (psycopg2,
scikit-learn, joblib) is used for the replay step via `docker compose exec`,
so no new host-side dependency is introduced.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_evaluation  # noqa: E402  (sibling module, see sys.path insert above)

REPO_ROOT = run_evaluation.REPO_ROOT
COMPOSE_FILE = run_evaluation.COMPOSE_FILE
RESULTS_DIR = run_evaluation.RESULTS_DIR
DETECTION_METHODS = run_evaluation.DETECTION_METHODS
SCENARIOS = run_evaluation.SCENARIOS


def query_stream_anomalies(start: datetime, end: datetime) -> list:
    sql = (
        "SELECT COALESCE(json_agg(row_to_json(t)), '[]') FROM ("
        "SELECT detection_method, detected_at, processed_at FROM anomalies "
        f"WHERE detected_at BETWEEN '{start.isoformat()}' AND '{end.isoformat()}' "
        "ORDER BY detected_at"
        ") t;"
    )
    cmd = [
        "docker", "compose", "-f", str(COMPOSE_FILE), "exec", "-T", "postgres",
        "psql", "-U", "logmonitor", "-d", "logmonitor", "-t", "-A", "-c", sql,
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(result.stdout.strip())


def run_batch_simulation(start: datetime, end: datetime, interval_minutes: int) -> list:
    cmd = [
        "docker", "compose", "-f", str(COMPOSE_FILE), "exec", "-T", "logprocessor",
        "python", "batch_simulator.py",
        "--start", start.isoformat(),
        "--end", end.isoformat(),
        "--interval-minutes", str(interval_minutes),
    ]
    print(f"[BatchComparison] Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    return json.loads(result.stdout.strip())


def _score(detected: int, expected: int) -> dict:
    tp = min(detected, expected)
    fp = max(detected - expected, 0)
    fn = max(expected - detected, 0)
    precision = tp / (tp + fp) if (tp + fp) > 0 else None
    recall = tp / (tp + fn) if (tp + fn) > 0 else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and (precision + recall) > 0
        else None
    )
    return {"detected": detected, "expected": expected, "tp": tp, "fp": fp, "fn": fn,
            "precision": precision, "recall": recall, "f1": f1}


def _latency_stats(latencies: list) -> dict:
    if not latencies:
        return {"min_latency_s": None, "mean_latency_s": None, "max_latency_s": None}
    return {
        "min_latency_s": min(latencies),
        "mean_latency_s": sum(latencies) / len(latencies),
        "max_latency_s": max(latencies),
    }


def evaluate_scenario(scenario: dict, grace_seconds: float, interval_minutes: int) -> dict:
    name = scenario["name"]
    print(f"\n[BatchComparison] === Scenario: {name} ===")

    start = datetime.now(timezone.utc)
    run_evaluation.run_traffic_simulator(scenario["mode"], scenario["count"], scenario["delay"])
    print(f"[BatchComparison] Waiting {grace_seconds}s grace period for LogProcessor to finish consuming...")
    subprocess.run(["sleep", str(grace_seconds)], check=True)
    end = datetime.now(timezone.utc)

    expected = scenario["expected_fn"](scenario["count"])

    # --- Stream mode: query the anomalies the live pipeline already produced ---
    stream_rows = query_stream_anomalies(start, end)
    stream_by_method = {method: [] for method in DETECTION_METHODS}
    for row in stream_rows:
        method = row["detection_method"]
        if method in stream_by_method:
            detected_at = datetime.fromisoformat(row["detected_at"].replace("Z", "+00:00"))
            processed_at = datetime.fromisoformat(row["processed_at"].replace("Z", "+00:00"))
            stream_by_method[method].append((processed_at - detected_at).total_seconds())

    stream_results = {}
    for method in DETECTION_METHODS:
        latencies = stream_by_method[method]
        stream_results[method] = {**_score(len(latencies), expected), **_latency_stats(latencies)}

    # --- Batch mode: replay the same underlying logs_raw/logs_agg data ---
    batch_anomalies = run_batch_simulation(start, end, interval_minutes)
    batch_by_method = {method: [] for method in DETECTION_METHODS}
    for anomaly in batch_anomalies:
        method = anomaly.get("detection_method")
        if method in batch_by_method:
            batch_by_method[method].append(anomaly["latency_seconds"])

    batch_results = {}
    for method in DETECTION_METHODS:
        latencies = batch_by_method[method]
        batch_results[method] = {**_score(len(latencies), expected), **_latency_stats(latencies)}

    return {
        "scenario": name, "mode": scenario["mode"], "count": scenario["count"],
        "start": start.isoformat(), "end": end.isoformat(), "expected": expected,
        "stream": stream_results, "batch": batch_results,
    }


def aggregate_overall(scenario_results: list, key: str) -> dict:
    overall = {}
    for method in DETECTION_METHODS:
        tp = sum(r[key][method]["tp"] for r in scenario_results)
        fp = sum(r[key][method]["fp"] for r in scenario_results)
        fn = sum(r[key][method]["fn"] for r in scenario_results)
        detected = sum(r[key][method]["detected"] for r in scenario_results)
        expected = sum(r["expected"] for r in scenario_results)
        precision = tp / (tp + fp) if (tp + fp) > 0 else None
        recall = tp / (tp + fn) if (tp + fn) > 0 else None
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision is not None and recall is not None and (precision + recall) > 0
            else None
        )
        all_latencies = [
            v for r in scenario_results
            for v in ([r[key][method]["mean_latency_s"]] if r[key][method]["mean_latency_s"] is not None else [])
        ]
        overall[method] = {
            "detected": detected, "expected": expected, "tp": tp, "fp": fp, "fn": fn,
            "precision": precision, "recall": recall, "f1": f1,
            "mean_latency_s": sum(all_latencies) / len(all_latencies) if all_latencies else None,
        }
    return overall


def _fmt(value, digits=2):
    return f"{value:.{digits}f}" if value is not None else "n/a"


def render_markdown(scenario_results: list, stream_overall: dict, batch_overall: dict,
                     run_started_at: datetime, interval_minutes: int) -> str:
    lines = []
    lines.append("# Stream vs. Simulated-Batch Comparison (RQ3)")
    lines.append("")
    lines.append(f"Run started (UTC): {run_started_at.isoformat()}")
    lines.append(f"Batch interval: {interval_minutes} minute(s)")
    lines.append("")
    lines.append("## How to reproduce")
    lines.append("")
    lines.append("```")
    lines.append("docker compose -f docker/docker-compose.yml up -d")
    lines.append(f"python3 ml/evaluation/run_batch_comparison.py --interval-minutes {interval_minutes}")
    lines.append("```")
    lines.append("")
    lines.append(
        "Stream mode uses the live, unchanged LogProcessor - detection runs immediately per message. "
        "Batch mode is a replay-based simulation (`src/LogProcessor/batch_simulator.py`) over the same "
        "underlying `logs_raw`/`logs_agg` data, using the same detection modules, but only executing "
        "detection once per batch interval. See CLAUDE.md \"Batch simulation (RQ3)\" for the full design."
    )
    lines.append("")
    lines.append("## Overall (aggregated across all scenarios)")
    lines.append("")
    lines.append("| Mode | Method | Detected | Expected | Precision | Recall | F1 | Mean latency (s) |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for mode_name, overall in (("stream", stream_overall), ("batch", batch_overall)):
        for method in DETECTION_METHODS:
            o = overall[method]
            lines.append(
                f"| {mode_name} | {method} | {o['detected']} | {o['expected']} | "
                f"{_fmt(o['precision'])} | {_fmt(o['recall'])} | {_fmt(o['f1'])} | {_fmt(o['mean_latency_s'])} |"
            )
    lines.append("")
    lines.append("## Per-scenario results")
    lines.append("")
    lines.append(
        "| Scenario | Method | Mode | Detected | Expected | Precision | Recall | F1 | "
        "Min latency (s) | Mean latency (s) | Max latency (s) |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for result in scenario_results:
        for method in DETECTION_METHODS:
            for mode_name in ("stream", "batch"):
                m = result[mode_name][method]
                lines.append(
                    f"| {result['scenario']} | {method} | {mode_name} | {m['detected']} | {m['expected']} | "
                    f"{_fmt(m['precision'])} | {_fmt(m['recall'])} | {_fmt(m['f1'])} | "
                    f"{_fmt(m['min_latency_s'])} | {_fmt(m['mean_latency_s'])} | {_fmt(m['max_latency_s'])} |"
                )
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append(
        "- Precision/recall/F1 use the same count-based approximation methodology as "
        "`run_evaluation.py` (see that script's generated report for the full caveats) - the same "
        "`expected` count is used for both stream and batch so the two are directly comparable."
    )
    lines.append(
        "- Stream latency = `processed_at - detected_at` (existing columns, application-level "
        "processing time, not RabbitMQ queue time)."
    )
    lines.append(
        f"- Batch latency = simulated `batch_execution_time - detected_at`, where `batch_execution_time` "
        f"is the next {interval_minutes}-minute boundary after the anomaly's window closed - deterministic, "
        "not a real wall-clock wait."
    )
    lines.append("")
    return "\n".join(lines)


def render_csv(scenario_results: list) -> str:
    lines = [
        "scenario,method,mode,detected,expected,tp,fp,fn,precision,recall,f1,"
        "min_latency_s,mean_latency_s,max_latency_s"
    ]
    for result in scenario_results:
        for method in DETECTION_METHODS:
            for mode_name in ("stream", "batch"):
                m = result[mode_name][method]

                def fmt(v):
                    return "" if v is None else f"{v:.4f}"

                lines.append(
                    f"{result['scenario']},{method},{mode_name},{m['detected']},{m['expected']},{m['tp']},"
                    f"{m['fp']},{m['fn']},{fmt(m['precision'])},{fmt(m['recall'])},{fmt(m['f1'])},"
                    f"{fmt(m['min_latency_s'])},{fmt(m['mean_latency_s'])},{fmt(m['max_latency_s'])}"
                )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare stream vs. simulated-batch detection (RQ3).")
    parser.add_argument(
        "--scenarios", nargs="+", choices=[s["name"] for s in SCENARIOS], default=None,
        help="Subset of scenarios to run (default: all).",
    )
    parser.add_argument("--grace-seconds", type=float, default=5.0)
    parser.add_argument("--interval-minutes", type=int, default=5, help="Simulated batch interval (default: 5).")
    args = parser.parse_args()

    selected = [s for s in SCENARIOS if s["name"] in args.scenarios] if args.scenarios else SCENARIOS

    run_started_at = datetime.now(timezone.utc)
    scenario_results = [
        evaluate_scenario(scenario, args.grace_seconds, args.interval_minutes) for scenario in selected
    ]
    stream_overall = aggregate_overall(scenario_results, "stream")
    batch_overall = aggregate_overall(scenario_results, "batch")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = run_started_at.strftime("%Y%m%dT%H%M%SZ")
    md_path = RESULTS_DIR / f"batch_comparison_{timestamp}.md"
    csv_path = RESULTS_DIR / f"batch_comparison_{timestamp}.csv"

    md_path.write_text(render_markdown(scenario_results, stream_overall, batch_overall, run_started_at, args.interval_minutes))
    csv_path.write_text(render_csv(scenario_results))

    print("\n[BatchComparison] === Overall summary ===")
    header = f"{'mode':<8}{'method':<18}{'detected':>10}{'expected':>10}{'precision':>11}{'recall':>9}{'f1':>7}{'latency(s)':>12}"
    print(header)
    for mode_name, overall in (("stream", stream_overall), ("batch", batch_overall)):
        for method in DETECTION_METHODS:
            o = overall[method]
            print(
                f"{mode_name:<8}{method:<18}{o['detected']:>10}{o['expected']:>10}"
                f"{_fmt(o['precision']):>11}{_fmt(o['recall']):>9}{_fmt(o['f1']):>7}{_fmt(o['mean_latency_s']):>12}"
            )

    print(f"\n[BatchComparison] Markdown report: {md_path}")
    print(f"[BatchComparison] CSV report:      {csv_path}")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print(f"[BatchComparison] Command failed: {exc}", file=sys.stderr)
        if exc.stderr:
            print(exc.stderr, file=sys.stderr)
        sys.exit(1)
