import unittest
from datetime import datetime, timezone

import hybrid_decision

DETECTED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def fired(detection_method, severity):
    return {"detection_method": detection_method, "severity": severity}


class TestHybridDecision(unittest.TestCase):
    def test_no_families_fired_returns_empty(self):
        self.assertEqual(hybrid_decision.evaluate("/api/products", DETECTED_AT, []), [])

    def test_unrecognized_detection_method_is_ignored(self):
        # only FAMILIES entries count; an unknown method fired alone should not
        # produce a hybrid_anomaly at all
        result = hybrid_decision.evaluate("/api/products", DETECTED_AT, [fired("something_else", "high")])
        self.assertEqual(result, [])

    def test_one_medium_family_gives_low(self):
        result = hybrid_decision.evaluate("/api/products", DETECTED_AT, [fired("isolation_forest", "medium")])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["severity"], "low")
        self.assertEqual(result[0]["anomaly_score"], 1.0)

    def test_one_high_family_gives_medium(self):
        result = hybrid_decision.evaluate("/api/products", DETECTED_AT, [fired("dynamic_baseline", "high")])
        self.assertEqual(result[0]["severity"], "medium")
        self.assertEqual(result[0]["anomaly_score"], 2.0)

    def test_two_medium_families_give_medium(self):
        result = hybrid_decision.evaluate(
            "/api/products", DETECTED_AT,
            [fired("isolation_forest", "medium"), fired("dynamic_baseline", "medium")],
        )
        self.assertEqual(result[0]["severity"], "medium")
        self.assertEqual(result[0]["anomaly_score"], 2.0)

    def test_two_families_one_high_not_rule_based_gives_high(self):
        result = hybrid_decision.evaluate(
            "/api/products", DETECTED_AT,
            [fired("isolation_forest", "medium"), fired("dynamic_baseline", "high")],
        )
        self.assertEqual(result[0]["severity"], "high")
        self.assertEqual(result[0]["anomaly_score"], 3.0)

    def test_rule_based_high_plus_another_gives_critical(self):
        result = hybrid_decision.evaluate(
            "/api/products", DETECTED_AT,
            [fired("rule_based", "high"), fired("isolation_forest", "medium")],
        )
        self.assertEqual(result[0]["severity"], "critical")
        self.assertEqual(result[0]["anomaly_score"], 4.0)

    def test_three_families_gives_critical_even_without_any_high(self):
        result = hybrid_decision.evaluate(
            "/api/products", DETECTED_AT,
            [fired("rule_based", "medium"), fired("isolation_forest", "medium"), fired("dynamic_baseline", "medium")],
        )
        self.assertEqual(result[0]["severity"], "critical")
        self.assertEqual(result[0]["anomaly_score"], 4.0)

    def test_multiple_anomalies_same_family_use_max_severity(self):
        result = hybrid_decision.evaluate(
            "/api/products", DETECTED_AT,
            [fired("isolation_forest", "medium"), fired("isolation_forest", "medium")],
        )
        # only one family present -> LOW, not counted twice as two families
        self.assertEqual(result[0]["severity"], "low")

    def test_output_shape(self):
        result = hybrid_decision.evaluate("/api/login", DETECTED_AT, [fired("rule_based", "high")])
        anomaly = result[0]
        self.assertEqual(anomaly["endpoint"], "/api/login")
        self.assertEqual(anomaly["anomaly_type"], "hybrid_anomaly")
        self.assertEqual(anomaly["detection_method"], "hybrid")
        self.assertEqual(anomaly["score_unit"], "hybrid_tier_rank")
        self.assertIsNone(anomaly["request_id"])
        self.assertIn("rule_based", anomaly["description"])


if __name__ == "__main__":
    unittest.main()
