"""Clasifica un texto contra la API real de Anthropic e imprime el resultado.

Util para iterar prompts manualmente o verificar la respuesta de la IA sin
arrancar todo el flujo (poller de Gmail, BD, web).

Uso:
    python scripts/classify_text.py --subject "Asunto" --text "Cuerpo del mensaje"
    echo "Cuerpo del mensaje" | python scripts/classify_text.py --subject "Asunto"

La salida en stdout es el resultado parseado (categoria, confianza, razon).
La linea de log INFO con metricas (tokens, latencia, ...) sale por stderr.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from app.services.classifier import classify  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clasifica un texto via la API de Anthropic."
    )
    parser.add_argument("--subject", required=True, help="Asunto del mensaje.")
    parser.add_argument(
        "--text",
        default=None,
        help="Cuerpo del mensaje. Si no se pasa, se lee de stdin.",
    )
    parser.add_argument(
        "--ticket-id",
        default=None,
        help="Identificador opcional para correlacion en logs.",
    )
    args = parser.parse_args()

    # Logs por stderr para que no se mezclen con la salida del resultado.
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    body = args.text if args.text is not None else sys.stdin.read()
    if not body.strip():
        print("ERROR: cuerpo vacio (--text o stdin).", file=sys.stderr)
        return 1

    result = classify(
        subject=args.subject,
        body=body,
        ticket_id=args.ticket_id,
    )

    if result.succeeded:
        assert result.category is not None  # garantizado por succeeded
        assert result.confidence is not None
        print(f"category   : {result.category.value}")
        print(f"confidence : {result.confidence:.2f}")
        print(f"reasoning  : {result.reasoning}")
        return 0

    print("ERROR: clasificacion fallida (revisa los logs).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
