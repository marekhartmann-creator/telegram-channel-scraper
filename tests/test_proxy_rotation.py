# -*- coding: utf-8 -*-
"""A6-V7 (R-F74AC6): proxy sa musi obnovit, nie sediet na jednej adrese.

Diferencialny dokaz: ten isty scenar (zlyhane spojenie) s dodavatelom proxy a
bez neho. S dodavatelom sa vypyta NOVA adresa a druhy pokus ide inou cestou;
bez neho ostava spravanie povodne (nic nepadne, len sa nerotuje).
"""
from __future__ import annotations

import asyncio

from tg.fetch import TelegramClient


async def _bez_cakania(s):
    """Testy nemaju cakat realny backoff."""
    return None


class FalosnaOdpoved:
    def __init__(self, status_code=200, headers=None, text="ok"):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


class FalosnyKlient:
    """Nahrada httpx.AsyncClient - pamata si, ktorou proxy bol vytvoreny."""

    def __init__(self, proxy, plan, pouzite):
        self.proxy = proxy
        self._plan = plan
        self._pouzite = pouzite

    async def get(self, url):
        self._pouzite.append(self.proxy)
        krok = self._plan.pop(0)
        if isinstance(krok, Exception):
            raise krok
        return krok

    async def aclose(self):
        return None


def _priprav(plan, adresy, rotate_after=40):
    """Klient s podstrcenou tovarnou na HTTP klienta a dodavatelom proxy."""
    pouzite: list[str | None] = []
    vyziadania: list[int] = []

    class Klient(TelegramClient):
        def _novy_http_klient(self, proxy):
            return FalosnyKlient(proxy, plan, pouzite)

    async def dodavatel():
        vyziadania.append(1)
        return adresy.pop(0) if adresy else None

    k = Klient(
        proxy_url="http://proxy-A:8000",
        proxy_provider=dodavatel,
        rotate_after=rotate_after,
    )
    return k, pouzite, vyziadania


def test_pri_chybe_spojenia_sa_vypyta_nova_proxy(monkeypatch):
    monkeypatch.setattr("tg.fetch.asyncio.sleep", _bez_cakania)
    k, pouzite, vyziadania = _priprav(
        [ConnectionError("connection reset"), FalosnaOdpoved(200, {}, "hotovo")],
        ["http://proxy-B:8000"],
    )
    vysledok = asyncio.run(k.get("https://t.me/s/x"))
    assert vysledok.status == 200
    assert len(vyziadania) == 1, "po chybe spojenia sa nevypytala nova proxy"
    assert k.stats.proxy_rotations == 1
    assert k.proxy_url == "http://proxy-B:8000"
    assert pouzite == ["http://proxy-A:8000", "http://proxy-B:8000"], (
        "druhy pokus isiel tou istou adresou - proxy nerotuje"
    )


def test_bez_dodavatela_ostava_povodne_spravanie(monkeypatch):
    """Diferencialna protivzorka: ta ista chyba, ziadny dodavatel."""
    monkeypatch.setattr("tg.fetch.asyncio.sleep", _bez_cakania)
    pouzite: list[str | None] = []
    plan = [ConnectionError("connection reset"), FalosnaOdpoved(200, {}, "hotovo")]

    class Klient(TelegramClient):
        def _novy_http_klient(self, proxy):
            return FalosnyKlient(proxy, plan, pouzite)

    k = Klient(proxy_url="http://proxy-A:8000")
    vysledok = asyncio.run(k.get("https://t.me/s/x"))
    assert vysledok.status == 200
    assert k.stats.proxy_rotations == 0
    assert pouzite == ["http://proxy-A:8000", "http://proxy-A:8000"]


def test_rotacia_po_n_poziadavkach(monkeypatch):
    monkeypatch.setattr("tg.fetch.asyncio.sleep", _bez_cakania)
    k, pouzite, vyziadania = _priprav(
        [FalosnaOdpoved(200), FalosnaOdpoved(200), FalosnaOdpoved(200)],
        ["http://proxy-B:8000", "http://proxy-C:8000"],
        rotate_after=2,
    )
    for _ in range(3):
        asyncio.run(k.get("https://t.me/s/x"))
    assert k.stats.proxy_rotations == 1, "po N poziadavkach sa proxy neobnovila"
    assert pouzite == [
        "http://proxy-A:8000",
        "http://proxy-A:8000",
        "http://proxy-B:8000",
    ]


def test_zlyhany_dodavatel_nezabije_beh(monkeypatch):
    monkeypatch.setattr("tg.fetch.asyncio.sleep", _bez_cakania)
    pouzite: list[str | None] = []
    plan = [ConnectionError("reset"), FalosnaOdpoved(200)]

    class Klient(TelegramClient):
        def _novy_http_klient(self, proxy):
            return FalosnyKlient(proxy, plan, pouzite)

    async def zlyhavajuci():
        raise RuntimeError("proxy service down")

    k = Klient(proxy_url="http://proxy-A:8000", proxy_provider=zlyhavajuci)
    vysledok = asyncio.run(k.get("https://t.me/s/x"))
    assert vysledok.status == 200
    assert k.stats.proxy_rotations == 0
    assert any("proxy rotation failed" in x for x in k.stats.failures)
