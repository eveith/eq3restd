# eq3restd -- A REST API for eQ-3 bluetooth thermostats

## About

eq3restd implements a simple, FastAPI-based REST frontend for
bluetooth-controlled eQ-3 thermostats of the company ELV. Under the hood, it
uses the shell script from
https://github.com/Heckie75/eQ-3-radiator-thermostat.git.

## Installation

### Prerequisites

The eQ-3 shell script needs:

- expect
- gattool
- hciconfig

In addition, all requirements of this project are in the
ˋrequirements.txtˋfile.

### Installing

There is no real installation procedure yet. Set up a virtual env and place
ˋeq3restd.pyˋthere. Modify the ˋeq3restd@.serviceˋfile to reflect the path.
Then, the service file will reference your bluetooth interface. E.g.,

    systemctl enable eq3restd@hci0.service

## API

Since eq3restd uses [FastAPI](https://fastapi.tiangolo.com/), one can easily
access an API overview at the root controller level (e.g.,
http://localhost:8000/).

## License

MIT, see the file LICENSE.txt.
