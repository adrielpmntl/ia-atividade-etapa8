"""
Fronteira entre o veredito do juiz e a escrita.

A tool que grava não confirma sozinha. Ela deixa aqui, com o id do turno,
o que faz a escrita permanecer e o que a desfaz. No fim do turno, só o
veredito aprovado do juiz confirma; qualquer outro final desfaz.
Fora de um turno (servidor MCP, scripts) não há juiz: confirma na hora.
"""
import threading

FRASE_ESCRITA_DESFEITA = "Por isso, nada deste pedido foi registrado. Confira os dados e envie de novo."

_lock = threading.Lock()
_pendentes: dict[str, list[tuple]] = {}


def _turno(config) -> str | None:
    return ((config or {}).get("configurable") or {}).get("turno_id")


def adiar_escrita(config, confirmar, desfazer=lambda: None) -> bool:
    """Guarda a escrita até o veredito. Devolve True quando ela ficou pendente."""
    turno_id = _turno(config)
    if not turno_id:
        confirmar()
        return False
    with _lock:
        _pendentes.setdefault(turno_id, []).append((confirmar, desfazer))
    return True


def adiar_commit(config, conn) -> bool:
    """Mantém a transação do Postgres aberta até o veredito.

    Com True, a conexão passa a ser da fronteira: quem chamou não a fecha.
    """
    def confirmar():
        try:
            conn.commit()
        finally:
            conn.close()

    def desfazer():
        try:
            conn.rollback()
        finally:
            conn.close()

    if not _turno(config):
        conn.commit()
        return False
    return adiar_escrita(config, confirmar, desfazer)


def _desfazer(escritas: list[tuple]) -> None:
    for _, desfazer in escritas:
        try:
            desfazer()
        except Exception as exc:
            print(f"[escrita] erro ao desfazer {exc}")


def confirmar_escritas(turno_id: str) -> int:
    with _lock:
        escritas = _pendentes.pop(turno_id, [])
    for indice, (confirmar, _) in enumerate(escritas):
        try:
            confirmar()
        except Exception:
            _desfazer(escritas[indice:])
            raise
    if escritas:
        print(f"[escrita] {len(escritas)} confirmada(s)")
    return len(escritas)


def descartar_escritas(turno_id: str) -> int:
    with _lock:
        escritas = _pendentes.pop(turno_id, [])
    _desfazer(escritas)
    if escritas:
        print(f"[escrita] {len(escritas)} desfeita(s)")
    return len(escritas)
