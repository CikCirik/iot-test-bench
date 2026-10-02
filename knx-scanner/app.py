"""Interfata web pentru scanare/detectie manuala pe bus-ul KNX.

Reuneste, intr-un singur serviciu, logica validata separat (scanare adrese
individuale + citire date fizice dispozitiv) - fara scriere pe bus (doar
servicii KNX standard de citire: T_Connect, A_DeviceDescriptor_Read,
A_PropertyValue_Read, A_PropertyDescription_Read).

Ruleaza cu network_mode: host (vezi docker-compose.yaml) - necesar pentru
multicast KNX/IP fiabil, la fel ca serviciul nodered din acelasi proiect.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from xknx import XKNX
from xknx.io import ConnectionConfig, ConnectionType
from xknx.management.procedures import nm_individual_address_check
from xknx.telegram import apci
from xknx.telegram.address import IndividualAddress
from xknxproject import XKNXProj
from xknxproject.exceptions import InvalidPasswordException

BASE_DIR = Path(__file__).parent

OWN_INDIVIDUAL_ADDRESS = os.environ.get("KNX_OWN_ADDRESS", "15.15.250")
LOCAL_IP = os.environ.get("KNX_LOCAL_IP", "192.168.21.195")

PID_SERIAL_NUMBER = 11
PID_MANUFACTURER_ID = 12
PID_PROGRAM_VERSION = 13
PID_ORDER_INFO = 15
PID_HARDWARE_TYPE = 78
PID_FIRMWARE_REVISION = 9
PID_MAX_APDU_LENGTH = 56

OBJECT_TYPE_NAMES = {
    0: "Device", 1: "Address Table", 2: "Association Table", 3: "Application Program",
    4: "Interface Program", 5: "KNX Object Association Table", 6: "Router",
    7: "LTE Address Routing Table", 8: "cEMI Server", 9: "Group Object Table",
    10: "Polling Master", 11: "KNXnet/IP Parameter", 13: "File Server",
    17: "Security", 19: "RF Medium",
}

try:
    with open(BASE_DIR / "knx_manufacturers.json", encoding="utf-8") as f:
        MANUFACTURERS: dict[str, str] = json.load(f)
except FileNotFoundError:
    MANUFACTURERS = {}


def _connection_config() -> ConnectionConfig:
    return ConnectionConfig(
        connection_type=ConnectionType.ROUTING,
        individual_address=OWN_INDIVIDUAL_ADDRESS,
        local_ip=LOCAL_IP,
    )


async def _read_property(conn, object_index: int, property_id: int, count: int = 1) -> bytes | None:
    try:
        response = await asyncio.wait_for(
            conn.request(
                payload=apci.PropertyValueRead(
                    object_index=object_index, property_id=property_id, count=count, start_index=1
                ),
                expected=apci.PropertyValueResponse,
            ),
            timeout=3,
        )
        if isinstance(response.payload, apci.PropertyValueResponse):
            return response.payload.data
    except Exception:
        pass
    return None


def _manufacturer_name(manufacturer_id: str) -> str:
    return MANUFACTURERS.get(str(int(manufacturer_id)), "necunoscut (nu e in registrul salvat)")


def _decode_order_info(raw: bytes | None) -> str | None:
    if not raw:
        return None
    stripped = raw.rstrip(b"\x00")
    if stripped and all(32 <= b < 127 for b in stripped):
        return stripped.decode("ascii")
    return None


# --------------------------------------------------------------------------
# Decodare Group Object Table (Object Type 9), pentru dispozitive System B
# (mask 0x0*7B0 si variante). Format confirmat empiric (validat pe un
# controller DALI cunoscut - 16 din 16 intrari asteptate au iesit exact cum
# trebuia) + sursa calimero-device (KnxDeviceServiceLogic.java,
# groupObjectDescriptor() si valueFieldTypeToBits()):
#   - fiecare intrare = 2 octeti, index 1-based, citit prin A_PropertyValue_Read
#     pe PID_TABLE (23) al obiectului Group Object Table.
#   - octet 0 = config: biti 0-1 prioritate, bit 2 = comunicare/enable,
#     bit 3 = citire/responder (valid doar daca enable), bit 7 = update-on-
#     response (valid doar daca enable). Bitul de transmit (0x40) NU e
#     confirmat pentru acest format pe 2 octeti - il raportam brut, neinterpretat.
#   - octet 1 = cod tip camp -> numar de biti ai valorii (tabel exact).
# NU da DPT-ul exact (doar dimensiunea in biti - mai multe DPT-uri au aceeasi
# dimensiune, ex. 5.001 si 5.010 sunt ambele 8 biti). Adresa de grup asociata
# fiecarei intrari vine din Address Table + Association Table, decodate mai
# jos.
# --------------------------------------------------------------------------
PID_TABLE = 23

TYPE_CODE_TO_BITS = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 7, 7: 8, 8: 16,
                       9: 24, 10: 32, 11: 48, 12: 64, 13: 80, 14: 112}

# Heuristica bits -> familii DPT plauzibile (NU exact, aceeasi limitare ca
# in calimero: mai multe DPT-uri au aceeasi dimensiune in biti).
BITS_TO_PLAUSIBLE_DPT = {
    1: ["1.xxx (switch/bool)"],
    2: ["2.xxx (control 1 bit + prioritate)"],
    4: ["3.xxx (dimming control)", "18.xxx (scene control)"],
    8: ["5.xxx (unsigned 8-bit, ex. 5.001 procent)", "6.xxx (signed 8-bit)", "20.xxx (enum HVAC)"],
    16: ["7.xxx (unsigned 16-bit)", "8.xxx (signed 16-bit)", "9.xxx (float 16-bit, ex. temperatura)"],
    32: ["12.xxx (unsigned 32-bit)", "13.xxx (signed 32-bit)", "14.xxx (float 32-bit)"],
}


def _decode_got_entry(raw: bytes) -> dict:
    config, type_code = raw[0], raw[1]
    bits = TYPE_CODE_TO_BITS.get(type_code, 2016 if type_code == 255 else max((type_code - 6) * 8, 0))
    enable = bool(config & 0x04)
    return {
        "raw_hex": raw.hex(),
        "priority": config & 0x03,
        "communication_enable": enable,
        "read_responder": enable and bool(config & 0x08),
        "update_on_response": enable and bool(config & 0x80),
        "type_code": type_code,
        "bits": bits,
        "plausible_dpt": BITS_TO_PLAUSIBLE_DPT.get(bits, [f"necunoscut ({bits} biti)"]),
        "group_addresses": [],
    }


# --------------------------------------------------------------------------
# Decodare Address Table (Object Type 1) + Association Table (Object Type 2),
# pentru a lega fiecare intrare din Group Object Table de adresa de grup ei
# reala. Format confirmat empiric (validat pe acelasi controller DALI - toate
# cele 15 asocieri citite s-au potrivit exact, incrucisat, intre cele 3
# tabele) + sursa calimero-device pentru principiul general (Address Table =
# lista de GA-uri fizice ale dispozitivului, Association Table = leaga
# indexul din Address Table de un obiect din Group Object Table):
#   - Address Table: intrari de 2 octeti fiecare, index 1-based, FARA header/
#     count la inceput - direct adrese de grup brute (16-bit), consecutive.
#   - Association Table: intrari de 4 octeti fiecare: primii 2 octeti = index
#     1-based in Address Table, urmatorii 2 octeti = numarul intrarii (1-based)
#     din Group Object Table (acelasi numar folosit de _decode_got_entry).
# --------------------------------------------------------------------------


def _ga_str(raw_ga: int) -> str:
    return f"{(raw_ga >> 11) & 0x1F}/{(raw_ga >> 8) & 0x7}/{raw_ga & 0xFF}"


def _decode_address_table(raw: bytes) -> dict[int, int]:
    table: dict[int, int] = {}
    for i in range(0, len(raw) - 1, 2):
        raw_ga = int.from_bytes(raw[i:i + 2], "big")
        if raw_ga:
            table[i // 2 + 1] = raw_ga
    return table


def _decode_association_table(raw: bytes) -> list[dict]:
    entries: list[dict] = []
    for i in range(0, len(raw) - 3, 4):
        addr_idx = int.from_bytes(raw[i:i + 2], "big")
        got_entry = int.from_bytes(raw[i + 2:i + 4], "big")
        if addr_idx == 0 and got_entry == 0:
            continue
        entries.append({"address_table_index": addr_idx, "group_object_entry": got_entry})
    return entries


class ScanRequest(BaseModel):
    area: int
    line: int
    start_device: int = 1
    end_device: int = 255
    delay: float = 0.1


# --------------------------------------------------------------------------
# Import proiect ETS (.knxproj) - sursa reala de adevar pentru semantica
# (nume + DPT + adresa de grup per obiect de comunicare al fiecarui
# dispozitiv). Scanarea bus-ului nu poate produce niciodata asta singura -
# validat azi (16/16 exact match) pe un proiect ETS real, exportat curent.
# Proiectul parsat se tine in memorie (un singur proiect activ per instanta
# de serviciu - unealta e folosita manual, de un singur operator).
# --------------------------------------------------------------------------
_PROJECT: dict | None = None


def _format_dpt(dpt: dict | None) -> str | None:
    if not dpt:
        return None
    main = dpt.get("main")
    sub = dpt.get("sub")
    if main is None:
        return None
    if sub is None:
        return str(main)
    return f"{main}.{sub:03d}"


def _project_device_summary(address: str, dev: dict) -> dict:
    return {
        "individual_address": address,
        "name": dev.get("name"),
        "manufacturer_name": dev.get("manufacturer_name"),
        "order_number": dev.get("order_number"),
        "communication_objects": len(dev.get("communication_object_ids", [])),
    }


app = FastAPI(title="KNX Scanner")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "own_address": OWN_INDIVIDUAL_ADDRESS, "local_ip": LOCAL_IP,
             "manufacturers_loaded": len(MANUFACTURERS)}


@app.post("/api/project/upload")
async def project_upload(file: UploadFile = File(...), password: str = Form("")) -> dict:
    global _PROJECT
    raw = await file.read()
    with tempfile.NamedTemporaryFile(suffix=".knxproj", delete=True) as tmp:
        tmp.write(raw)
        tmp.flush()
        try:
            parsed = XKNXProj(path=tmp.name, password=password or None).parse()
        except InvalidPasswordException as exc:
            raise HTTPException(status_code=400, detail="Parola proiectului e gresita sau lipseste.") from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Nu am putut parsa proiectul: {exc}") from exc
    _PROJECT = parsed
    info = parsed.get("info", {})
    return {
        "name": info.get("name"),
        "last_modified": info.get("last_modified"),
        "created_by": info.get("created_by"),
        "devices_count": len(parsed.get("devices", {})),
        "group_addresses_count": len(parsed.get("group_addresses", {})),
    }


@app.get("/api/project/status")
async def project_status() -> dict:
    if _PROJECT is None:
        return {"loaded": False}
    info = _PROJECT.get("info", {})
    return {
        "loaded": True,
        "name": info.get("name"),
        "last_modified": info.get("last_modified"),
        "devices_count": len(_PROJECT.get("devices", {})),
        "group_addresses_count": len(_PROJECT.get("group_addresses", {})),
    }


@app.get("/api/project/devices")
async def project_devices() -> dict:
    if _PROJECT is None:
        raise HTTPException(status_code=404, detail="Niciun proiect ETS incarcat inca.")
    devices = _PROJECT.get("devices", {})
    return {"devices": [_project_device_summary(addr, dev) for addr, dev in sorted(devices.items())]}


@app.get("/api/project/device/{address}")
async def project_device(address: str) -> dict:
    if _PROJECT is None:
        raise HTTPException(status_code=404, detail="Niciun proiect ETS incarcat inca.")
    devices = _PROJECT.get("devices", {})
    dev = devices.get(address)
    if dev is None:
        raise HTTPException(status_code=404, detail=f"Adresa {address} nu exista in proiectul ETS incarcat.")

    co_dict = _PROJECT.get("communication_objects", {})
    objects = []
    for cid in dev.get("communication_object_ids", []):
        co = co_dict.get(cid)
        if not co:
            continue
        flags = co.get("flags", {})
        objects.append({
            "id": cid,
            "name": co.get("name"),
            "text": co.get("text"),
            "function_text": co.get("function_text"),
            "dpt": _format_dpt(co.get("dpts")[0]) if co.get("dpts") else None,
            "object_size": co.get("object_size"),
            "flags": flags,
            "group_addresses": co.get("group_address_links", []),
        })

    return {
        "individual_address": address,
        "name": dev.get("name"),
        "manufacturer_name": dev.get("manufacturer_name"),
        "order_number": dev.get("order_number"),
        "hardware_name": dev.get("hardware_name"),
        "communication_objects": objects,
    }


# --------------------------------------------------------------------------
# Config final - subsetul de obiecte de comunicare pe care operatorul le
# alege manual din proiectul ETS (pas 3 din flow: import -> scanare -> alocare).
# Persistat pe disc (nu doar in memorie ca proiectul ETS) intr-un volum Docker
# dedicat, ca sa supravietuiasca unui restart/redeploy al containerului -
# spre deosebire de proiectul incarcat, care e ok sa se piarda.
# --------------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("KNX_DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA_DIR / "config.json"
_config_lock = asyncio.Lock()


def _load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"entries": {}}


def _save_config(config: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


class ConfigEntryIn(BaseModel):
    id: str
    individual_address: str
    device_name: str | None = None
    group_addresses: list[str] = []
    dpt: str | None = None
    object_size: str | None = None
    flags: dict = {}
    label: str = ""
    room: str = ""


class ConfigEntriesIn(BaseModel):
    entries: list[ConfigEntryIn]


@app.get("/api/config")
async def get_config() -> dict:
    config = _load_config()
    return {"entries": list(config.get("entries", {}).values())}


@app.post("/api/config/entries")
async def upsert_config_entries(payload: ConfigEntriesIn) -> dict:
    async with _config_lock:
        config = _load_config()
        entries = config.setdefault("entries", {})
        for e in payload.entries:
            entries[e.id] = e.model_dump()
        _save_config(config)
        return {"entries": list(entries.values())}


@app.delete("/api/config/entries/{entry_id:path}")
async def delete_config_entry(entry_id: str) -> dict:
    async with _config_lock:
        config = _load_config()
        entries = config.get("entries", {})
        entries.pop(entry_id, None)
        _save_config(config)
        return {"entries": list(entries.values())}


@app.post("/api/scan")
async def scan(req: ScanRequest) -> dict:
    xknx = XKNX(connection_config=_connection_config())
    await xknx.start()
    found: list[str] = []
    checked = 0
    try:
        for device in range(req.start_device, req.end_device + 1):
            addr = f"{req.area}.{req.line}.{device}"
            try:
                present = await nm_individual_address_check(xknx, addr)
            except Exception:
                present = False
            checked += 1
            if present:
                found.append(addr)
            await asyncio.sleep(req.delay)
    finally:
        await xknx.stop()
    return {"checked": checked, "found": found}


@app.get("/api/device/{address}")
async def device_info(address: str) -> dict:
    xknx = XKNX(connection_config=_connection_config())
    await xknx.start()
    info: dict = {"address": address}
    try:
        async with xknx.management.connection(IndividualAddress(address)) as conn:
            try:
                dd = await asyncio.wait_for(
                    conn.request(
                        payload=apci.DeviceDescriptorRead(descriptor=0),
                        expected=apci.DeviceDescriptorResponse,
                    ),
                    timeout=3,
                )
                if isinstance(dd.payload, apci.DeviceDescriptorResponse):
                    info["mask_version"] = f"{dd.payload.value:#06x}"
            except Exception:
                info["mask_version"] = None

            serial = await _read_property(conn, 0, PID_SERIAL_NUMBER)
            manufacturer_id = None
            serial_hex = None
            if serial:
                manufacturer_id = str(int.from_bytes(serial[0:2], "big"))
                serial_hex = serial[2:].hex()
            else:
                manuf = await _read_property(conn, 0, PID_MANUFACTURER_ID)
                if manuf:
                    manufacturer_id = str(int.from_bytes(manuf, "big"))

            info["manufacturer_id"] = manufacturer_id
            info["manufacturer_name"] = _manufacturer_name(manufacturer_id) if manufacturer_id else None
            info["serial"] = serial_hex

            order = await _read_property(conn, 0, PID_ORDER_INFO)
            info["order_info_hex"] = order.hex() if order else None
            info["order_info_ascii"] = _decode_order_info(order)

            hw = await _read_property(conn, 0, PID_HARDWARE_TYPE)
            info["hardware_type"] = hw.hex() if hw else None

            fw = await _read_property(conn, 0, PID_FIRMWARE_REVISION)
            info["firmware_revision"] = fw.hex() if fw else None

            apdu = await _read_property(conn, 0, PID_MAX_APDU_LENGTH)
            info["max_apdu_length"] = int.from_bytes(apdu, "big") if apdu else None
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Nu am putut citi {address}: {exc}") from exc
    finally:
        await xknx.stop()
    return info


@app.get("/api/device/{address}/objects")
async def device_objects(address: str) -> dict:
    xknx = XKNX(connection_config=_connection_config())
    await xknx.start()
    objects: list[dict] = []
    try:
        async with xknx.management.connection(IndividualAddress(address)) as conn:
            for obj_idx in range(0, 15):
                try:
                    response = await asyncio.wait_for(
                        conn.request(
                            payload=apci.PropertyValueRead(
                                object_index=obj_idx, property_id=1, count=1, start_index=1
                            ),
                            expected=apci.PropertyValueResponse,
                        ),
                        timeout=2,
                    )
                    if not (isinstance(response.payload, apci.PropertyValueResponse) and response.payload.data):
                        break
                    otype = int.from_bytes(response.payload.data, "big")
                except Exception:
                    break

                properties = []
                for prop_idx in range(1, 26):
                    try:
                        desc = await asyncio.wait_for(
                            conn.request(
                                payload=apci.PropertyDescriptionRead(
                                    object_index=obj_idx, property_id=0, property_index=prop_idx
                                ),
                                expected=apci.PropertyDescriptionResponse,
                            ),
                            timeout=2,
                        )
                        p = desc.payload
                        if p.property_id == 0 and p.max_count == 0:
                            break
                        entry = {"property_id": p.property_id, "type": p.type_,
                                  "max_count": p.max_count, "access": p.access}
                        if p.property_id != 7:  # 7 = TABLE_REFERENCE, e adresa de memorie, nu date
                            value = await _read_property(conn, obj_idx, p.property_id,
                                                           count=min(p.max_count, 10) or 1)
                            if value is not None:
                                entry["value_hex"] = value.hex()
                        properties.append(entry)
                    except Exception:
                        break
                    await asyncio.sleep(0.05)

                objects.append({
                    "object_index": obj_idx,
                    "object_type": otype,
                    "object_type_name": OBJECT_TYPE_NAMES.get(otype, f"necunoscut ({otype})"),
                    "properties": properties,
                })
                await asyncio.sleep(0.05)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Nu am putut explora {address}: {exc}") from exc
    finally:
        await xknx.stop()
    return {"address": address, "objects": objects}


def _no_got_result(address: str) -> dict:
    return {
        "address": address,
        "group_object_table_found": False,
        "entries": [],
        "message": (
            "Acest dispozitiv nu are un Group Object Table modern (Object Type 9) - "
            "foloseste modelul clasic (Address Table + Association Table), care nu poate "
            "fi decodificat momentan (necesita citire de memorie bruta, nesuportata de "
            "acest dispozitiv, sau cercetare suplimentara pe Address/Association Table)."
        ),
    }


async def _find_object_index(address: str, object_type: int) -> int | None:
    """Conexiune separata, doar pentru a localiza un Object Type dat (daca exista).

    Unele dispozitive (alta generatie de cip, ex. Intesis) trimit un disconnect
    explicit cand verific un object_index care nu exista, nu doar tacere -
    exceptia asta poate scapa din try/except-ul de pe citirea individuala si
    iese din blocul "async with" la iesire. Izolat aici, intr-o conexiune
    separata cu propriul try/except larg, ca o deconectare neasteptata sa
    insemne pur si simplu "nu am gasit", nu o eroare 502 pentru tot endpoint-ul.
    Fiecare tip de obiect cautat (Address Table=1, Association Table=2, Group
    Object Table=9) foloseste propria conexiune izolata - la fel ca citirea
    propriu-zisa a continutului (vezi _read_got_raw / _read_indexed_table_raw).
    """
    xknx = XKNX(connection_config=_connection_config())
    await xknx.start()
    try:
        async with xknx.management.connection(IndividualAddress(address)) as conn:
            for obj_idx in range(0, 15):
                try:
                    response = await asyncio.wait_for(
                        conn.request(
                            payload=apci.PropertyValueRead(
                                object_index=obj_idx, property_id=1, count=1, start_index=1
                            ),
                            expected=apci.PropertyValueResponse,
                        ),
                        timeout=2,
                    )
                    if not (isinstance(response.payload, apci.PropertyValueResponse) and response.payload.data):
                        return None
                    otype = int.from_bytes(response.payload.data, "big")
                    if otype == object_type:
                        return obj_idx
                except Exception:
                    return None
                await asyncio.sleep(0.05)
    except Exception:
        return None
    finally:
        await xknx.stop()
    return None


async def _read_got_raw(address: str, object_index: int) -> bytes:
    """Citeste PID_TABLE al Group Object Table, in propria conexiune izolata.

    GOT e un array de dimensiune fixa (padded) - sloturile neconfigurate
    intorc 2 octeti de zero, nu lipsesc din raspuns - asa ca citim pana la un
    plafon fix (250) cu bucati de 15, fara sa avem nevoie de un header de
    count la start_index=0 (nu s-a gasit unul pe GOT; spre deosebire de
    Address/Association Table, vezi _read_indexed_table_raw).
    """
    xknx = XKNX(connection_config=_connection_config())
    await xknx.start()
    all_data = b""
    try:
        async with xknx.management.connection(IndividualAddress(address)) as conn:
            start, chunk, max_entries = 1, 15, 250
            while start <= max_entries:
                n = min(chunk, max_entries - start + 1)
                try:
                    response = await asyncio.wait_for(
                        conn.request(
                            payload=apci.PropertyValueRead(
                                object_index=object_index, property_id=PID_TABLE, count=n, start_index=start
                            ),
                            expected=apci.PropertyValueResponse,
                        ),
                        timeout=3,
                    )
                    if isinstance(response.payload, apci.PropertyValueResponse) and response.payload.data:
                        all_data += response.payload.data
                    else:
                        break
                except Exception:
                    break
                start += n
                await asyncio.sleep(0.15)
    except Exception:
        pass  # pastram ce am apucat sa citim pana la eventuala deconectare
    finally:
        await xknx.stop()
    return all_data


async def _read_indexed_table_raw(address: str, object_index: int, max_chunk: int) -> bytes:
    """Citeste PID_TABLE al Address Table / Association Table, in propria
    conexiune izolata.

    Spre deosebire de GOT, aceste doua tabele NU sunt array-uri padded - au
    un header real la start_index=0 (2 octeti = numarul de intrari valide,
    validat pe mai multe dispozitive: 0x0010=16 pe Address+Association Table
    ale unui controller DALI cunoscut, 0x0005=5 pe Address Table a doua
    dispozitive Weinzierl). Root cause al problemei de fiabilitate gasite
    initial: dispozitivele testate REFUZA (raspuns gol, count=0) orice cerere
    care cere mai multe elemente decat mai raman valide de la start_index dat
    - de-asta un plafon de citire ghicit orbeste (15, sau chiar 5) esua in
    functie de cate intrari avea de fapt tabelul. Solutia corecta: citim
    header-ul intai, apoi cerem exact atatea cate stim sigur ca exista, in
    bucati de maxim `max_chunk` care nu depasesc niciodata restul real.
    Daca header-ul nu arata ca un numar plauzibil de intrari (ex. dispozitive
    cu model de tabel diferit/mai vechi, precum 1.1.1/1.1.3 - Siemens, dar
    fara Group Object Table), consideram tabelul necitit in acest format si
    intoarcem gol, in loc sa ghicim gresit.
    """
    xknx = XKNX(connection_config=_connection_config())
    await xknx.start()
    all_data = b""
    try:
        async with xknx.management.connection(IndividualAddress(address)) as conn:
            try:
                header_response = await asyncio.wait_for(
                    conn.request(
                        payload=apci.PropertyValueRead(
                            object_index=object_index, property_id=PID_TABLE, count=1, start_index=0
                        ),
                        expected=apci.PropertyValueResponse,
                    ),
                    timeout=3,
                )
                header = header_response.payload.data if isinstance(
                    header_response.payload, apci.PropertyValueResponse
                ) else None
            except Exception:
                header = None
            total = int.from_bytes(header, "big") if header else None
            if total is None or not (0 < total <= 1024):
                return b""
            await asyncio.sleep(0.15)

            start = 1
            while start <= total:
                n = min(max_chunk, total - start + 1)
                try:
                    response = await asyncio.wait_for(
                        conn.request(
                            payload=apci.PropertyValueRead(
                                object_index=object_index, property_id=PID_TABLE, count=n, start_index=start
                            ),
                            expected=apci.PropertyValueResponse,
                        ),
                        timeout=3,
                    )
                    if isinstance(response.payload, apci.PropertyValueResponse) and response.payload.data:
                        all_data += response.payload.data
                    else:
                        break
                except Exception:
                    break
                start += n
                await asyncio.sleep(0.15)
    except Exception:
        pass  # pastram ce am apucat sa citim pana la eventuala deconectare
    finally:
        await xknx.stop()
    return all_data


async def _classic_fallback(address: str) -> dict:
    """Dispozitiv fara Group Object Table (model clasic) - incercam macar Address Table."""
    addr_index = await _find_object_index(address, 1)
    if addr_index is None:
        return _no_got_result(address)
    addr_raw = await _read_indexed_table_raw(address, addr_index, max_chunk=15)
    addr_table = _decode_address_table(addr_raw)
    if not addr_table:
        return _no_got_result(address)
    return {
        "address": address,
        "group_object_table_found": False,
        "address_table_index": addr_index,
        "group_addresses": [_ga_str(ga) for ga in addr_table.values()],
        "entries": [],
        "message": (
            "Acest dispozitiv nu are Group Object Table (Object Type 9) - foloseste "
            "modelul clasic. Am putut citi Address Table (lista de adrese de grup "
            "folosite de dispozitiv), dar nu si asocierea cu parametrii/DPT "
            "(Application Program-ul clasic nu expune asocierea generic, ci doar "
            "per-producator)."
        ),
    }


@app.get("/api/device/{address}/parameters")
async def device_parameters(address: str) -> dict:
    got_index = await _find_object_index(address, 9)
    if got_index is None:
        return await _classic_fallback(address)

    result: dict = {
        "address": address,
        "group_object_table_found": True,
        "group_object_table_index": got_index,
        "entries": [],
    }

    got_raw = await _read_got_raw(address, got_index)
    got_entries: dict[int, dict] = {}
    for i in range(0, len(got_raw) - 1, 2):
        raw = got_raw[i:i + 2]
        if raw == b"\x00\x00":
            continue
        entry_num = i // 2 + 1
        decoded = _decode_got_entry(raw)
        decoded["entry"] = entry_num
        got_entries[entry_num] = decoded
        result["entries"].append(decoded)

    addr_index = await _find_object_index(address, 1)
    assoc_index = await _find_object_index(address, 2)
    result["address_table_index"] = addr_index
    result["association_table_index"] = assoc_index
    if addr_index is not None and assoc_index is not None:
        addr_table = _decode_address_table(await _read_indexed_table_raw(address, addr_index, max_chunk=15))
        associations = _decode_association_table(
            await _read_indexed_table_raw(address, assoc_index, max_chunk=5)
        )
        result["address_table_entries"] = len(addr_table)
        result["associations_found"] = len(associations)
        for assoc in associations:
            ga_raw = addr_table.get(assoc["address_table_index"])
            entry = got_entries.get(assoc["group_object_entry"])
            if ga_raw is not None and entry is not None:
                entry["group_addresses"].append(_ga_str(ga_raw))
    else:
        result["note"] = (
            "Address Table / Association Table nu au fost gasite - adresele de "
            "grup nu au putut fi asociate cu parametrii (raman doar biti/DPT plauzibil)."
        )
    return result
