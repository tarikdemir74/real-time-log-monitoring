import unittest

import ml_features


class TestBuildFeatureVector(unittest.TestCase):
    def test_vector_length_matches_feature_names(self):
        vector = ml_features.build_feature_vector(200, 100, "/api/products", 10, 0.1)
        self.assertEqual(len(vector), len(ml_features.FEATURE_NAMES))

    def test_scalar_fields_in_expected_positions(self):
        vector = ml_features.build_feature_vector(
            status_code=404, response_time_ms=250, endpoint="/api/login",
            window_request_count=42, window_error_rate=0.25,
        )
        self.assertEqual(vector[0], 250.0)  # response_time_ms
        self.assertEqual(vector[1], 404.0)  # status_code
        self.assertEqual(vector[-2], 42.0)  # window_request_count
        self.assertEqual(vector[-1], 0.25)  # window_error_rate

    def test_one_hot_encoding_known_endpoint(self):
        vector = ml_features.build_feature_vector(200, 100, "/api/login", 10, 0.0)
        one_hot = vector[2:2 + len(ml_features.KNOWN_ENDPOINTS)]
        expected = [1.0 if e == "/api/login" else 0.0 for e in ml_features.KNOWN_ENDPOINTS]
        self.assertEqual(one_hot, expected)
        self.assertEqual(sum(one_hot), 1.0)

    def test_one_hot_encoding_each_known_endpoint_individually(self):
        for endpoint in ml_features.KNOWN_ENDPOINTS:
            vector = ml_features.build_feature_vector(200, 100, endpoint, 10, 0.0)
            one_hot = vector[2:2 + len(ml_features.KNOWN_ENDPOINTS)]
            self.assertEqual(sum(one_hot), 1.0, f"endpoint={endpoint}")

    def test_unknown_endpoint_gives_all_zero_one_hot(self):
        vector = ml_features.build_feature_vector(200, 100, "/health", 10, 0.0)
        one_hot = vector[2:2 + len(ml_features.KNOWN_ENDPOINTS)]
        self.assertEqual(sum(one_hot), 0.0)

    def test_all_values_are_floats(self):
        vector = ml_features.build_feature_vector(200, 100, "/api/products", 10, 0.1)
        self.assertTrue(all(isinstance(v, float) for v in vector))

    def test_feature_names_length_matches_definition(self):
        self.assertEqual(
            len(ml_features.FEATURE_NAMES),
            2 + len(ml_features.KNOWN_ENDPOINTS) + 2,
        )


if __name__ == "__main__":
    unittest.main()
