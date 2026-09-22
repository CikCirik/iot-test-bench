# IoT Test Bench (Docker Compose, deployat prin Coolify)

Banc de test pentru serviciile IoT (Mosquitto, Node-RED, Zigbee2MQTT,
ChirpStack) în spatele unui reverse proxy Traefik propriu. Comutare
`INSTALL_*=true/false` prin profile Docker Compose, ca pe clusterul Swarm.

Repo: `https://github.com/CikCirik/iot-test-bench` (branch `main`).
Deployat momentan pe reComputer, `192.168.21.195`, prin Coolify.

## Arhitectură

Pe aceeași mașină coexistă **două lucruri separate, independente**:

1. **`iot-test-bench`** (acest repo) — servicii IoT + Traefik propriu,
   ascultă pe porturile **9080/9443**. Deployat prin Coolify (vezi mai jos),
   nu manual — Coolify clonează acest repo Git și rulează
   `docker-compose.yaml` de aici.
2. **Coolify** — PaaS self-hosted, instalat prin scriptul oficial
   (`curl -fsSL https://cdn.coollabs.io/coolify/install.sh | sudo bash`),
   pe porturile 80/443/8080 (implicit) + `:8000` direct. Găzduiește
   `iot-test-bench` ca "Application" (Git → Docker Compose) și orice alte
   resurse (ex. `floor-manager`).

Traefik-ul din `iot-test-bench` are o rută către Coolify
(`traefik/coolify.yml`), accesibilă și prin `coolify.<domeniu>:9443`, pe
lângă adresa directă `:8000`.

De ce porturi diferite (9080/9443, nu 8080/8443): portul implicit al
proxy-ului Coolify e 8080 — cu porturi separate, `iot-test-bench` nu mai
depinde deloc de ce face Coolify cu ale lui la reinstalări/update-uri.

## Structura

```
iot-test-bench/
├── docker-compose.yaml
├── .env                          # comutatoare INSTALL_*=true/false + credentiale (NU e in git)
├── .gitignore                    # exclude .env din git (secrete)
├── up.sh                         # fallback: traduce .env in --profile si porneste manual (fara Coolify)
├── setup-traefik-auth.sh         # genereaza Basic Auth pentru Traefik (o singura data per deployment)
├── nodered/
│   └── settings.js               # login (adminAuth) generat automat din NODERED_AUTH_USER/PASS
├── traefik/
│   ├── routes.tmpl.yml           # TEMPLATE rute catre serviciile IoT (vezi "De ce routes.tmpl.yml")
│   ├── entrypoint.sh             # randeaza routes.tmpl.yml -> routes.yml la fiecare pornire
│   ├── tls-options.yml           # forteaza HTTP/1.1 (fix WebSocket Node-RED, vezi comentariul din fisier)
│   └── coolify.yml               # ruta catre Coolify (instalare separata, port de host 8000)
└── chirpstack/
    ├── chirpstack.toml           # config principal (hostnames + parole deja completate)
    ├── region_eu868.toml         # plan de canale LoRaWAN pentru regiunea EU868
    └── postgres-init/
        └── 01-extensions.sql     # extensii Postgres necesare (ruleaza automat la prima pornire)
```

## De ce `routes.tmpl.yml` în loc de etichete Docker pe servicii

Traefik suportă rutare fie prin **etichete** pe fiecare container
(`traefik.http.routers.*` în `labels:`), fie printr-un **fișier static**
(provider `file`). Am ales varianta a doua pentru rutele către nodered,
zigbee2mqtt, chirpstack și dashboard-ul Traefik, dintr-un motiv concret,
descoperit prin depanare directă:

> **Coolify dublează `$` în orice `${VAR}` găsit în interiorul unui bloc
> `labels:`, la fiecare redeploy din Git** — indiferent dacă variabila are
> fallback (`${VAR:-default}`) sau nu. Rezultatul: `Host(\`nodered.${CLUSTER_DOMAIN}\`)`
> ajunge literal `$${CLUSTER_DOMAIN}` în containerul pornit, deci Traefik
> primește hostname-ul neinterpolat și rutele pică (404). Variabilele
> folosite în `command:`, `environment:` sau `ports:` NU sunt afectate —
> doar `labels:`.

Soluția: `CLUSTER_DOMAIN` e citit ca variabilă de mediu normală (safe,
necontaminat de bug), iar `traefik/entrypoint.sh` randează
`traefik/routes.tmpl.yml` → `/etc/traefik/dynamic/routes.yml` la fiecare
pornire a containerului, înlocuind `__CLUSTER_DOMAIN__` cu valoarea reală.
Beneficiu secundar: Traefik nu mai are nevoie de acces la `docker.sock`
(provider-ul `docker` a fost complet dezactivat).

**Implicație practică**: schimbi domeniul unui deployment DOAR din
`CLUSTER_DOMAIN` (Environment Variables în Coolify, sau `.env` local) —
niciodată nu mai editezi `docker-compose.yaml` sau `routes.tmpl.yml` pentru
asta. Exact mecanismul care face posibilă scalarea de mai jos.

## Scalare pe mai multe etaje / clădiri

Fiecare etaj sau clădire = un gateway fizic separat (reComputer sau
similar), cu propriile dispozitive Zigbee/LoRaWAN și propriul domeniu.
Structura recomandată în Coolify:

- **Project = clădire.** Un Project Coolify per clădire fizică.
- **Environment = etaj.** În interiorul Project-ului, câte un Environment
  per etaj — fiecare Environment redeployează **același** `docker-compose.yaml`
  din acest repo, dar cu propriile Environment Variables:
  ```
  CLUSTER_DOMAIN=etaj2.cladireA.iotstack
  ZIGBEE_HOST=192.168.x.x        # coordonatorul Zigbee al ACELUI etaj
  CHIRPSTACK_POSTGRES_PASS=...   # parole separate per site (recomandat)
  ```
- **Server = gateway fizic.** Fiecare reComputer/gateway se adaugă ca
  "Server" separat în Coolify (conexiune SSH), iar Application-ul
  (Environment-ul etajului respectiv) se deployează pe Server-ul lui.
  Un singur Coolify central poate administra toate site-urile din același
  dashboard, fără să fie nevoie de o instalare Coolify pe fiecare gateway.
- **Convenție de nume domeniu**: `<serviciu>.<etaj>.<cladire>.iotstack`
  (ex. `nodered.etaj2.cladireA.iotstack`) — configurabil liber, dar
  păstrează-l consistent între site-uri ca să nu confunzi loguri/dashboard-uri.

Pentru că `CLUSTER_DOMAIN` e o variabilă de mediu normală (nu mai e prinsă
de bug-ul de escaping din `labels:`), adăugarea unui site nou **nu necesită
nicio modificare de cod** — doar un nou Project/Environment în Coolify cu
valorile potrivite.

## Coolify — cum se administrează

**Deploy**: Coolify clonează acest repo (SSH, cheie de deploy dedicată) și
rulează `docker compose ... up -d` pe baza `docker-compose.yaml` de aici.
Fiecare serviciu IoT are `profiles: [...]` — Coolify NU pasează `--profile`,
deci activarea se face prin variabila de mediu `COMPOSE_PROFILES` (setată
în Environment Variables ale resursei):
```
COMPOSE_PROFILES=mosquitto,nodered,zigbee2mqtt,chirpstack
```

**Build Pack**: trebuie setat manual la "Docker Compose" (nu auto-detect)
în Configuration-ul resursei, altfel Coolify încearcă alt buildpack generic.

**Environment Variables necesare** (Coolify → resursă → Environment
Variables), aceleași chei ca în `.env` local:
```
INSTALL_MOSQUITTO=true
INSTALL_NODERED=true
INSTALL_ZIGBEE2MQTT=true
INSTALL_CHIRPSTACK=true
CHIRPSTACK_POSTGRES_USER=admin
CHIRPSTACK_POSTGRES_PASS=admin-chirpstack
MQTT_USER=admin
MQTT_PASS=admin-mqtt
TRAEFIK_AUTH_USER=admin
TRAEFIK_AUTH_PASS=<parola>
NODERED_AUTH_USER=admin
NODERED_AUTH_PASS=<parola>
QLEAP_BASE_URL=https://bmsn.quartzmatrix.ro:8085
QLEAP_USER=admin@client.ro
QLEAP_PASS=<parola>
CLUSTER_DOMAIN=test-bench.iotstack
TRAEFIK_HTTP_PORT=9080
TRAEFIK_HTTPS_PORT=9443
TZ=Europe/Bucharest
ZIGBEE_HOST=192.168.21.170
ZIGBEE_PORT=6638
ZIGBEE_ADAPTER=zstack
COMPOSE_PROFILES=mosquitto,nodered,zigbee2mqtt,chirpstack
```

**Basic Auth Traefik** (`admin-auth@file`, folosit de zigbee2mqtt și
dashboard-ul Traefik): NU e generat automat de Coolify — rulează o dată,
manual, echivalentul lui `setup-traefik-auth.sh` (vezi scriptul, citește
mountpoint-ul real din containerul traefik rulant). Fișierul rezultat
(`auth.yml`) trăiește într-un **volum Docker named** (`traefik-dynamic`),
deci **supraviețuiește redeploy-urilor normale** (fără `-v`) — nu trebuie
refăcut la fiecare deploy, doar dacă volumul e șters explicit.

**Empty-directory bug de reținut**: dacă un bind mount (ex.
`./nodered/settings.js:/data/settings.js:ro`) are sursa lipsă pe disc la
prima creare a containerului, Docker creează silent un DIRECTOR gol acolo
în loc să eșueze — containerul intră în crash loop. Fix: `sudo rmdir
<path-gol>` + repopulare din git (`git show HEAD:<fisier> > <path>`), atât
la sursă cât și, dacă a apucat să se propage, în volumul named (verifică
`docker volume inspect <volum> --format '{{.Mountpoint}}'`).

**Dacă portul Coolify (8080 implicit) intră în conflict**: se schimbă în
`/data/coolify/proxy/docker-compose.yml`, apoi `cd /data/coolify/proxy &&
sudo docker compose up -d`. Nu ar trebui să afecteze `iot-test-bench`
(separat, pe 9080/9443).

## Instalare / deploy de la zero

### Prin Coolify (recomandat, vezi și secțiunea de mai sus)
1. Coolify → New Resource → Docker Compose → conectează acest repo Git
   (SSH deploy key, `docker-compose.yaml` la rădăcină).
2. Build Pack = Docker Compose.
3. Environment Variables = lista de mai sus (cu `CLUSTER_DOMAIN` potrivit
   site-ului).
4. Deploy. Prima dată, rulează manual `setup-traefik-auth.sh`-ul echivalent
   (vezi mai sus) pentru Basic Auth.
5. Adaugă în `/etc/hosts` (mașina de pe care accesezi) câte un record per
   subdomeniu activat, cu IP-ul gateway-ului respectiv.

### Manual, fără Coolify (fallback, ex. depanare locală)
```bash
chmod +x up.sh setup-traefik-auth.sh
./up.sh
./setup-traefik-auth.sh
docker compose restart traefik
```

## Checklist — mod de lucru pentru schimbări viitoare

1. **Pull configul curent înainte să editezi orbește** — Coolify poate
   redeploya automat din Git (push → redeploy), deci starea de pe mașină
   se poate schimba fără intervenție manuală:
   ```bash
   ssh recomputer@192.168.21.195 "cat ~/iot-test-bench/docker-compose.yaml"
   ```
2. Editează local, verifică diff-ul.
3. Commit + push pe `main` — dacă auto-deploy e activ în Coolify, se aplică
   singur; altfel, apasă Deploy manual din UI.
4. **Nu presupune că un push a rezolvat problema** — verifică efectiv
   logurile Traefik (`docker logs <container-traefik>`) și codurile HTTP
   după fiecare deploy, atât local pe gateway cât și din rețea (`curl
   --resolve`), nu doar loopback.
5. Sincronizează copia locală din acest folder — checksum md5 identic cu
   ce e pe mașină:
   ```bash
   md5sum docker-compose.yaml .env
   ssh recomputer@192.168.21.195 "cd ~/iot-test-bench && md5sum docker-compose.yaml .env"
   ```

## Comenzi utile

```bash
docker compose ps                    # ce ruleaza (pe gateway, in directorul Coolify al resursei)
docker compose logs -f nodered       # log-uri live pentru un serviciu
docker compose down                  # opreste tot (pastreaza datele/volumele)
docker compose down -v               # opreste tot SI sterge volumele (date pierdute! sterge si auth.yml)
```

## Diferențe față de instalarea Swarm de pe cluster

- Fără noduri/placement — un gateway = o mașină.
- Fără n8n/py-runner (scoase din acest deployment).
- Node-RED folosește imaginea oficială `nodered/node-red` (fără paleta
  preinstalată: modbus, revpi-nodes, dashboard, knx-ultimate) — se
  instalează manual din Manage Palette.
- Certificat TLS self-signed — pentru Let's Encrypt (fără avertisment de
  browser), e posibil de adăugat cât timp gateway-ul e online și are un
  domeniu public valid.
