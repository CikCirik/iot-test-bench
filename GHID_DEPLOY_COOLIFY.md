# Ghid complet: Coolify + deploy pentru `iot-test-bench` și `agent-ML-realdata`

Document de referință, pas cu pas, pentru tot ce s-a făcut ca să ajungem la
starea actuală: Coolify instalat pe reComputer (`172.16.1.120`), cu **două**
resurse deployate din **două repo-uri Git separate**:

1. `iot-test-bench` (`github.com/CikCirik/iot-test-bench`) — stack IoT complet
   (Mosquitto, Node-RED, Zigbee2MQTT, ChirpStack, Traefik), prin Docker Compose.
2. `agent-ML-realdata` (`github.com/StefanVlad0/agent-ML-realdata`, branch
   `production`) — serviciul de ML (`/predict_command`), doar partea de
   **serving**, prin Dockerfile simplu, rutat prin Traefik-ul de la punctul 1.

Nu conține parole/chei reale — acolo unde apare un secret, documentul spune
**unde** îl găsești (fișier `.env` local sau Environment Variables din
Coolify), nu îl reproduce. Motivul: acest fișier poate ajunge să fie
copiat/trimis/arhivat, iar secretele nu trebuie să existe în mai mult de un
singur loc controlat.

---

## 0. Arhitectura de ansamblu — ca să nu se piardă contextul

```
reComputer (172.16.1.120)
│
├── Coolify (instalare oficială, PaaS) ── dashboard pe :8000, proxy propriu pe :80/:443/:8080
│     │
│     ├── Resursa "iot-test-bench"  (Docker Compose, din repo CikCirik/iot-test-bench)
│     │     └── mosquitto, nodered, zigbee2mqtt, chirpstack(+postgres+redis+gwbridge), traefik
│     │           Traefik-ul ĂSTA ascultă pe :9080/:9443 (NU pe porturile Coolify)
│     │
│     └── Resursa "agent-ML-realdata" (Dockerfile, din repo StefanVlad0/agent-ML-realdata)
│           └── un singur container, serviciul de predict, publicat pe host :8002
│
└── Traefik-ul din iot-test-bench rutează TOATE domeniile (inclusiv predict.*
      și coolify.*) prin fișiere randate dintr-un TEMPLATE, nu prin etichete
      Docker pe fiecare container — motivul e explicat detaliat la §3.7.
```

**De ce două resurse Coolify separate și nu totul într-un singur
`docker-compose.yaml`?** Pentru că `agent-ML-realdata` e alt repo, alt
proprietar de cont GitHub, alt ciclu de dezvoltare (antrenare model,
independent de stack-ul IoT). Amestecarea lor într-un singur compose ar
însemna că orice modificare la unul forțează un redeploy și la celălalt, și
ar necesita acces de scriere pe repo-ul altcuiva — nedorit explicit.

---

## Partea 1 — Instalare Coolify (dacă pornești de la o mașină goală)

Dacă Coolify e deja instalat (cazul de acum, pe reComputer), sari la Partea 2.

```bash
curl -fsSL https://cdn.coollabs.io/coolify/install.sh | sudo bash
```

Scriptul face, în ordine: verifică pachetele necesare, verifică SSH, verifică
Docker (îl instalează dacă lipsește), configurează daemon-ul Docker, descarcă
fișierele de configurare proprii, generează `.env`-ul intern (parole DB etc.),
generează o cheie SSH pentru conexiunea Coolify → localhost, apoi
instalează+pornește totul.

La final afișează adresa dashboard-ului, de obicei `http://<IP>:8000`. Prima
accesare = creezi contul de admin.

**Capcană de reținut** (ni s-a întâmplat în acest proiect): dacă vreodată
trebuie să **reinstalezi complet** Coolify (nu doar update), rularea simplă a
scriptului **nu resetează garantat tot** — volumele Docker `coolify-db` și
`coolify-redis` nu sunt sub `/data/coolify`, deci supraviețuiesc unui
`rm -rf /data/coolify` și pot păstra o parolă de DB veche, ceea ce produce o
eroare de autentificare Postgres la pornire. Curățare completă corectă:

```bash
sudo docker stop coolify coolify-db coolify-redis coolify-realtime coolify-proxy coolify-sentinel
sudo docker rm   coolify coolify-db coolify-redis coolify-realtime coolify-proxy coolify-sentinel
sudo docker volume rm coolify-db coolify-redis
sudo rm -rf /data/coolify
# apoi rulezi din nou curl ... | sudo bash
```

---

## Partea 2 — Deploy `iot-test-bench` (Docker Compose)

### 2.1 Pregătirea repo-ului Git + cheie SSH dedicată

Fiecare repo pe care Coolify trebuie să-l cloneze are nevoie de o **cheie SSH
dedicată** (nu se refolosește cheia personală a nimănui) — practică de
securitate: dacă acea cheie e vreodată compromisă, are acces DOAR la acel
repo, nu la tot GitHub-ul cuiva.

Pe reComputer (sau orice mașină care are deja acces de scriere pe repo):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/github_iot_test_bench -N '' -C 'iot-test-bench@recomputer'
cat ~/.ssh/github_iot_test_bench.pub    # cheia PUBLICĂ
```

Pe GitHub → repo `iot-test-bench` → **Settings → Deploy keys → Add deploy
key** → lipești cheia publică → **bifezi "Allow write access"** (aici avem
nevoie de scriere, pentru că tot din reComputer facem `git push` când
modificăm ceva).

```bash
git remote set-url origin git@github.com:CikCirik/iot-test-bench.git
ssh -T git@github.com    # test: trebuie sa spuna "successfully authenticated"
```

### 2.2 Adăugarea cheii private în Coolify

Coolify → **Keys & Tokens → Private Keys → Add Private Key** → lipești
conținutul complet al `~/.ssh/github_iot_test_bench` (inclusiv liniile
`-----BEGIN/END OPENSSH PRIVATE KEY-----`) → dai un nume ușor de recunoscut,
ex. `iot-test-bench-key`.

**Explicație de context, ca să nu se confunde cu §3.2 mai jos**: cheia asta
o folosește Coolify ca să clonezi TU codul din GitHub în Coolify — e complet
separată de orice cheie SSH ai folosi tu personal ca să te loghezi PE
reComputer.

### 2.3 Creare resursă în Coolify

**Projects → (proiect nou sau existent) → New Resource → Docker Compose**

- **Repository**: `git@github.com:CikCirik/iot-test-bench.git`
- **Private Key**: `iot-test-bench-key` (cea de la §2.2)
- **Branch**: `main`
- **Build Pack**: **Docker Compose** — setează-l manual, dropdown-ul din
  Configuration. **Capcană întâlnită**: dacă îl lași pe auto-detect, Coolify
  poate alege alt buildpack generic (`railpack`), care ignoră complet
  `docker-compose.yaml` și pornește un singur container greșit.
- **Docker Compose Location**: `/docker-compose.yaml` — atenție la extensie,
  Coolify caută implicit `.yaml`, nu `.yml`. Dacă fișierul din repo se numește
  `docker-compose.yml`, redenumește-l (`git mv docker-compose.yml
  docker-compose.yaml`) sau schimbă manual această valoare în Coolify.
- **Server**: mașina țintă (reComputer, sau alt Server adăugat anterior în
  Coolify prin SSH).

### 2.4 Environment Variables

Resursă → **Environment Variables** → adaugi, câte una:

```
INSTALL_MOSQUITTO=true
INSTALL_NODERED=true
INSTALL_ZIGBEE2MQTT=true
INSTALL_CHIRPSTACK=true
CHIRPSTACK_POSTGRES_USER=<vezi .env local>
CHIRPSTACK_POSTGRES_PASS=<vezi .env local>
MQTT_USER=<vezi .env local>
MQTT_PASS=<vezi .env local>
TRAEFIK_AUTH_USER=<vezi .env local>
TRAEFIK_AUTH_PASS=<vezi .env local>
NODERED_AUTH_USER=<vezi .env local>
NODERED_AUTH_PASS=<vezi .env local>
QLEAP_BASE_URL=<vezi .env local>
QLEAP_USER=<vezi .env local>
QLEAP_PASS=<vezi .env local>
CLUSTER_DOMAIN=test-bench.iotstack
TRAEFIK_HTTP_PORT=9080
TRAEFIK_HTTPS_PORT=9443
TZ=Europe/Bucharest
ZIGBEE_HOST=<IP coordonator Zigbee>
ZIGBEE_PORT=6638
ZIGBEE_ADAPTER=zstack
COMPOSE_PROFILES=mosquitto,nodered,zigbee2mqtt,chirpstack
```

**De ce `COMPOSE_PROFILES` separat, deși ai deja `INSTALL_*=true`?** Fiecare
serviciu din `docker-compose.yaml` are `profiles: ["nume"]`. Comanda pe care
Coolify o rulează efectiv e un simplu `docker compose up -d`, **fără**
`--profile` (am verificat direct în log-urile de deploy). Docker Compose
ignoră orice serviciu cu profil neactivat dacă nu-i spui explicit ce profile
vrei — iar `COMPOSE_PROFILES` (variabilă de mediu, listă separată prin
virgulă, FĂRĂ spații) e mecanismul prin care Compose CLI activează profile
fără flag-uri explicite pe linia de comandă. Fără ea, la deploy pornește
**doar Traefik** (singurul serviciu fără profil) și nimic altceva — exact ce
am pățit prima dată.

**De ce porturi 9080/9443 și nu 8080/8443?** Portul implicit al proxy-ului
Coolify e 8080. Cu porturi complet separate, Traefik-ul din `iot-test-bench`
nu mai depinde deloc de ce face Coolify cu porturile lui la orice
reinstalare/update viitor.

### 2.5 Primul deploy — ce poate merge greșit

Apeși **Deploy**. Probleme întâlnite concret în acest proiect, în ordinea în
care apar de obicei:

**a) `ssh: Could not resolve hostname github.com`** — eroare de DNS
tranzitorie, la clonarea repo-ului. Nu ține de configurația noastră — verifică
cu (de pe reComputer):
```bash
sudo docker run --rm --network coolify alpine getent hosts github.com
```
Dacă asta merge dar deploy-ul tot a picat, e pur și simplu un blip de rețea
în acel moment exact — **reîncearcă Deploy**, de obicei merge din a doua
încercare. Dacă se repetă des, cauza probabilă e resolver-ul routerului local
(`/etc/resolv.conf` → `nameserver <IP router>`) care are momente de
lag — soluția structurală e să adaugi DNS-uri publice explicite
(`1.1.1.1`, `8.8.8.8`) în `/etc/docker/daemon.json` al gazdei și repornești
Docker, ca toate containerele să nu mai depindă de routerul local pentru
rezoluție externă.

**b) Doar Traefik pornește, restul serviciilor lipsesc** — vezi §2.4,
`COMPOSE_PROFILES` lipsă sau greșit scris.

**c) `Bind for 0.0.0.0:9080 failed: port is already allocated`** — ceva
ascultă deja pe acel port (de obicei o versiune anterioară, pornită manual cu
`docker compose up` direct, în paralel cu cea gestionată de Coolify). Verifici
cu `sudo docker ps --format '{{.Names}}\t{{.Ports}}'` și oprești ce nu
trebuie.

**d) Un serviciu intră în crash loop, cu eroare de genul `Is a directory` sau
`Cannot find module '/data/settings.js'`** — bug cunoscut al Docker: dacă
sursa unui bind mount (ex. `./nodered/settings.js:/data/settings.js:ro`) nu
există fizic pe disc în momentul creării containerului, Docker creează
silențios un **director gol** acolo în loc să dea eroare — iar containerul nu
mai pornește niciodată corect cu acel "fișier" care e de fapt un folder gol.
Se verifică și repară așa (pe host, în directorul persistent al resursei
Coolify, `/data/coolify/applications/<uuid-resursa>/`):
```bash
sudo rmdir <cale-catre-directorul-gol>
sudo sh -c "git -C ~/iot-test-bench show HEAD:<fisier> > <cale-corecta>"
sudo docker compose --env-file .env --profile ... up -d --force-recreate <serviciu>
```
**Atenție**: bug-ul poate apărea și **în interiorul unui volum Docker named**
(nu doar la calea externă) — dacă eroarea persistă după fix-ul de mai sus,
verifică și acolo:
```bash
docker volume inspect <nume-volum> --format '{{.Mountpoint}}'
sudo rmdir <mountpoint>/<fisierul-problema>
```

### 2.6 Basic Auth pentru Traefik — pas manual, o singură dată

Middleware-ul `admin-auth@file` (folosit de `zigbee2mqtt` și de dashboard-ul
Traefik) **nu se generează automat** la deploy. Se rulează o singură dată,
manual, echivalentul lui `setup-traefik-auth.sh` din repo — citește
mountpoint-ul REAL al directorului `/etc/traefik/dynamic` direct din
containerul Traefik rulant (nu presupune un nume fix de volum, pentru că
Compose îl prefixează cu numele proiectului, care diferă între deploy manual
și deploy Coolify):
```bash
CID=$(sudo docker ps -qf name=<nume-container-traefik>)
VOL_DIR=$(sudo docker inspect "$CID" --format '{{range .Mounts}}{{if eq .Destination "/etc/traefik/dynamic"}}{{.Source}}{{end}}{{end}}')
HASH=$(openssl passwd -apr1 "<TRAEFIK_AUTH_PASS>")
sudo tee "$VOL_DIR/auth.yml" > /dev/null <<EOF
http:
  middlewares:
    admin-auth:
      basicAuth:
        users:
          - "<TRAEFIK_AUTH_USER>:$HASH"
EOF
```
**De ce doar o dată și nu la fiecare deploy?** Fișierul ăsta trăiește
într-un volum Docker **named** (`traefik-dynamic`), nu în codul din git — un
redeploy normal (fără `-v`) NU șterge volumele, deci `auth.yml` supraviețuiește
la orice redeploy viitor automat. Dispare doar dacă cineva rulează explicit
`docker compose down -v` sau șterge volumul.

### 2.7 De ce rutele Traefik nu sunt etichete Docker pe fiecare serviciu

Cea mai importantă lecție tehnică din tot acest proiect, merită explicată pe
larg pentru că a produs cele mai multe ore de depanare:

Traefik poate afla rutele fie din **etichete** puse direct pe fiecare
container (`labels: - traefik.http.routers.X.rule=...`), fie dintr-un
**fișier static** citit de "provider-ul file". Am pornit cu varianta cu
etichete (mai simplă, mai comună), și a funcționat perfect **cât timp am
scris fișierele manual direct pe disc**. Problema a apărut la fiecare
**redeploy automat din Git prin Coolify**:

> Coolify pre-procesează `docker-compose.yaml`-ul înainte să-l predea lui
> Docker Compose, și **dublează caracterul `$`** în orice `${VARIABILA}`
> găsit în interiorul unui bloc `labels:` — indiferent dacă variabila are o
> valoare implicită (`${VAR:-implicit}`) sau nu. Rezultatul: în container
> ajunge literalmente `$${CLUSTER_DOMAIN}`, Docker Compose NU mai
> interpolează asta (`$$` e sintaxa lui Compose pentru "un `$` literal"), iar
> Traefik primește un hostname literal neexistent, deci ruta pică (404).
> Variabilele folosite în `command:`, `environment:` sau `ports:` NU sunt
> afectate de acest bug — doar cele din interiorul `labels:`.

Soluția aplicată (și cea corectă, "ca la carte", pentru orice proiect care va
fi redeployat automat din Git prin Coolify): rutele NU mai sunt etichete, ci
sunt randate dintr-un **template** (`traefik/routes.tmpl.yml`) de un mic
script (`traefik/entrypoint.sh`) rulat la **fiecare pornire** a containerului
Traefik, care înlocuiește `__CLUSTER_DOMAIN__` cu valoarea reală citită din
variabila de mediu `CLUSTER_DOMAIN` (asta, fiind în `environment:`, NU e
afectată de bug). Beneficiu secundar: Traefik nu mai are nevoie deloc de
acces la `docker.sock`, pentru că nu mai citește etichete Docker.

**Implicație practică pentru tine**: dacă vreodată adaugi un serviciu nou cu
domeniu propriu în acest proiect, **NU** pune ruta ca etichetă `labels:` pe
serviciul respectiv — adaugi o intrare nouă în `traefik/routes.tmpl.yml`
(router + service), exact ca modelul deja existent pentru `nodered`,
`zigbee2mqtt`, `chirpstack`, `predict`.

### 2.8 DNS / `/etc/hosts` pe mașina de pe care accesezi

Pe orice mașină de pe care vrei să accesezi serviciile (nu pe reComputer):
```
172.16.1.120   nodered.test-bench.iotstack
172.16.1.120   zigbee2mqtt.test-bench.iotstack
172.16.1.120   chirpstack.test-bench.iotstack
172.16.1.120   traefik.test-bench.iotstack
172.16.1.120   coolify.test-bench.iotstack
172.16.1.120   predict.test-bench.iotstack
```

### 2.9 Verificare finală

```bash
curl -sk -o /dev/null -w '%{http_code}\n' --resolve nodered.test-bench.iotstack:9443:172.16.1.120 https://nodered.test-bench.iotstack:9443/
```
Repetă pentru fiecare subdomeniu, **din rețea** (nu doar loopback pe
reComputer) — loopback-ul poate arăta bine chiar și când accesul din rețea nu
funcționează (firewall, binding greșit etc.).

---

## Partea 3 — Deploy `agent-ML-realdata` (Dockerfile, doar serving)

### 3.1 Context — ce anume se deployează

Repo-ul are **două** Dockerfile-uri:
- `Dockerfile` (rădăcină) — aplicația COMPLETĂ de training (`/train`,
  `/eval`, `/dataset`, `/predict_command`), imagine grea (~633 MB, torch +
  pandas), are nevoie de volume persistente și de credențiale către API-ul
  BMS extern. **NU se deployează** (decizie explicită — doar serving, fără
  training, pentru moment).
- `Dockerfile.serving` — DOAR `/predict_command` și `/predict_command/debug`,
  imagine mică (~1/10 din cealaltă, doar numpy+fastapi), fără nicio
  credențială necesară, modelul e copiat direct în imagine la build. **Asta
  se deployează.**

**Detaliu critic, ușor de ratat**: fișierul `Dockerfile.serving` există doar
pe branch-ul **`production`**, NU pe `main` (verificat direct:
`git show origin/main:Dockerfile.serving` → "exists on disk, but not in
'origin/main'"). Dacă în Coolify selectezi branch-ul `main` din reflex, build-ul
eșuează cu "Dockerfile not found" sau similar.

### 3.2 Cheie SSH dedicată — DE DATA ASTA DOAR CITIRE

Diferență esențială față de §2.1: la `iot-test-bench` aveam nevoie de
scriere (noi facem `git push` acolo). La `agent-ML-realdata` **NU** —
Coolify doar clonează codul altcuiva ca să facă build, niciodată nu scrie
înapoi în acel repo.

```bash
ssh-keygen -t ed25519 -f ~/.ssh/github_agent_ml_serving -N '' -C 'agent-ml-serving@recomputer'
cat ~/.ssh/github_agent_ml_serving.pub
```

Pe GitHub → repo-ul `StefanVlad0/agent-ML-realdata` (are nevoie cineva cu
acces admin pe ACEL repo să facă asta, tu sau proprietarul) → **Settings →
Deploy keys → Add deploy key** → lipești cheia publică → **NU** bifezi
"Allow write access" (read-only, e suficient doar pentru clone).

### 3.3 Adăugarea cheii private în Coolify

La fel ca §2.2: **Keys & Tokens → Private Keys → Add Private Key** →
conținutul din `~/.ssh/github_agent_ml_serving` → nume ex.
`agent-ml-serving-key`.

**Capcană reală, întâlnită exact în acest proiect, merită subliniată**:
adăugarea cheii AICI nu înseamnă automat că resursa o și folosește. Am pățit
de două ori ca deploy-ul să eșueze cu `ERROR: Repository not found` pentru că
resursa Coolify rămăsese legată de ALTĂ cheie (implicit `localhost's key`,
sau cheia de la `iot-test-bench`, care evident n-are acces la repo-ul altcuiva).
**Verificare sigură**, dacă ai acces la baza de date Postgres a Coolify (utilă
mai ales când UI-ul pare că a salvat dar de fapt nu):
```bash
sudo docker exec coolify-db psql -U coolify -d coolify -c \
  "select id, name, git_repository, private_key_id from applications;"
sudo docker exec coolify-db psql -U coolify -d coolify -c \
  "select id, name, fingerprint from private_keys;"
```
`private_key_id` de pe rândul aplicației tale trebuie să corespundă cu `id`-ul
cheii corecte din al doilea tabel. Dacă nu corespunde, întoarce-te în UI la
resursă → Settings/Source → schimbă din dropdown cheia corectă → **caută
explicit un buton de Save/Update lângă acel câmp** — la unele versiuni
Coolify, schimbarea din dropdown nu se salvează automat fără o acțiune
explicită de confirmare.

### 3.4 Creare resursă în Coolify

**New Resource → Dockerfile** (NU Docker Compose, de data asta — un singur
container, fără orchestrare):

- **Repository**: `git@github.com:StefanVlad0/agent-ML-realdata.git`
- **Private Key**: `agent-ml-serving-key`
- **Branch**: **`production`** (nu `main` — vezi §3.1)
- **Dockerfile Location**: `/Dockerfile.serving`
- **Server**: aceeași mașină ca `iot-test-bench` (reComputer)
- **Ports Exposes**: `8000` (portul intern din imagine — vezi `EXPOSE 8000`
  și `CMD uvicorn ... --port ${PORT}` din `Dockerfile.serving`)
- **Ports Mappings**: `8002:8000` — alege un port de HOST liber; verifică
  întâi ce e deja ocupat:
  ```bash
  sudo ss -tlnp | grep -E ':(8000|8001|8002|8080|9080|9443) '
  ```
  (în acest proiect, 8001 era deja folosit de altă resursă Coolify
  preexistentă — de-aia s-a ales 8002).

### 3.5 Environment Variables — de ce NU sunt necesare aici

Spre deosebire de `iot-test-bench`, la această resursă **nu adaugi nimic** în
Environment Variables. Motiv verificat direct în cod (`app/serving_app.py`,
`app/inference.py`, `app/features.py`) — calea de serving NU importă
`bms_client`, deci nu are nevoie de `BMS_USERNAME`/`BMS_PASSWORD`/
`BMS_BASE_URL` — acelea sunt necesare DOAR pentru `/train`, care nu se
deployează aici.

### 3.6 Volume persistente — de ce NU sunt necesare aici

Modelul (`models/iql_model.npz`) e copiat **direct în imagine, la build**
(`COPY models/iql_model.npz ./models/` în `Dockerfile.serving`), deci nu are
nevoie de volum persistent — modelul "trăiește" în imagine, nu pe disc extern.

**Implicație importantă pentru viitor**: dacă cineva antrenează un model nou
(pe altă mașină, cu `Dockerfile`-ul complet), actualizarea aici înseamnă
commit + push al noului `iql_model.npz` pe branch-ul `production`, urmat de
un **redeploy** — nu există niciun mecanism de "hot-swap" al modelului fără
rebuild de imagine.

### 3.7 Verificare

```bash
curl -s http://127.0.0.1:8002/health
```
Răspuns așteptat: `{"status":"ok","model_path":"...","model_trained":true,"trained_at":"..."}`.
Dacă `model_trained` e `false`, imaginea s-a construit dar fără fișierul
`.npz` — verifică dacă `models/iql_model.npz` chiar există și e urmărit în
git pe branch-ul `production` (`git ls-files | grep model`).

---

## Partea 4 — Integrarea finală: ruta `predict.<domeniu>` prin Traefik

Odată ce §3 e complet și `/health` răspunde direct pe portul de host, ultimul
pas e să faci serviciul accesibil prin **același domeniu HTTPS** ca restul
stack-ului, nu doar direct pe IP:port.

În `traefik/routes.tmpl.yml` din `iot-test-bench`, adaugi un router + un
service nou (pattern identic cu cel deja existent pentru `coolify`):

```yaml
# in sectiunea "routers":
predict:
  rule: "Host(`predict.__CLUSTER_DOMAIN__`)"
  entrypoints:
    - websecure
  tls: true
  service: predict
  middlewares:
    - admin-auth@file   # serviciul NU are login propriu, deci OBLIGATORIU

# in sectiunea "services":
predict:
  loadBalancer:
    servers:
      - url: "http://host.docker.internal:8002"
```

**De ce `host.docker.internal:8002` și nu numele containerului direct** (cum
se face pentru `nodered`/`zigbee2mqtt`/`chirpstack`, ex. `http://nodered:1880`)?
Pentru că resursa ML e o aplicație Coolify **separată**, pe altă rețea Docker
internă — Traefik-ul din `iot-test-bench` n-o poate găsi după nume de
container. Soluția simplă, deja folosită și pentru rutarea către Coolify
însuși: publici portul pe HOST (`8002:8000`, vezi §3.4) și Traefik îl accesează
prin gazdă, la fel cum ar accesa orice alt serviciu extern.

După editare: commit + push în `iot-test-bench`, apoi restart pe containerul
Traefik ca să re-randeze `routes.yml` din template (fișierul `.tmpl` nu e
"urmărit" (`watch`) de Traefik, doar rezultatul randat este — deci un simplu
`docker restart <container-traefik>` e suficient, nu trebuie recreare completă).

---

## Anexă — index rapid de probleme întâlnite și cauza reală

| Simptom | Cauză reală | Unde e explicat |
|---|---|---|
| `Could not resolve hostname github.com` | Blip DNS tranzitoriu (routerul local) | §2.5-a |
| Doar Traefik pornește | `COMPOSE_PROFILES` lipsă | §2.4, §2.5-b |
| `port is already allocated` | Deploy vechi manual încă rulează, sau port ales deja ocupat | §2.5-c, §3.4 |
| Container crash loop, `Is a directory` | Bug Docker: bind mount cu sursă lipsă devine director gol | §2.5-d |
| Rută 404 deși totul pare corect, doar după redeploy din Git | Coolify dublează `$` în `labels:` | §2.7 |
| `middleware "admin-auth@file" does not exist` | `setup-traefik-auth.sh` nu a fost rulat pentru ACEA resursă | §2.6 |
| `ERROR: Repository not found` la deploy | Private Key greșit selectat/neconfirmat pe acea resursă | §3.3 |
| Doar un container pornește dintr-un compose cu mai multe servicii | Build Pack setat greșit (auto-detect în loc de Docker Compose) | §2.3 |

---

## Ce NU e clar din context și ar trebui confirmat

- **Pentru `agent-ML-realdata`**: dacă la un moment dat se decide să se
  deployeze și aplicația de TRAINING (`Dockerfile` complet, cu `/train`),
  aceasta are nevoie de propriile Environment Variables (`BMS_USERNAME`,
  `BMS_PASSWORD`, `BMS_BASE_URL`, `BMS_VERIFY_SSL=1` — NU `0`, ăla e doar
  pentru dev local) și de volume persistente pe `/app/data`, `/app/data-raw`,
  `/app/models` — altfel un redeploy șterge modelul antrenat. Nu era în scopul
  acestei implementări, dar merită documentat separat dacă devine relevant.
- **Numărul exact de porturi libere pe reComputer** se poate schimba dacă se
  adaugă alte resurse Coolify între timp — verifică mereu cu `ss -tlnp`
  înainte să alegi un port nou, nu presupune că 8002 rămâne mereu liber după
  acest punct.
