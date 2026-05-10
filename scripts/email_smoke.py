"""CLI para verificar manualmente el cliente IMAP/SMTP contra la cuenta real.

Subcomandos:

    python scripts/email_smoke.py --list
        Lista UIDs sin el keyword TLY_PROCESSED (max 20 por defecto).

    python scripts/email_smoke.py --get <UID>
        Imprime el mensaje parseado: From, Subject, Date, body_source,
        body (truncado), adjuntos.

    python scripts/email_smoke.py --send-test <to>
        Envia un mensaje de prueba al destinatario indicado.

    python scripts/email_smoke.py --mark-processed <UID>
        Aplica el keyword TLY_PROCESSED al mensaje. NO BORRA NADA, solo
        lo marca; sigue visible en el INBOX.

Logs (INFO) salen por stderr; el resultado por stdout.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from app.services.email_client import EmailClient  # noqa: E402


def _print_email(client: EmailClient, uid: str) -> int:
    msg = client.get(uid)
    print(f"uid              : {msg.uid}")
    print(f"message_id       : {msg.rfc822_message_id}")
    print(f"from             : {msg.from_name or '-'} <{msg.from_email or '-'}>")
    print(f"subject          : {msg.subject}")
    print(f"received_at      : {msg.received_at.isoformat()}")
    print(f"body_source      : {msg.body_source}")
    print(f"body_len         : {len(msg.body)}")
    print(f"attachments      : {len(msg.attachments)}")
    for a in msg.attachments:
        print(f"  - {a.name} ({a.size_bytes} bytes)")
    print("---- body (primeros 500 chars) ----")
    print(msg.body[:500])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Verifica IMAP/SMTP contra la cuenta real.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="Lista UIDs no procesados.")
    group.add_argument("--get", metavar="UID", help="Imprime el mensaje parseado.")
    group.add_argument(
        "--send-test", metavar="TO", help="Envia un mensaje de prueba al destinatario."
    )
    group.add_argument(
        "--mark-processed", metavar="UID", help="Marca el mensaje con TLY_PROCESSED."
    )
    parser.add_argument(
        "--max", type=int, default=20, help="Maximo de UIDs a listar (default 20)."
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    client = EmailClient()

    if args.list:
        uids = client.list_unprocessed(max_results=args.max)
        if not uids:
            print("(sin mensajes pendientes)")
            return 0
        for uid in uids:
            print(uid)
        return 0

    if args.get:
        return _print_email(client, args.get)

    if args.send_test:
        msg_id = client.send(
            to=args.send_test,
            subject="[SMOKE TEST] email_smoke.py",
            body=(
                "Mensaje generado por scripts/email_smoke.py.\n\n"
                "Si lo recibes es que SMTP + App Password funcionan.\n"
            ),
        )
        print(f"OK: enviado, message_id={msg_id}")
        return 0

    if args.mark_processed:
        client.mark_processed(args.mark_processed)
        print(f"OK: UID={args.mark_processed} marcado como TLY_PROCESSED.")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
