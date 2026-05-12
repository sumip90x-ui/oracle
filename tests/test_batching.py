#!/usr/bin/env python3
"""Unit tests for make_batches() — verifies every input stock appears in exactly one batch."""

import sys, os
sys.path.insert(0, os.path.expanduser("~/ORACLE/engine"))
from oracle_think_tank import make_batches


def test_make_batches(stock_count: int, batch_size: int = 2) -> bool:
    stocks = [f"SYM{i}" for i in range(stock_count)]
    batches = make_batches(stocks, size=batch_size)

    all_in_batches = [s for batch in batches for s in batch]

    # Every input stock appears at least once
    for s in stocks:
        if s not in all_in_batches:
            print(f"  FAIL [{stock_count} stocks]: {s} missing from all batches")
            return False

    # Every stock appears exactly once (no duplicates)
    if len(all_in_batches) != len(stocks):
        print(f"  FAIL [{stock_count} stocks]: expected {len(stocks)} total, got {len(all_in_batches)}")
        return False

    # No empty batches
    for i, b in enumerate(batches):
        if not b:
            print(f"  FAIL [{stock_count} stocks]: batch {i} is empty")
            return False

    # Each batch is at most batch_size
    for i, b in enumerate(batches):
        if len(b) > batch_size:
            print(f"  FAIL [{stock_count} stocks]: batch {i} has {len(b)} stocks (max {batch_size})")
            return False

    print(f"  PASS [{stock_count} stocks]: {len(batches)} batch(es) — {batches}")
    return True


def main():
    print("\n=== make_batches() unit tests ===\n")
    results = {}
    for n in (3, 4, 5, 6, 7):
        results[n] = test_make_batches(n)

    print()
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    print(f"Results: {passed}/{total} passed")

    if passed < total:
        failed = [n for n, v in results.items() if not v]
        print(f"FAILED on input sizes: {failed}")
        sys.exit(1)
    else:
        print("All tests passed.")


if __name__ == "__main__":
    main()
