import unittest
from datetime import datetime, timezone

import dynamic_baseline

DETECTED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def windows(avg_latencies, error_rates):
    return [{"avg_latency": lat, "error_rate": err} for lat, err in zip(avg_latencies, error_rates)]


class TestEvaluateWindowBaseline(unittest.TestCase):
    def test_no_anomaly_with_insufficient_history(self):
        history = windows([100, 100, 100, 100], [0.0, 0.0, 0.0, 0.0])  # only 4, need >= 5
        result = dynamic_baseline.evaluate_window_baseline("/api/products", DETECTED_AT, 5000, 0.9, history)
        self.assertEqual(result, [])

    def test_no_anomaly_when_within_normal_range(self):
        history = windows([100, 100, 100, 100, 100], [0.0, 0.0, 0.0, 0.0, 0.0])
        result = dynamic_baseline.evaluate_window_baseline("/api/products", DETECTED_AT, 100, 0.0, history)
        self.assertEqual(result, [])

    def test_latency_anomaly_triggers_above_mean_plus_3std(self):
        # mean=100, pstdev=0, threshold=100 + 3*0=100 -> anything > 100 triggers
        history = windows([100, 100, 100, 100, 100], [0.0, 0.0, 0.0, 0.0, 0.0])
        result = dynamic_baseline.evaluate_window_baseline("/api/products", DETECTED_AT, 101, 0.0, history)
        types = {a["anomaly_type"] for a in result}
        self.assertIn("dynamic_baseline_latency", types)
        latency_anomaly = next(a for a in result if a["anomaly_type"] == "dynamic_baseline_latency")
        self.assertEqual(latency_anomaly["severity"], "medium")
        self.assertEqual(latency_anomaly["detection_method"], "dynamic_baseline")
        self.assertEqual(latency_anomaly["anomaly_score"], 101.0)
        self.assertEqual(latency_anomaly["score_unit"], "avg_latency_ms")

    def test_latency_anomaly_does_not_trigger_at_exact_threshold(self):
        # strictly-greater-than comparison: value == threshold must NOT trigger
        history = windows([100, 100, 100, 100, 100], [0.0, 0.0, 0.0, 0.0, 0.0])
        result = dynamic_baseline.evaluate_window_baseline("/api/products", DETECTED_AT, 100, 0.0, history)
        self.assertEqual([a for a in result if a["anomaly_type"] == "dynamic_baseline_latency"], [])

    def test_error_rate_anomaly_triggers_above_mean_plus_3std(self):
        history = windows([100, 100, 100, 100, 100], [0.0, 0.0, 0.0, 0.0, 0.0])
        result = dynamic_baseline.evaluate_window_baseline("/api/products", DETECTED_AT, 100, 0.1, history)
        error_anomaly = next(a for a in result if a["anomaly_type"] == "dynamic_baseline_error_rate")
        self.assertEqual(error_anomaly["severity"], "high")
        self.assertEqual(error_anomaly["detection_method"], "dynamic_baseline")
        self.assertEqual(error_anomaly["anomaly_score"], 0.1)
        self.assertEqual(error_anomaly["score_unit"], "error_rate")

    def test_both_signals_can_fire_together(self):
        history = windows([100, 100, 100, 100, 100], [0.0, 0.0, 0.0, 0.0, 0.0])
        result = dynamic_baseline.evaluate_window_baseline("/api/products", DETECTED_AT, 500, 0.5, history)
        types = {a["anomaly_type"] for a in result}
        self.assertEqual(types, {"dynamic_baseline_latency", "dynamic_baseline_error_rate"})

    def test_wider_historical_variance_raises_the_bar(self):
        # mean=100, values spread 50..150 give a large pstdev, so a much higher
        # threshold results - the same 101 that triggered in the tight-variance
        # test above should NOT trigger here.
        history = windows([50, 75, 100, 125, 150], [0.0, 0.0, 0.0, 0.0, 0.0])
        result = dynamic_baseline.evaluate_window_baseline("/api/products", DETECTED_AT, 101, 0.0, history)
        self.assertEqual([a for a in result if a["anomaly_type"] == "dynamic_baseline_latency"], [])


if __name__ == "__main__":
    unittest.main()
