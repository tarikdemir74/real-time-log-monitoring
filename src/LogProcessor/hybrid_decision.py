FAMILIES = ("rule_based", "isolation_forest", "dynamic_baseline")

TIER_SCORE = {"LOW": 1.0, "MEDIUM": 2.0, "HIGH": 3.0, "CRITICAL": 4.0}


def _max_severity(anomalies: list) -> str:
    severities = {a["severity"] for a in anomalies}
    return "high" if "high" in severities else "medium"


def evaluate(endpoint: str, detected_at, fired_anomalies: list) -> list:
    by_family = {family: [] for family in FAMILIES}
    for anomaly in fired_anomalies:
        family = anomaly.get("detection_method")
        if family in by_family:
            by_family[family].append(anomaly)

    fired = {family: _max_severity(anomalies) for family, anomalies in by_family.items() if anomalies}
    if not fired:
        return []

    num_fired = len(fired)
    rule_severity = fired.get("rule_based")

    if num_fired == 1:
        only_severity = next(iter(fired.values()))
        tier = "MEDIUM" if only_severity == "high" else "LOW"
    elif num_fired == 2:
        if rule_severity == "high":
            tier = "CRITICAL"
        elif "high" in fired.values():
            tier = "HIGH"
        else:
            tier = "MEDIUM"
    else:
        tier = "CRITICAL"

    contributing = ", ".join(f"{family} ({severity})" for family, severity in sorted(fired.items()))

    return [{
        "detected_at": detected_at,
        "endpoint": endpoint,
        "anomaly_type": "hybrid_anomaly",
        "severity": tier.lower(),
        "detection_method": "hybrid",
        "anomaly_score": TIER_SCORE[tier],
        "score_unit": "hybrid_tier_rank",
        "description": (
            f"Hybrid decision: {tier} confidence ({num_fired}/{len(FAMILIES)} methods fired) - {contributing}"
        ),
        "request_id": None,
    }]
