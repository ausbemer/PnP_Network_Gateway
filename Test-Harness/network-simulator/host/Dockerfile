# Simulated LAN host for the autonet test segment.
# Tiny image: just enough to ping (incl. broadcast) and manipulate the neighbor
# table to keep ARP traffic flowing.
FROM alpine:3.20

RUN apk add --no-cache iputils iproute2

COPY entrypoint.sh /entrypoint.sh
# Strip any CRLF (the script may be edited on Windows) so the shebang works,
# then make it executable.
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
