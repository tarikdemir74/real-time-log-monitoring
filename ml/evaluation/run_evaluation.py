"""
Evaluation harness comparing the four anomaly detection methods
(rule_based, isolation_forest, dynamic_baseline, hybrid) using controlled
TrafficSimulator scenarios.

How it works (see "Limitations" in the generated report for the caveats):
  1. For each scenario, it runs `docker compose run --rm traffic-simulator`
     with a fixed --mode/--count/--delay, recording the wall-clock window
     the run occupied.
  2. It waits a short grace period for LogProcessor to finish consuming the
     resulting RabbitMQ messages, then queries the `anomalies` table
     (via `docker compose exec postgres psql`) for rows whose `detected_at`
     falls inside that window, grouped by detection_method.
  3. "Expected anomaly count" per scenario is *inferred*, not recorded by
     TrafficSimulator - it mirrors TrafficSimulator's own deterministic
     dispatch logic (documented in CLAUDE.md) rather than any label
     TrafficSimulator emits, since TrafficSimulator never learns DemoApi's
     internal request_id and so cannot tag its own requests as
     anomalous/normal for us.
  4. Detected vs. expected counts are combined into TP/FP/FN counts, and
     precision/recall/F1 are derived from those - see Limitations for why
     this is a count-based approximation, not exact per-event matching.

Usage:
    python3 ml/evaluation/run_evaluation.py
    python3 ml/evaluation/run_evaluation.py --scenarios normal latency
    python3 ml/evaluation/run_evaluation.py --grace-seconds 8

Requires: Python 3.9+ stdlib only (no pip install), Docker Compose stack
already running (`docker compose up -d` from docker/), executed from
anywhere in the repo.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker" / "docker-compose.yml"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

DETECTION_METHODS = ["rule_based", "isolation_forest", "dynamic_baseline", "hybrid"]


def _expected_normal(count: int) -> int:
    return 0


def _expected_latency(count: int) -> int:
    # TrafficSimulator's "latency" mode sets X-Simulate-Latency on exactly
    # one of the 4 endpoints per iteration (src/TrafficSimulator/main.py).
    return count


def _expected_errors(count: int) -> int:
    # "errors" mode runs exactly one invalid request per iteration.
    return count


def _expected_mixed(count: int) -> int:
    # "mixed" mode dispatches on i % 3: 0 -> normal (no injected anomaly),
    # 1 -> latency (1 injected anomaly), 2 -> errors (1 injected anomaly).
    return sum(1 for i in range(count) if i % 3 != 0)


SCENARIOS = [
    {"name": "normal", "mode": "normal", "count": 10, "delay": 0.2, "expected_fn": _expected_normal},
    {"name": "latency", "mode": "latency", "count": 8, "delay": 0.2, "expected_fn": _expected_latency},
    {"name": "errors", "mode": "errors", "count": 6, "delay": 0.1, "expected_fn": _expected_errors},
    {"name": "mixed", "mode": "mixed", "count": 15, "delay": 0.2, "expected_fn": _expected_mixed},
]


def run_traffic_simulator(mode: str, count: int, delay: float) -> None:
    cmd = [
        "docker", "compose", "-f", str(COMPOSE_FILE), "run", "--rm", "traffic-simulator",
        "--mode", mode, "--count", str(count), "--delay", str(delay),
    ]
    print(f"[Evaluation] Running: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def query_anomalies(start: datetime, end: datetime) -> list:
    sql = (
        "SELECT COALESCE(json_agg(row_to_json(t)), '[]') FROM ("
        "SELECT detection_method, detected_at FROM anomalies "
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


def evaluate_scenario(scenario: dict, grace_seconds: float) -> dict:
    name = scenario["name"]
    print(f"\n[Evaluation] === Scenario: {name} ===")

    start = datetime.now(timezone.utc)
    run_traffic_simulator(scenario["mode"], scenario["count"], scenario["delay"])
    print(f"[Evaluation] Waiting {grace_seconds}s grace period for LogProcessor to finish consuming...")
    subprocess.run(["sleep", str(grace_seconds)], check=True)
    end = datetime.now(timezone.utc)

    rows = query_anomalies(start, end)
    expected = scenario["expected_fn"](scenario["count"])

    by_method = {method: [] for method in DETECTION_METHODS}
    for row in rows:
        method = row["detection_method"]
        if method in by_method:
            by_method[method].append(row["detected_at"])

    method_results = {}
    for method in DETECTION_METHODS:
        timestamps = by_method[method]
        detected = len(timestamps)
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
        latencies = [
            (datetime.fromisoformat(ts.replace("Z", "+00:00")) - start).total_seconds()
            for ts in timestamps
        ]
        method_results[method] = {
            "detected": detected,
            "expected": expected,
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "min_latency_s": min(latencies) if latencies else None,
            "mean_latency_s": sum(latencies) / len(latencies) if latencies else None,
            "max_latency_s": max(latencies) if latencies else None,
        }

    return {
        "scenario": name,
        "mode": scenario["mode"],
        "count": scenario["count"],
        "start": start.isoformat(),
        "end": end.isoformat(),
        "expected": expected,
        "methods": method_results,
    }


def aggregate_overall(scenario_results: list) -> dict:
    overall = {}
    for method in DETECTION_METHODS:
        tp = sum(r["methods"][method]["tp"] for r in scenario_results)
        fp = sum(r["methods"][method]["fp"] for r in scenario_results)
        fn = sum(r["methods"][method]["fn"] for r in scenario_results)
        detected = sum(r["methods"][method]["detected"] for r in scenario_results)
        expected = sum(r["expected"] for r in scenario_results)
        precision = tp / (tp + fp) if (tp + fp) > 0 else None
        recall = tp / (tp + fn) if (tp + fn) > 0 else None
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision is not None and recall is not None and (precision + recall) > 0
            else None
        )
        overall[method] = {
            "detected": detected,
            "expected": expected,
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }
    return overall


def _fmt(value, digits=2):
    return f"{value:.{digits}f}" if value is not None else "n/a"


def render_markdown(scenario_results: list, overall: dict, run_started_at: datetime) -> str:
    lines = []
    lines.append("# Anomaly Detection Evaluation Report")
    lines.append("")
    lines.append(f"Run started (UTC): {run_started_at.isoformat()}")
    lines.append("")
    lines.append("## How to reproduce")
    lines.append("")
    lines.append("```")
    lines.append("docker compose -f docker/docker-compose.yml up -d")
    lines.append("python3 ml/evaluation/run_evaluation.py")
    lines.append("```")
    lines.append("")
    lines.append("## Per-scenario results")
    lines.append("")
    lines.append(
        "| Scenario | Method | Detected | Expected | TP | FP | FN | Precision | Recall | F1 | "
        "Mean time-to-detection (s) |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for result in scenario_results:
        for method in DETECTION_METHODS:
            m = result["methods"][method]
            lines.append(
                f"| {result['scenario']} | {method} | {m['detected']} | {m['expected']} | "
                f"{m['tp']} | {m['fp']} | {m['fn']} | {_fmt(m['precision'])} | {_fmt(m['recall'])} | "
                f"{_fmt(m['f1'])} | {_fmt(m['mean_latency_s'])} |"
            )
    lines.append("")
    lines.append("## Overall (aggregated across all scenarios)")
    lines.append("")
    lines.append("| Method | Detected | Expected | TP | FP | FN | Precision | Recall | F1 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for method in DETECTION_METHODS:
        o = overall[method]
        lines.append(
            f"| {method} | {o['detected']} | {o['expected']} | {o['tp']} | {o['fp']} | {o['fn']} | "
            f"{_fmt(o['precision'])} | {_fmt(o['recall'])} | {_fmt(o['f1'])} |"
        )
    lines.append("")
    lines.append("## Limitations")
    lines.append("")
    lines.append(
        "- **No per-event ground truth matching.** TrafficSimulator calls DemoApi over plain HTTP and "
        "never learns DemoApi's internally-generated `request_id`, so detected anomalies cannot be "
        "matched 1:1 against the specific request that was meant to trigger them. `expected` counts "
        "are derived by replicating TrafficSimulator's own deterministic per-iteration dispatch logic "
        "(documented in CLAUDE.md), not from a label TrafficSimulator recorded. TP/FP/FN are therefore "
        "**count-based approximations** (`TP = min(detected, expected)`, `FP = max(detected - expected, 0)`, "
        "`FN = max(expected - detected, 0)`), not exact instance-level matches."
    )
    lines.append(
        "- **\"Time-to-detection\" is not pipeline processing latency.** `anomalies.detected_at` is set "
        "from the *event's own timestamp* (as logged by DemoApi), not the wall-clock time LogProcessor "
        "inserted the row. This metric measures how far into the scenario's request stream the "
        "underlying event occurred, not RabbitMQ/LogProcessor queueing or processing delay."
    )
    lines.append(
        "- **Scenarios are not fully independent.** `dynamic_baseline` (and `hybrid`, which incorporates "
        "it) computes a rolling baseline from *all* prior `logs_agg` windows for an endpoint, including "
        "windows from earlier scenarios in the same run (and from any previous testing session). A "
        "latency spike in one scenario can raise the baseline enough to suppress detection of a similar "
        "spike in a later scenario. This is inherent to what a rolling baseline is meant to do, but it "
        "means scenario order and prior database history affect these results."
    )
    lines.append(
        "- **Small sample sizes.** Scenario counts are kept low (6-15 iterations) to keep the harness "
        "fast; precision/recall/F1 at this scale are illustrative, not statistically robust."
    )
    lines.append(
        "- **`normal` scenario has 0 expected anomalies.** Any detection during that window is entirely "
        "a false positive by definition, so precision for `normal` is always 0/undefined - this is "
        "intentional (it measures the false-positive rate under quiet traffic), not a bug."
    )
    lines.append("")
    return "\n".join(lines)


def render_csv(scenario_results: list) -> str:
    lines = [
        "scenario,method,detected,expected,tp,fp,fn,precision,recall,f1,"
        "min_latency_s,mean_latency_s,max_latency_s"
    ]
    for result in scenario_results:
        for method in DETECTION_METHODS:
            m = result["methods"][method]

            def fmt(v):
                return "" if v is None else f"{v:.4f}"

            lines.append(
                f"{result['scenario']},{method},{m['detected']},{m['expected']},{m['tp']},{m['fp']},"
                f"{m['fn']},{fmt(m['precision'])},{fmt(m['recall'])},{fmt(m['f1'])},"
                f"{fmt(m['min_latency_s'])},{fmt(m['mean_latency_s'])},{fmt(m['max_latency_s'])}"
            )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate anomaly detection methods using controlled TrafficSimulator scenarios.")
    parser.add_argument(
        "--scenarios", nargs="+", choices=[s["name"] for s in SCENARIOS], default=None,
        help="Subset of scenarios to run (default: all, in order normal/latency/errors/mixed).",
    )
    parser.add_argument("--grace-seconds", type=float, default=5.0, help="Seconds to wait after each scenario for LogProcessor to catch up.")
    args = parser.parse_args()

    selected = (
        [s for s in SCENARIOS if s["name"] in args.scenarios] if args.scenarios else SCENARIOS
    )

    run_started_at = datetime.now(timezone.utc)
    scenario_results = [evaluate_scenario(scenario, args.grace_seconds) for scenario in selected]
    overall = aggregate_overall(scenario_results)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = run_started_at.strftime("%Y%m%dT%H%M%SZ")
    md_path = RESULTS_DIR / f"evaluation_{timestamp}.md"
    csv_path = RESULTS_DIR / f"evaluation_{timestamp}.csv"

    md_path.write_text(render_markdown(scenario_results, overall, run_started_at))
    csv_path.write_text(render_csv(scenario_results))

    print("\n[Evaluation] === Overall summary ===")
    header = f"{'method':<18}{'detected':>10}{'expected':>10}{'tp':>6}{'fp':>6}{'fn':>6}{'precision':>11}{'recall':>9}{'f1':>7}"
    print(header)
    for method in DETECTION_METHODS:
        o = overall[method]
        print(
            f"{method:<18}{o['detected']:>10}{o['expected']:>10}{o['tp']:>6}{o['fp']:>6}{o['fn']:>6}"
            f"{_fmt(o['precision']):>11}{_fmt(o['recall']):>9}{_fmt(o['f1']):>7}"
        )

    print(f"\n[Evaluation] Markdown report: {md_path}")
    print(f"[Evaluation] CSV report:      {csv_path}")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print(f"[Evaluation] Command failed: {exc}", file=sys.stderr)
        sys.exit(1)
