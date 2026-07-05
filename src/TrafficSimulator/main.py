import argparse
import os
import time

import requests

DEMO_API_URL = os.environ.get("DEMO_API_URL", "http://localhost:8080")

USERS = ["alice", "bob", "carol", "dave", "erin"]

VALID_PRODUCT_IDS = [1, 2, 3, 4, 5]
PRODUCT_PRICES = {1: 19.99, 2: 49.99, 3: 29.99, 4: 199.99, 5: 24.99}
INVALID_PRODUCT_IDS = [901, 902, 903]

LATENCY_HEADER = {"X-Simulate-Latency": "true"}


def _request(method: str, path: str, label: str, json=None, headers=None) -> None:
    url = f"{DEMO_API_URL}{path}"
    try:
        response = requests.request(method, url, json=json, headers=headers, timeout=10)
        print(f"[TrafficSimulator] {label} -> {response.status_code}")
    except requests.RequestException as exc:
        print(f"[TrafficSimulator] {label} -> request failed: {exc}")


def login(i: int, headers=None) -> None:
    username = USERS[i % len(USERS)]
    _request(
        "POST", "/api/login", f"POST /api/login (user={username})",
        json={"username": username, "password": "secret123"}, headers=headers,
    )


def login_invalid(i: int) -> None:
    username = USERS[i % len(USERS)]
    _request(
        "POST", "/api/login", f"POST /api/login (user={username}, missing password)",
        json={"username": username}, headers=None,
    )


def list_products(i: int, headers=None) -> None:
    _request("GET", "/api/products", "GET /api/products", headers=headers)


def add_to_cart(i: int, headers=None) -> None:
    username = USERS[i % len(USERS)]
    product_id = VALID_PRODUCT_IDS[i % len(VALID_PRODUCT_IDS)]
    quantity = (i % 3) + 1
    _request(
        "POST", "/api/cart/add", f"POST /api/cart/add (user={username}, product={product_id})",
        json={"userId": f"u-{username}", "productId": product_id, "quantity": quantity},
        headers=headers,
    )


def add_to_cart_invalid(i: int) -> None:
    username = USERS[i % len(USERS)]
    if i % 2 == 0:
        product_id = INVALID_PRODUCT_IDS[i % len(INVALID_PRODUCT_IDS)]
        _request(
            "POST", "/api/cart/add", f"POST /api/cart/add (user={username}, unknown product={product_id})",
            json={"userId": f"u-{username}", "productId": product_id, "quantity": 1},
        )
    else:
        product_id = VALID_PRODUCT_IDS[i % len(VALID_PRODUCT_IDS)]
        _request(
            "POST", "/api/cart/add", f"POST /api/cart/add (user={username}, invalid quantity=0)",
            json={"userId": f"u-{username}", "productId": product_id, "quantity": 0},
        )


def checkout(i: int, headers=None) -> None:
    username = USERS[i % len(USERS)]
    product_id = VALID_PRODUCT_IDS[i % len(VALID_PRODUCT_IDS)]
    quantity = (i % 3) + 1
    amount = round(PRODUCT_PRICES[product_id] * quantity, 2)
    _request(
        "POST", "/api/payment/checkout", f"POST /api/payment/checkout (user={username}, amount={amount})",
        json={"userId": f"u-{username}", "amount": amount}, headers=headers,
    )


def checkout_invalid(i: int) -> None:
    username = USERS[i % len(USERS)]
    _request(
        "POST", "/api/payment/checkout", f"POST /api/payment/checkout (user={username}, amount=0)",
        json={"userId": f"u-{username}", "amount": 0},
    )


def run_normal_journey(i: int, delay: float, latency_target: str = None) -> None:
    steps = [
        ("login", login),
        ("products", list_products),
        ("cart", add_to_cart),
        ("checkout", checkout),
    ]
    for name, step in steps:
        headers = LATENCY_HEADER if name == latency_target else None
        step(i, headers=headers)
        time.sleep(delay)


ERROR_STEPS = [login_invalid, add_to_cart_invalid, checkout_invalid]


def run_error_journey(i: int, delay: float, error_call_index: int) -> None:
    ERROR_STEPS[error_call_index % len(ERROR_STEPS)](i)
    time.sleep(delay)


def run_iteration(mode: str, i: int, delay: float, error_call_index: int) -> bool:
    """Returns True if an error journey was invoked (caller should advance error_call_index)."""
    if mode == "normal":
        run_normal_journey(i, delay)
        return False
    elif mode == "latency":
        targets = ["login", "products", "cart", "checkout"]
        run_normal_journey(i, delay, latency_target=targets[i % len(targets)])
        return False
    elif mode == "errors":
        run_error_journey(i, delay, error_call_index)
        return True
    elif mode == "mixed":
        sub_mode = i % 3
        if sub_mode == 0:
            run_normal_journey(i, delay)
            return False
        elif sub_mode == 1:
            targets = ["login", "products", "cart", "checkout"]
            run_normal_journey(i, delay, latency_target=targets[i % len(targets)])
            return False
        else:
            run_error_journey(i, delay, error_call_index)
            return True
    else:
        raise ValueError(f"Unknown mode: {mode}")


def main() -> None:
    parser = argparse.ArgumentParser(description="TrafficSimulator: sends HTTP traffic to DemoApi")
    parser.add_argument("--mode", choices=["normal", "latency", "errors", "mixed"], default="normal")
    parser.add_argument("--count", type=int, default=10, help="number of traffic iterations to run")
    parser.add_argument("--delay", type=float, default=0.5, help="seconds to sleep between individual requests")
    args = parser.parse_args()

    print(
        f"[TrafficSimulator] starting: mode={args.mode} count={args.count} delay={args.delay} "
        f"target={DEMO_API_URL}"
    )

    error_call_index = 0
    for i in range(args.count):
        print(f"[TrafficSimulator] --- iteration {i + 1}/{args.count} ---")
        invoked_error_journey = run_iteration(args.mode, i, args.delay, error_call_index)
        if invoked_error_journey:
            error_call_index += 1

    print("[TrafficSimulator] done.")


if __name__ == "__main__":
    main()
