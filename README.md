# IoT Test Bench (Docker Compose, fara Swarm)

Banc de test single-node pentru serviciile IoT (Mosquitto, Node-RED,
Zigbee2MQTT, n8n, Py-Runner, ChirpStack) in spatele unui reverse proxy
Traefik. Aceeasi logica de comutare `INSTALL_*=true/false` ca pe clusterul
Swarm, adaptata la Docker Compose.

## Cerinte

- Un Linux (Debian/Ubuntu/Raspberry Pi OS) cu conexiune la internet.
- Docker + plugin-ul Compose (`docker compose`, nu vechiul `docker-compose`).

Instalare Docker, daca nu exista deja:
```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
# delogheaza-te si reloagheaza-te (sau `newgrp docker`) ca sa intre in efect grupul
```

Verifica ca merge Compose:
```bash
docker compose version
```

## Fisiere

| Fisier | Rol |
|---|---|
| `docker-compose.yml` | Definitia tuturor serviciilor. Fiecare serviciu IoT are `profiles: [...]` - se porneste doar daca ii activezi profilul. Traefik nu are profil, porneste mereu. |
| `.env` | Comutatoare `INSTALL_*=true/false` + credentiale (user/parola pentru Postgres, MQTT, Traefik Basic Auth). **Schimba parolele implicite inainte de a expune spre internet.** |
| `up.sh` | Citeste `.env`, traduce `true/false` in lista corecta de `--profile` si porneste (`docker compose ... up -d`). |
| `setup-traefik-auth.sh` | Pas separat, o singura data: genereaza middleware-ul `admin-auth@file` (Basic Auth) folosit de zigbee2mqtt, py-runner si dashboard-ul Traefik. |

## Instalare de la zero

1. Copiaza acest folder (`iot-test-bench/`) pe masina Linux tinta.
2. Editeaza `.env`:
   - pune `true`/`false` la fiecare `INSTALL_*` dupa ce servicii vrei active
   - schimba parolele (`POSTGRES_PASS`, `CHIRPSTACK_POSTGRES_PASS`, `MQTT_PASS`, `TRAEFIK_AUTH_PASS`)
   - pune `CLUSTER_DOMAIN` la domeniul tau real (sau lasa-l cum e, pentru test local)
3. Fa scripturile executabile si porneste:
   ```bash
   chmod +x up.sh setup-traefik-auth.sh
   ./up.sh
   ```
4. Dupa primul `up.sh` (Traefik trebuie sa fie deja pornit o data ca sa existe volumul), genereaza autentificarea:
   ```bash
   ./setup-traefik-auth.sh
   ```
5. Adauga in DNS-ul local (sau in `/etc/hosts` pe masina de pe care accesezi) un record catre IP-ul masinii, pentru fiecare subdomeniu activat, ex:
   ```
   192.168.x.x   nodered.test-bench.iotstack
   192.168.x.x   n8n.test-bench.iotstack
   192.168.x.x   traefik.test-bench.iotstack
   ```
6. Acceseaza `https://<serviciu>.<CLUSTER_DOMAIN>:<TRAEFIK_HTTPS_PORT>/` (implicit port `8443`).
   Certificatul e auto-generat de Traefik (self-signed) - browserul va da avertisment
   de securitate la prima accesare, e normal, accepti manual o data.

## Comenzi utile

```bash
docker compose ps                    # ce ruleaza
docker compose logs -f nodered       # log-uri live pentru un serviciu
docker compose down                  # opreste tot (pastreaza datele/volumele)
docker compose down -v               # opreste tot SI sterge volumele (date pierdute!)
./up.sh                              # reporneste cu profilele din .env curent
```

Ca sa schimbi ce servicii sunt active: editezi `.env`, apoi rulezi din nou
`./up.sh` - serviciile scoase din `.env` (puse pe `false`) NU se opresc automat
singure, trebuie oprite explicit:
```bash
docker compose stop <nume-serviciu>
```

## Diferente fata de instalarea Swarm de pe cluster

- Fara noduri/placement - totul ruleaza pe o singura masina.
- Fara Swarmpit (dashboard de management Swarm) - nu are sens fara Swarm.
  Pentru monitorizare simpla, `docker compose ps` / `docker stats` sau
  adauga manual un `dozzle`/`cadvisor` daca ai nevoie.
- Fara reteaua macvlan pentru KNX (era specifica problemei de multicast pe
  Swarm) - daca ai nevoie de KNX/IP, spune-mi si vedem ce trebuie adaugat
  pentru single-node.
- Node-RED foloseste imaginea oficiala `nodered/node-red` (fara paleta
  preinstalata: modbus, revpi-nodes, dashboard, knx-ultimate). Se instaleaza
  manual din Manage Palette, in interfata web, sau imi spui si construiesc
  o imagine custom si pentru asta.
- Certificat TLS self-signed (generat automat de Traefik) - daca vrei
  Let's Encrypt (fara avertisment de browser), spune-mi, e usor de adaugat
  cat timp masina e online si are un domeniu public valid.
