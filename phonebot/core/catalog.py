"""Katalog modeli iPhone z dostępnymi pojemnościami.

Nowe modele: dopisz wpis do ``IPHONE_MODELS``. Modele o numerze wyższym niż
ostatni w katalogu (np. przyszły „iPhone 18 Pro") są i tak rozpoznawane,
tylko bez walidacji pojemności.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IPhoneModel:
    name: str
    storages: tuple[int, ...]
    year: int


_S = {
    "a": (32, 128, 256),
    "b": (64, 128, 256),
    "c": (64, 256),
    "d": (64, 256, 512),
    "e": (128, 256, 512),
    "f": (128, 256, 512, 1024),
    "g": (256, 512),
    "h": (256, 512, 1024),
    "i": (256, 512, 1024, 2048),
    "j": (16, 32, 64, 128),
}

IPHONE_MODELS: tuple[IPhoneModel, ...] = (
    IPhoneModel("iPhone 6s", _S["j"], 2015),
    IPhoneModel("iPhone 6s Plus", _S["j"], 2015),
    IPhoneModel("iPhone SE (2016)", (16, 32, 64, 128), 2016),
    IPhoneModel("iPhone 7", _S["a"], 2016),
    IPhoneModel("iPhone 7 Plus", _S["a"], 2016),
    IPhoneModel("iPhone 8", _S["b"], 2017),
    IPhoneModel("iPhone 8 Plus", _S["b"], 2017),
    IPhoneModel("iPhone X", _S["c"], 2017),
    IPhoneModel("iPhone XR", _S["b"], 2018),
    IPhoneModel("iPhone XS", _S["d"], 2018),
    IPhoneModel("iPhone XS Max", _S["d"], 2018),
    IPhoneModel("iPhone 11", _S["b"], 2019),
    IPhoneModel("iPhone 11 Pro", _S["d"], 2019),
    IPhoneModel("iPhone 11 Pro Max", _S["d"], 2019),
    IPhoneModel("iPhone SE (2020)", _S["b"], 2020),
    IPhoneModel("iPhone 12 mini", _S["b"], 2020),
    IPhoneModel("iPhone 12", _S["b"], 2020),
    IPhoneModel("iPhone 12 Pro", (128, 256, 512), 2020),
    IPhoneModel("iPhone 12 Pro Max", (128, 256, 512), 2020),
    IPhoneModel("iPhone 13 mini", _S["e"], 2021),
    IPhoneModel("iPhone 13", _S["e"], 2021),
    IPhoneModel("iPhone 13 Pro", _S["f"], 2021),
    IPhoneModel("iPhone 13 Pro Max", _S["f"], 2021),
    IPhoneModel("iPhone SE (2022)", _S["b"], 2022),
    IPhoneModel("iPhone 14", _S["e"], 2022),
    IPhoneModel("iPhone 14 Plus", _S["e"], 2022),
    IPhoneModel("iPhone 14 Pro", _S["f"], 2022),
    IPhoneModel("iPhone 14 Pro Max", _S["f"], 2022),
    IPhoneModel("iPhone 15", _S["e"], 2023),
    IPhoneModel("iPhone 15 Plus", _S["e"], 2023),
    IPhoneModel("iPhone 15 Pro", _S["f"], 2023),
    IPhoneModel("iPhone 15 Pro Max", (256, 512, 1024), 2023),
    IPhoneModel("iPhone 16", _S["e"], 2024),
    IPhoneModel("iPhone 16 Plus", _S["e"], 2024),
    IPhoneModel("iPhone 16 Pro", _S["f"], 2024),
    IPhoneModel("iPhone 16 Pro Max", (256, 512, 1024), 2024),
    IPhoneModel("iPhone 16e", _S["e"], 2025),
    IPhoneModel("iPhone 17", _S["g"], 2025),
    IPhoneModel("iPhone Air", _S["h"], 2025),
    IPhoneModel("iPhone 17 Pro", _S["h"], 2025),
    IPhoneModel("iPhone 17 Pro Max", _S["i"], 2025),
)

MODELS_BY_NAME: dict[str, IPhoneModel] = {m.name: m for m in IPHONE_MODELS}
ALL_STORAGES: tuple[int, ...] = (16, 32, 64, 128, 256, 512, 1024, 2048)
MAX_KNOWN_GENERATION = 17


def model_names() -> list[str]:
    return [m.name for m in IPHONE_MODELS]


def storages_for(model: str | None) -> tuple[int, ...]:
    if model and model in MODELS_BY_NAME:
        return MODELS_BY_NAME[model].storages
    return ALL_STORAGES


def format_storage(gb: int | None) -> str:
    if gb is None:
        return "?"
    if gb >= 1024 and gb % 1024 == 0:
        return f"{gb // 1024} TB"
    return f"{gb} GB"
