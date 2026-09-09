# Simple image for the pilot_trader Dash dashboard.
# The app reads /home/fbazsa/pilot_trader/trades.json (absolute path), so the
# host project dir is bind-mounted at that same path at runtime; the COPY below
# only provides a fallback if the volume is absent.
FROM python:3.12-slim

WORKDIR /home/fbazsa/pilot_trader

COPY requirements-dashboard.txt ./
RUN pip install --no-cache-dir -r requirements-dashboard.txt

# Bind-mount overlays these at runtime, so the COPYs are a fallback only. The
# Dashboard imports only resolver and the shared account registry.
COPY dashboard.py resolver.py accounts.py ./

EXPOSE 8051

CMD ["python", "dashboard.py"]
