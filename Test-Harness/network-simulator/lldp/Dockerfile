# Optional LLDP advertiser, so the segment also exercises autonet's LLDP path.
# Runs with host networking (see docker-compose.yml) and advertises on the
# host's physical interfaces.
FROM alpine:3.20

RUN apk add --no-cache lldpd

# -d: run in foreground so the container stays alive and logs to stdout.
CMD ["lldpd", "-d"]
