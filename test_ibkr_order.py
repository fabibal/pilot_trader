#!/usr/bin/env python3
"""One-shot paper-account test: buy 1 share of AAPL through ibkr_connector.

Sizes the notional to ~1 share at the live price and runs the real execute_order
path (order_id=None so the production data/orders.json ledger is untouched).
Prints the execution result, then the paper account value + portfolio.
"""
import json

import ibkr_connector as ibk

ib = ibk.connect()
try:
    print("market_open:", ibk.is_market_open())
    contract = ibk._stock(ib, "AAPL")
    px = ibk._price(ib, contract)
    print(f"AAPL price ~ ${px:.2f}")

    order = {"ticker": "AAPL", "action": "BUY", "quantity_usd": px,
             "signal_id": "TEST-AAPL", "order_id": None}
    res = ibk.execute_order(order, ib=ib)
    print("execute_order result:")
    print(json.dumps(res, indent=2, default=str))

    print("\naccount:", json.dumps(ibk.get_account_value(ib), indent=2))
    print("\nportfolio:", json.dumps(ibk.get_portfolio(ib), indent=2))
finally:
    ib.disconnect()
