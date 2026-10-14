"""Ejecución de código bloqueante fuera del event loop.

`zeep` y `httpx` síncronos (el transporte de SUNAT) son bloqueantes: si se llaman
directamente desde una corrutina, congelan el worker entero durante el timeout
de la petición (`SUNAT_TIMEOUT_SECONDS`, 20 s por defecto, más reintentos). Con
`run_blocking` la llamada se ejecuta en el pool de hilos y el evento sigue
atiendiendo al resto de peticiones.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

import anyio


async def run_blocking[**P, T](
    func: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
) -> T:
    """Invoca `func(*args, **kwargs)` en un hilo y espera el resultado.

    Equivale a `anyio.to_thread.run_sync` con los argumentos ya enlazados, para
    poder usarlo igual que una llamada normal: `await run_blocking(cliente.metodo, x=1)`.
    """
    return await anyio.to_thread.run_sync(partial(func, *args, **kwargs))
