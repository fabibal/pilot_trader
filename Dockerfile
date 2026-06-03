# Simple image for the pilot_trader Dash dashboard.
# The app reads /home/fbazsa/pilot_trader/trades.json (absolute path), so the
# host project dir is bind-mounted at that same path at runtime; the COPY below
# only provides a fallback if the volume is absent.
FROM python:3.12-slim

WORKDIR /home/fbazsa/pilot_trader

# dash/plotly/pandas/yfinance: dashboard core. ib_insync+tzdata: live IBKR paper
# portfolio reads (tzdata is required — IB stamps fills US/Eastern, which
# zoneinfo can't resolve without it).
RUN pip install --no-cache-dir dash plotly pandas yfinance ib_insync tzdata

# Bind-mount overlays these at runtime, so the COPYs are a fallback only. The
# IBKR tab imports ibkr_connector -> order_manager -> reconcile, so include them.
COPY dashboard.py resolver.py ibkr_connector.py order_manager.py reconcile.py ./

EXPOSE 8051

CMD ["python", "dashboard.py"]
