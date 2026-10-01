#!/bin/sh
# Randeaza routes.yml din routes.tmpl.yml (inlocuieste __CLUSTER_DOMAIN__ cu
# valoarea reala din environment), apoi preda controlul entrypoint-ului
# oficial al imaginii Traefik - vezi comentariul din routes.tmpl.yml pentru
# motivul acestei indirectii (bug de escaping Coolify pe labels).
set -eu
sed "s|__CLUSTER_DOMAIN__|${CLUSTER_DOMAIN:-test-bench.iotstack}|g" \
  /etc/traefik-init/routes.tmpl.yml > /etc/traefik/dynamic/routes.yml

# Genereaza automat auth.yml (middleware admin-auth@file, folosit de
# zigbee2mqtt/predict/knx-scanner/traefik-dashboard) din TRAEFIK_AUTH_USER/
# TRAEFIK_AUTH_PASS (.env) - la fiecare pornire, la fel cum Node-RED isi
# genereaza singur hash-ul bcrypt din NODERED_AUTH_USER/PASS (vezi
# nodered/settings.js). Inlocuieste fostul pas manual setup-traefik-auth.sh,
# care se uita usor la un deploy nou pe o masina noua.
#
# Imaginea traefik:v3.6 (Alpine) nu are openssl/htpasswd preinstalat - le
# instalam la pornire (apache2-utils, ~1-2s, din repo-ul Alpine). htpasswd
# produce bcrypt direct in formatul "user:hash" asteptat mai jos.
if [ -n "${TRAEFIK_AUTH_USER:-}" ] && [ -n "${TRAEFIK_AUTH_PASS:-}" ]; then
  apk add --no-cache apache2-utils >/dev/null 2>&1
  HASH_LINE="$(htpasswd -nbB "$TRAEFIK_AUTH_USER" "$TRAEFIK_AUTH_PASS")"
  cat > /etc/traefik/dynamic/auth.yml <<EOF
http:
  middlewares:
    admin-auth:
      basicAuth:
        users:
          - "$HASH_LINE"
EOF
fi

exec /entrypoint.sh "$@"
