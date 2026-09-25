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
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from xknx import XKNX
from xknx.io import ConnectionConfig, ConnectionType
from xknx.management.procedures import nm_individual_address_check
from xknx.telegram import apci
from xknx.telegram.address import IndividualAddress

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
# NU da adresa de grup asociata (aia ar necesita Address Table + Association
# Table, cercetare separata, nefinalizata) si NU da DPT-ul exact (doar
# dimensiunea in biti - mai multe DPT-uri au aceeasi dimensiune, ex. 5.001 si
# 5.010 sunt ambele 8 biti).
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
    }


class ScanRequest(BaseModel):
    area: int
    line: int
    start_device: int = 1
    end_device: int = 255
    delay: float = 0.1


app = FastAPI(title="KNX Scanner")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "own_address": OWN_INDIVIDUAL_ADDRESS, "local_ip": LOCAL_IP,
             "manufacturers_loaded": len(MANUFACTURERS)}


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


async def _find_got_index(address: str) -> int | None:
    """Conexiune separata, doar pentru a localiza Group Object Table (daca exista).

    Unele dispozitive (alta generatie de cip, ex. Intesis) trimit un disconnect
    explicit cand verific un object_index care nu exista, nu doar tacere -
    exceptia asta poate scapa din try/except-ul de pe citirea individuala si
    iese din blocul "async with" la iesire. Izolat aici, intr-o conexiune
    separata cu propriul try/except larg, ca o deconectare neasteptata sa
    insemne pur si simplu "nu am gasit", nu o eroare 502 pentru tot endpoint-ul.
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
                    if otype == 9:
                        return obj_idx
                except Exception:
                    return None
                await asyncio.sleep(0.05)
    except Exception:
        return None
    finally:
        await xknx.stop()
    return None


@app.get("/api/device/{address}/parameters")
async def device_parameters(address: str) -> dict:
    got_index = await _find_got_index(address)
    if got_index is None:
        return _no_got_result(address)

    result: dict = {
        "address": address,
        "group_object_table_found": True,
        "group_object_table_index": got_index,
        "entries": [],
    }

    xknx = XKNX(connection_config=_connection_config())
    await xknx.start()
    try:
        async with xknx.management.connection(IndividualAddress(address)) as conn:
            all_data = b""
            start = 1
            chunk = 15
            max_entries = 250
            while start <= max_entries:
                n = min(chunk, max_entries - start + 1)
                try:
                    response = await asyncio.wait_for(
                        conn.request(
                            payload=apci.PropertyValueRead(
                                object_index=got_index, property_id=PID_TABLE, count=n, start_index=start
                            ),
                            expected=apci.PropertyValueResponse,
                        ),
                        timeout=3,
                    )
                    if isinstance(response.payload, apci.PropertyValueResponse):
                        if not response.payload.data:
                            break
                        all_data += response.payload.data
                    else:
                        break
                except Exception:
                    break
                start += n
                await asyncio.sleep(0.1)
    except Exception:
        pass  # pastram ce am apucat sa citim pana la eventuala deconectare
    finally:
        await xknx.stop()

    for i in range(0, len(all_data) - 1, 2):
        raw = all_data[i:i + 2]
        if raw == b"\x00\x00":
            continue
        entry_num = i // 2 + 1
        decoded = _decode_got_entry(raw)
        decoded["entry"] = entry_num
        result["entries"].append(decoded)
    return result
