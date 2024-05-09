from enum import Enum


class PunData:
    def __init__(self) -> None:
        self.orari = {
            Fascia.MONO: 0,
            Fascia.F1: 0,
            Fascia.F2: 0,
            Fascia.F3: 0,
            Fascia.F23: 0,
        }
        self.pun = {
            Fascia.MONO: [],
            Fascia.F1: [],
            Fascia.F2: [],
            Fascia.F3: [],
            Fascia.F23: [],
        }

    def init(self):
        self.orari = {
            Fascia.MONO: 0,
            Fascia.F1: 0,
            Fascia.F2: 0,
            Fascia.F3: 0,
            Fascia.F23: 0,
        }
        self.pun = {
            Fascia.MONO: [],
            Fascia.F1: [],
            Fascia.F2: [],
            Fascia.F3: [],
            Fascia.F23: [],
        }


class Fascia(Enum):
    MONO = "MONO"
    F1 = "F1"
    F2 = "F2"
    F3 = "F3"
    F23 = "F23"


class PunValues:
    value: dict[Fascia, float]
    value = {
        Fascia.MONO: 0.0,
        Fascia.F1: 0.0,
        Fascia.F2: 0.0,
        Fascia.F3: 0.0,
        Fascia.F23: 0.0,
    }
