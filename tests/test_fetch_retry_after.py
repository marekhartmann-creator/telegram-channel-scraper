# -*- coding: utf-8 -*-
"""A3-V6: Retry-After sa cita a respektuje.

Predtym: backoff `min(8.0, 2**attempt) + random.random()` mal nahodnu zlozku, ale
hlavicku ignoroval. Pri 429 s "Retry-After: 60" sme vycerpali vsetky pokusy za
~20 s vnutri okna, ktore server zavrel - a beh skoncil ako zlyhanie.
"""
from __future__ import annotations

import asyncio

import pytest

from tg.fetch import RETRY_AFTER_MAX_S, TelegramClient, _retry_after_seconds


class FalosnaOdpoved:
    def __init__(self, status_code: int, headers: dict | None = None, text: str = "ok"):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


def _klient(odpovede, spanky):
    klient = TelegramClient()
    rad = list(odpovede)

    async def falosny_get(url):
        return rad.pop(0)

    async def falosny_sleep(s):
        spanky.append(s)

    klient._client.get = falosny_get  # type: ignore[assignment]
    asyncio.sleep_povodny = asyncio.sleep
    return klient, falosny_sleep


def test_retry_after_v_sekundach():
    assert _retry_after_seconds({"retry-after": "60"}) == 60.0
    assert _retry_after_seconds({"retry-after": " 5 "}) == 5.0
    assert _retry_after_seconds({}) is None
    assert _retry_after_seconds({"retry-after": "nezmysel"}) is None


def test_backoff_nikdy_neskraca_retry_after():
    klient = TelegramClient()
    for pokus in range(1, 4):
        cakanie = klient._backoff(pokus, 60.0)
        assert cakanie >= 60.0, "Retry-After je pokyn, nie navrh"
        assert cakanie <= RETRY_AFTER_MAX_S


def test_backoff_bez_hlavicky_ma_nahodnu_zlozku():
    klient = TelegramClient()
    hodnoty = {klient._backoff(1, None) for _ in range(20)}
    assert len(hodnoty) > 1, "bez jitteru by sa suborne behy zobudili naraz"


def test_429_pocka_tolko_kolko_server_povedal(monkeypatch):
    spanky: list[float] = []
    klient, falosny_sleep = _klient(
        [
            FalosnaOdpoved(429, {"retry-after": "60"}),
            FalosnaOdpoved(200, {}, "hotovo"),
        ],
        spanky,
    )
    monkeypatch.setattr("tg.fetch.asyncio.sleep", falosny_sleep)
    vysledok = asyncio.run(klient.get("https://t.me/s/x"))
    assert vysledok.status == 200
    assert vysledok.html == "hotovo"
    assert spanky, "medzi pokusmi sa malo cakat"
    assert spanky[0] >= 60.0, f"cakal len {spanky[0]} s, server pytal 60 s"


def test_bez_hlavicky_ostava_povodny_odstup(monkeypatch):
    spanky: list[float] = []
    klient, falosny_sleep = _klient(
        [FalosnaOdpoved(503), FalosnaOdpoved(200)],
        spanky,
    )
    monkeypatch.setattr("tg.fetch.asyncio.sleep", falosny_sleep)
    vysledok = asyncio.run(klient.get("https://t.me/s/x"))
    assert vysledok.status == 200
    assert 2.0 <= spanky[0] <= 9.0
