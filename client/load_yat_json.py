#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Public GitHub JSON dosyasını indirir; erişim yoksa yerel cache'i kullanır."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests


def load_yat_data(
    raw_url: str,
    cache_path: str | Path,
    timeout: int = 12,
) -> dict[str, Any]:
    cache = Path(cache_path)

    try:
        response = requests.get(
            raw_url,
            timeout=timeout,
            headers={"User-Agent": "FonPulse/1.0"},
        )
        response.raise_for_status()
        payload = response.json()

        if int(payload.get("fund_count", 0)) < 1500:
            raise ValueError("GitHub JSON fon sayısı olağan dışı düşük.")

        cache.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache.with_suffix(cache.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(cache)
        return payload

    except Exception:
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        raise
