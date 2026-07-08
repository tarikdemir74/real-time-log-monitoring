import unittest
from datetime import datetime, timezone

import anomaly_rules

DETECTED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


class TestEvaluateRequestRules(unittest.TestCase):
    def test_no_anomaly_for_normal_request(self):
        entry = {"responseTimeMs": 200, "statusCode": 200, "endpoint": "/api/products", "requestId": "r1"}
        self.assertEqual(anomaly_rules.evaluate_request_rules(entry, DETECTED_AT), [])

    def test_high_latency_triggers_at_threshold(self):
        entry = {"responseTimeMs": 1500, "statusCode": 200, "endpoint": "/api/products", "requestId": "r1"}
        anomalies = anomaly_rules.evaluate_request_rules(entry, DETECTED_AT)
        self.assertEqual(len(anomalies), 1)
        anomaly = anomalies[0]
        self.assertEqual(anomaly["anomaly_type"], "high_latency")
        self.assertEqual(anomaly["severity"], "medium")
        self.assertEqual(anomaly["detection_method"], "rule_based")
        self.assertEqual(anomaly["anomaly_score"], 1500.0)
        self.assertEqual(anomaly["score_unit"], "response_time_ms")
        self.assertEqual(anomaly["request_id"], "r1")

    def test_high_latency_does_not_trigger_below_threshold(self):
        entry = {"responseTimeMs": 1499, "statusCode": 200, "endpoint": "/api/products", "requestId": "r1"}
        self.assertEqual(anomaly_rules.evaluate_request_rules(entry, DETECTED_AT), [])

    def test_error_response_triggers_at_threshold(self):
        entry = {"responseTimeMs": 50, "statusCode": 500, "endpoint": "/api/payment/checkout", "requestId": "r2"}
        anomalies = anomaly_rules.evaluate_request_rules(entry, DETECTED_AT)
        self.assertEqual(len(anomalies), 1)
        anomaly = anomalies[0]
        self.assertEqual(anomaly["anomaly_type"], "error_response")
        self.assertEqual(anomaly["severity"], "high")
        self.assertEqual(anomaly["anomaly_score"], 500.0)
        self.assertEqual(anomaly["score_unit"], "status_code")

    def test_error_response_does_not_trigger_on_4xx(self):
        entry = {"responseTimeMs": 50, "statusCode": 404, "endpoint": "/api/products", "requestId": "r3"}
        self.assertEqual(anomaly_rules.evaluate_request_rules(entry, DETECTED_AT), [])

    def test_both_rules_can_fire_together(self):
        entry = {"responseTimeMs": 2000, "statusCode": 500, "endpoint": "/api/products", "requestId": "r4"}
        anomalies = anomaly_rules.evaluate_request_rules(entry, DETECTED_AT)
        types = {a["anomaly_type"] for a in anomalies}
        self.assertEqual(types, {"high_latency", "error_response"})

    def test_missing_fields_default_safely(self):
        entry = {"endpoint": "/api/products", "requestId": "r5"}
        self.assertEqual(anomaly_rules.evaluate_request_rules(entry, DETECTED_AT), [])


class TestEvaluateWindowRules(unittest.TestCase):
    def test_no_anomaly_below_error_rate_threshold(self):
        self.assertEqual(
            anomaly_rules.evaluate_window_rules("/api/products", DETECTED_AT, 0.49, 10), []
        )

    def test_no_anomaly_below_min_requests(self):
        self.assertEqual(
            anomaly_rules.evaluate_window_rules("/api/products", DETECTED_AT, 1.0, 2), []
        )

    def test_triggers_at_exact_thresholds(self):
        anomalies = anomaly_rules.evaluate_window_rules("/api/products", DETECTED_AT, 0.5, 3)
        self.assertEqual(len(anomalies), 1)
        anomaly = anomalies[0]
        self.assertEqual(anomaly["anomaly_type"], "high_error_rate_window")
        self.assertEqual(anomaly["severity"], "high")
        self.assertEqual(anomaly["detection_method"], "rule_based")
        self.assertEqual(anomaly["anomaly_score"], 0.5)
        self.assertEqual(anomaly["score_unit"], "error_rate")
        self.assertIsNone(anomaly["request_id"])

    def test_endpoint_is_propagated(self):
        anomalies = anomaly_rules.evaluate_window_rules("/api/login", DETECTED_AT, 1.0, 5)
        self.assertEqual(anomalies[0]["endpoint"], "/api/login")


if __name__ == "__main__":
    unittest.main()
