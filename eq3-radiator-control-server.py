#!/usr/bin/env python3

import json
import signal
import asyncio
import datetime
import json.decoder

from aiocache import Cache
from pydantic import BaseModel
from fastapi import FastAPI, BackgroundTasks
from fastapi.logger import logger
from subprocess import run, Popen, TimeoutExpired, PIPE


class Temperature(BaseModel):
    setpoint: float


app = FastAPI()
cache = Cache()
thermostats_states = {}
query_task = None


@app.on_event("startup")
async def start_background_query():
    query_task = asyncio.create_task(_query_known_thermostats())

@app.on_event("shutdown")
async def stop_background_query():
    query_task.cancel()

async def _query_known_thermostats():
    while True:
        for hwaddr in thermostats_states.keys():
            new_state = await _thermostat_state(hwaddr)
            if "temperature" in new_state:
                thermostats_states[hwaddr] = new_state
            await asyncio.sleep(1)
        await asyncio.sleep(3)

async def _thermostat_state(hwaddr: str):
    res = run(["./eq3.exp", hwaddr, "json"], capture_output=True)
    try:
        state = json.loads(res.stdout)
    except json.decoder.JSONDecodeError:
        state = {}
    return state

async def _set_or_yield_temperature(hwaddr: str, temperature: Temperature):
    now = datetime.datetime.now()
    await asyncio.sleep(5)
    setpoints = sorted(
        await cache.get('temperature_setpoints'),
        key=lambda x: x[0]
    )
    if setpoints[-1][0] <= now:
        res = run(
            ["./eq3.exp", hwaddr, "temp", str(temperature.setpoint)],
            capture_output=True
        )
        await cache.delete('temperature_setpoints')
    else:
        res = None

@app.get("/thermostats")
async def thermostats():
    _ = run(["hciconfig", "hci0", "reset"])
    scan_proc = Popen(
        ["hcitool", "lescan", "--discovery=g"],
        stdin=PIPE,
        stdout=PIPE
    )
    await asyncio.sleep(15)
    scan_proc.send_signal(signal.SIGINT)
    stdout, stderr = scan_proc.communicate()
    hwaddrs = [
        l.split(" ")[0]
        for l in stdout.decode("ASCII").split("\n")
        if "CC-RT-BLE" in l
    ]
    return hwaddrs

@app.get("/thermostats/{hwaddr}")
async def thermostat_state(hwaddr: str):
    if hwaddr not in thermostats_states:
        thermostats_states[hwaddr] = await _thermostat_state(hwaddr)
    return thermostats_states[hwaddr]

@app.get("/thermostats/{hwaddr}/temperature")
async def thermostat_current_temprature(hwaddr: str) -> float:
    if hwaddr not in thermostats_states:
        thermostats_states[hwaddr] = await _thermostat_state(hwaddr)
    return thermostats_states[hwaddr].get("temperature", None)

@app.post("/thermostats/{hwaddr}/temperature")
async def thermostat_set_temperature(
        hwaddr: str,
        temperature: Temperature,
        background_tasks: BackgroundTasks
    ):
    setpoints = await cache.get('temperature_setpoints', default=list())
    setpoints += [(datetime.datetime.now(), temperature.setpoint)]
    await cache.set('temperature_setpoints', setpoints, ttl=10)
    background_tasks.add_task(_set_or_yield_temperature, hwaddr, temperature)
    return setpoints
