"""Cliente mínimo del LLM local (Ollama en el Mac) para narrar el Radar.

Ollama expone API en http://localhost:11434. `coffex:latest` es qwen2 (sin
"thinking"), rápido para narración. El motor NO depende de esto: si Ollama no
responde, `narrate` devuelve None y la UI cae a la razón por plantilla.
"""
from __future__ import annotations

import aiohttp

OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "coffex:latest"
SYSTEM = ("Eres un asistente de trading que explica setups de futuros con base en "
          "zonas de soporte/resistencia, R:R y flujo (OI, funding, taker). Responde "
          "breve, concreto y en español. NUNCA prometas ganancias ni des certezas; "
          "la decisión es del trader. REGLA CRÍTICA: usa SOLO los números que te "
          "entrega el motor en el contexto; JAMÁS inventes precios, entradas, stops "
          "ni objetivos. Si no hay datos del motor para un símbolo, dilo claramente "
          "y sugiere usar el botón Analizar; nunca te inventes cifras.")


async def available(timeout: float = 3.0) -> bool:
    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.get(f"{OLLAMA_URL}/api/tags") as r:
                return r.status == 200
    except Exception:
        return False


async def narrate(prompt: str, *, model: str = DEFAULT_MODEL,
                  system: str = SYSTEM, num_predict: int = 220,
                  timeout: float = 40.0) -> str | None:
    """Genera texto con Ollama. Devuelve None si el servidor no responde."""
    body = {
        "model": model, "prompt": prompt, "system": system, "stream": False,
        "keep_alive": "10m",  # mantiene el modelo caliente (evita el cold-start de 6s)
        "options": {"num_predict": num_predict, "temperature": 0.4},
    }
    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.post(f"{OLLAMA_URL}/api/generate", json=body) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                return (data.get("response") or "").strip() or None
    except Exception:
        return None
