#!/usr/bin/env bash
# Citeste INSTALL_*=true/false din .env si porneste doar profilele active.
# Docker Compose nu stie sa citeasca "true/false" direct - profiles se dau
# explicit la pornire, deci construim lista aici.
set -euo pipefail
cd "$(dirname "$0")"

# shellcheck disable=SC1091
set -a; source .env; set +a

profiles=()
$INSTALL_MOSQUITTO   && profiles+=(--profile mosquitto)
$INSTALL_NODERED     && profiles+=(--profile nodered)
$INSTALL_ZIGBEE2MQTT && profiles+=(--profile zigbee2mqtt)
$INSTALL_CHIRPSTACK  && profiles+=(--profile chirpstack)

echo "Pornesc: ${profiles[*]:-(niciun serviciu IoT, doar Traefik)}"
docker compose "${profiles[@]}" up -d
