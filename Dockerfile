# Simple image for the pilot_trader Dash dashboard.
# The app reads /home/fbazsa/pilot_trader/trades.json (absolute path), so the
# host project dir is bind-mounted at that same path at runtime; the COPY below
# only provides a fallback if the volume is absent.
FROM python:3.12-slim

WORKDIR /home/fbazsa/pilot_trader

RUN pip install --no-cache-dir dash plotly pandas yfinance

COPY dashboard.py ./

EXPOSE 8051

CMD ["python", "dashboard.py"]
