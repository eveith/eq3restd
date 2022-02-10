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
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.logger import logger
from subprocess import run, Popen, TimeoutExpired, PIPE, CalledProcessError


class Temperature(BaseModel):
    setpoint: float


BT_IF = os.environ.get("BT_IF", "hci0")
EQ3_EXP = "%s/eq3.exp" % os.path.dirname(os.path.realpath(__file__))
VALUES_MAX_AGE=int(os.environ.get("VALUES_MAX_AGE", 300))

app = FastAPI(debug=True)
cache = Cache()
refresh_task = None
thermostats_states = {}


def exponential_backoff(func, retries=3):
    @functools.wraps(func)
    async def _backoff_wrapper(*args, **kwargs):
        error = None
        result = None
        for i in range(retries):
            await asyncio.sleep(0.1 * random.randint(0, 2**i - 1))
            try:
                if inspect.iscoroutinefunction(func):
                    result = await func(*args, **kwargs)
                else:
                    result = func(*args, **kwargs)
            except Exception as e:
                logger.warning(
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

    now = datetime.datetime.now()
    cache_key = f"{hwaddr}.state"
    state = await cache.get(cache_key, (dict(), now))
    if (
        state[0]
        and state[1] + datetime.timedelta(seconds=int(VALUES_MAX_AGE)) <= now
    ):
        # We have a fresh value cached, don't query again:
        return state[0]

    try:
        res = await eq3_json(hwaddr)
        state = (json.loads(res.stdout), now)
        await cache.set(
            cache_key,
            state,  # Data + Timestamp
            ttl=VALUES_MAX_AGE
        )
    except CalledProcessError as e:
        logger.error("Could not run eq3.exp %s json: %s", hwaddr, e.stderr)
        raise e
    except json.decoder.JSONDecodeError as e:
        logger.error(
            "Could not parse JSON from eq3.exp for %s: %s",
            hwaddr,
            e.stderr
        )
        raise e
    return state


async def _set_or_yield_temperature(hwaddr: str, temperature: Temperature):
    cache_key = f"{hwaddr}.temperature_setpoints"
    now = datetime.datetime.now()
    await asyncio.sleep(5)
    setpoints = await cache.get(cache_key) or []
    setpoints = sorted(setpoints, key=lambda x: x[0])
    
    # If in the meantime another setpoint was issued, or there simply
    # is nothing to see, sielently yield:
    if not setpoints and setpoints[-1][0] > now:
        return

    _reset_hci()
    run(
        [EQ3_EXP, BT_IF, hwaddr, "temp", str(temperature.setpoint)]
    )
    await cache.delete(cache_key)
    await cache.delete(f"{hwaddr}.state")


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
    return await _thermostat_state(hwaddr)


@app.get("/thermostats/{hwaddr}/temperature")
async def thermostat_current_temprature(hwaddr: str) -> float:
    try:
        state = await _thermostat_state(hwaddr)
        return state[0].get("temperature", None)
    except Exception as e:
        import traceback
        raise HTTPException(
            status_code=500,
            detail="".join(
                traceback.format_exception(
                    etype=type(e), value=e, tb=e.__traceback__
                )
            )
        )


@app.post("/thermostats/{hwaddr}/temperature")
async def thermostat_set_temperature(
    hwaddr: str,
    temperature: Temperature,
    background_tasks: BackgroundTasks
):
    cache_key = f"{hwaddr}.temperature_setpoints"
    setpoints = await cache.get(cache_key, default=list())
    setpoints += [(datetime.datetime.now(), temperature.setpoint)]
    await cache.set(cache_key, setpoints, ttl=10)
    background_tasks.add_task(_set_or_yield_temperature, hwaddr, temperature)
    return setpoints

@app.exception_handler(Exception)
async def debug_exception_handler(request: Request, exc: Exception):
    import traceback

    return Response(
	content="".join(
	    traceback.format_exception(
		etype=type(exc), value=exc, tb=exc.__traceback__
	    )
	)
    )

