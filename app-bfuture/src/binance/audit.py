"""Log de auditoría de órdenes (JSONL).

Persiste cada solicitud/respuesta de orden a un archivo de líneas JSON para tener
trazabilidad ante cualquier operación (imprescindible antes de operar en real).
El archivo vive fuera del bundle (raíz del proyecto, en logs/) y está gitignored.

El registro NUNCA debe tumbar el trading: los errores de escritura se ignoran.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class AuditLog:
    def __init__(self, path) -> None:
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def record(self, action: str, request=None, response=None, error=None) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "request": request,
            "response": response,
            "error": None if error is None else str(error),
        }
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")
        except OSError:
            pass  # nunca interrumpir el trading por el log


def read_audit(path) -> list[dict]:
    """Lee un log de auditoría (útil para tests/inspección)."""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out
