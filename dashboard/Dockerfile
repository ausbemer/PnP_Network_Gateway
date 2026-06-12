# Tailscale Gateway dashboard image.
# Small Flask app that reads network state and runs arp-scan. Runs with host
# networking and CAP_NET_RAW so arp-scan can probe the local subnet.
FROM python:3.12-slim

# iproute2 -> `ip`; arp-scan -> active device discovery; iputils-ping -> reachability.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        iproute2 arp-scan iputils-ping \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

EXPOSE 8088
CMD ["python3", "app.py"]
