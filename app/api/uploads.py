"""Lectura de subidas con tope de tamaño, sin cargar el archivo entero en memoria.

Leer la subida completa antes de validar el tamaño permitiría a un usuario
autenticado tumbar el proceso con un archivo enorme, así que se lee por bloques y
se corta con `413` en cuanto se pasa del máximo.
"""

from __future__ import annotations

from fastapi import UploadFile

from app.core.errors import FileTooLargeError

BLOQUE_LECTURA_BYTES = 64 * 1024


async def leer_con_limite(archivo: UploadFile, maximo: int) -> bytes:
    """Devuelve el contenido de la subida o lanza `FileTooLargeError` (413)."""
    declarado = archivo.size
    if declarado is not None and declarado > maximo:
        raise FileTooLargeError(
            details={"maximo_bytes": maximo, "recibido_bytes": declarado}
        )

    bloques: list[bytes] = []
    total = 0
    while True:
        bloque = await archivo.read(BLOQUE_LECTURA_BYTES)
        if not bloque:
            break
        total += len(bloque)
        if total > maximo:
            raise FileTooLargeError(
                details={"maximo_bytes": maximo, "recibido_bytes": total}
            )
        bloques.append(bloque)
    return b"".join(bloques)
