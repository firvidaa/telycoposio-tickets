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

    python scripts/email_smoke.py --mark-all-existing-processed [--yes]
        BOOTSTRAP one-shot antes del primer arranque del worker.
        Marca TODOS los emails actuales del INBOX como ya procesados
        (con keyword TLY_PROCESSED) SIN crear tickets ni enviar emails.
        Pide confirmacion interactiva salvo que se pase --yes.

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


#: Tope generoso al listar UIDs para el bootstrap. ``list_unprocessed`` esta
#: capado por ``max_results``; le pasamos un valor lo bastante alto para
#: cubrir cualquier INBOX razonable de una cuenta nueva.
_BOOTSTRAP_LIST_LIMIT = 10_000


def _confirm(prompt: str) -> bool:
    """Pide confirmacion ``y/N`` al usuario. Default = No."""
    try:
        ans = input(prompt).strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes", "s", "si")


def _mark_all_existing_processed(client: EmailClient, *, assume_yes: bool) -> int:
    """Marca TODOS los UIDs actuales con TLY_PROCESSED. Idempotente.

    Diseñado para ejecutarse UNA VEZ antes del primer arranque del worker:
    asi los emails que ya estan en el INBOX (bienvenidas de Google, pruebas
    previas, etc.) no se convierten en tickets falsos.
    """
    uids = client.list_unprocessed(max_results=_BOOTSTRAP_LIST_LIMIT)
    if not uids:
        print("(nada que marcar: no hay emails sin TLY_PROCESSED)")
        return 0

    print(
        f"Se van a marcar {len(uids)} emails del INBOX con keyword TLY_PROCESSED."
    )
    print("NO se crearan tickets ni se enviaran emails. Los mensajes seguiran")
    print("visibles en el INBOX; solo se les anyade un flag IMAP.")
    if not assume_yes:
        if not _confirm("Continuar? [y/N]: "):
            print("Cancelado.")
            return 1

    failed: list[str] = []
    for uid in uids:
        try:
            client.mark_processed(uid)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{uid} ({type(exc).__name__})")
    ok = len(uids) - len(failed)
    print(f"OK: marcados {ok}/{len(uids)} UIDs.")
    if failed:
        print("Errores:")
        for line in failed:
            print(f"  - {line}")
        return 2
    return 0


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
    group.add_argument(
        "--mark-all-existing-processed",
        action="store_true",
        help=(
            "BOOTSTRAP: marca TODOS los emails actuales del INBOX como "
            "procesados (sin crear tickets). Pide confirmacion."
        ),
    )
    parser.add_argument(
        "--max", type=int, default=20, help="Maximo de UIDs a listar (default 20)."
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Asume 'si' en las confirmaciones interactivas (uso en scripts).",
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

    if args.mark_all_existing_processed:
        return _mark_all_existing_processed(client, assume_yes=args.yes)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
