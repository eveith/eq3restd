#!/usr/bin/env python3

import os
import json
import random
import signal
import inspect
import asyncio
import logging
import datetime
import functools
import json.decoder

from aiocache import Cache
from pydantic import BaseModel
from fastapi import FastAPI, BackgroundTasks
from fastapi.logger import logger
from subprocess import run, Popen, TimeoutExpired, PIPE, CalledProcessError


class Temperature(BaseModel):
    setpoint: float


BT_IF = "hci1"
EQ3_EXP = "%s/eq3.exp" % os.path.dirname(os.path.realpath(__file__))

app = FastAPI()
cache = Cache()
refresh_task = None
thermostats_states = {}
log = logging.getLogger('eq3restd')


def exponential_backoff(func, retries=5):
    @functools.wraps(func)
    async def _backoff_wrapper(*args, **kwargs):
        error = None
        result = None
        for i in range(retries):
            await asyncio.sleep(random.randint(0, 2**i - 1))
            try:
                if inspect.iscoroutinefunction(func):
                    result = await func(*args, **kwargs)
                else:
                    result = func(*args, **kwargs)
            except Exception as e:
                log.warning(
                    "Error executing %s: %s (%d/%d)",
                    func,
                    e,
                    i,
                    retries
                ) 
                error = e
        if error:
            raise error
        return result
    return _backoff_wrapper


@app.on_event("startup")
def initrand():
    random.seed(None)


@app.on_event("startup")
async def start_background_refresh():
    refresh_task = asyncio.create_task(_refresh_known_thermostats())


@app.on_event("shutdown")
async def stop_background_query():
    if refresh_task:
        refresh_task.cancel()


def _reset_hci():
    _ = run(["hciconfig", BT_IF, "reset"])


async def _refresh_known_thermostats():
    while True:
        for hwaddr in thermostats_states.keys():
            new_state = await _thermostat_state(hwaddr)
            if new_state and "temperature" in new_state:
                thermostats_states[hwaddr] = new_state
        await asyncio.sleep(120)


async def _thermostat_state(hwaddr: str):
    @exponential_backoff
    def eq3_json(hwaddr: str):
        _reset_hci()
        return run([EQ3_EXP, BT_IF, hwaddr, "json"], capture_output=True, check=True)

    try:
        res = await eq3_json(hwaddr)
        state = json.loads(res.stdout)
    except CalledProcessError as e:
        state = {}
        log.warning("Could not run eq3.exp %s json: %s", hwaddr, e.stderr)
    except json.decoder.JSONDecodeError:
        state = {}
    return state


async def _set_or_yield_temperature(hwaddr: str, temperature: Temperature):
    now = datetime.datetime.now()
    await asyncio.sleep(5)
    setpoints = await cache.get('temperature_setpoints') or []
    setpoints = sorted(setpoints, key=lambda x: x[0])
    if setpoints and setpoints[-1][0] <= now:
        _reset_hci()
        res = run(
            [EQ3_EXP, BT_IF, hwaddr, "temp", str(temperature.setpoint)],
            capture_output=True
        )
        await cache.delete('temperature_setpoints')
    else:
        res = None


@app.get("/thermostats")
async def thermostats():
    _reset_hci()
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
