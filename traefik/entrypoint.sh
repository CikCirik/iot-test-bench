#!/bin/sh
# Randeaza routes.yml din routes.tmpl.yml (inlocuieste __CLUSTER_DOMAIN__ cu
# valoarea reala din environment), apoi preda controlul entrypoint-ului
# oficial al imaginii Traefik - vezi comentariul din routes.tmpl.yml pentru
# motivul acestei indirectii (bug de escaping Coolify pe labels).
set -eu
sed "s|__CLUSTER_DOMAIN__|${CLUSTER_DOMAIN:-test-bench.iotstack}|g" \
  /etc/traefik-init/routes.tmpl.yml > /etc/traefik/dynamic/routes.yml
exec /entrypoint.sh "$@"
