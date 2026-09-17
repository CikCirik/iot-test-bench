#!/usr/bin/env bash
# Pas separat, o singura data: genereaza Basic Auth pentru admin-auth@file
# (folosit de zigbee2mqtt, pyrunner, traefik-dashboard) - Compose nu are
# echivalent pentru configure_traefik() din instalarea Swarm, deci se
# ruleaza manual. TLS ramane implicit (Traefik genereaza certificat propriu
# la fiecare pornire) - daca vrei Let's Encrypt, spune-mi si adaug ACME in
# docker-compose.yml (e online, deci e posibil, spre deosebire de Swarm).
set -euo pipefail
cd "$(dirname "$0")"
set -a; source .env; set +a

# Citim mountpoint-ul REAL direct din containerul traefik (nu presupunem
# numele volumului - Compose il prefixeaza cu numele proiectului, ex:
# "iot-test-bench_traefik-dynamic", care poate diferi in functie de numele
# folderului/COMPOSE_PROJECT_NAME).
CID="$(docker compose ps -q traefik 2>/dev/null)"
[ -n "$CID" ] || { echo "Ruleaza 'docker compose up -d traefik' o data, apoi reincearca."; exit 1; }
VOL_DIR="$(docker inspect "$CID" --format '{{range .Mounts}}{{if eq .Destination "/etc/traefik/dynamic"}}{{.Source}}{{end}}{{end}}')"
[ -n "$VOL_DIR" ] || { echo "Nu gasesc mount-ul /etc/traefik/dynamic pe containerul traefik."; exit 1; }

HASH=$(openssl passwd -apr1 "$TRAEFIK_AUTH_PASS")
sudo tee "$VOL_DIR/auth.yml" > /dev/null <<EOF
http:
  middlewares:
    admin-auth:
      basicAuth:
        users:
          - "$TRAEFIK_AUTH_USER:$HASH"
EOF
echo "auth.yml scris in $VOL_DIR"
