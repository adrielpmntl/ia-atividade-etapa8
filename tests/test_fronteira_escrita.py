"""
Testes da fronteira entre o veredito do juiz e a escrita.

As duas primeiras classes não usam banco nem LLM: testam a fronteira sozinha
e o veredito do juiz com o HTTP da TypeSafe simulado.

As duas últimas usam o Postgres e o MongoDB de verdade, sem LLM. Cada teste
chama a tool de escrita como o agente chama (com o id do turno no config) e
decide o veredito pela fronteira. Os testes de turno passam por
executar_fluxo_assessor com um grafo de mentira que chama a tool real: assim
o veredito é controlado e o caminho até o banco é o de produção. Sem os
bancos, essas duas classes são puladas.

Os registros usam um marcador único e são apagados no fim. Isso é a limpeza
do teste, não faz parte da solução.

Uso, a partir da raiz do projeto:
    python -m unittest discover -s tests -v
"""

import json
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import app.graph
from app import escritas_pendentes as fronteira
from app import juiz
from app.escritas_pendentes import FRASE_ESCRITA_DESFEITA, confirmar_escritas, descartar_escritas
from app.memory import get_db
from app.observabilidade import listar_turnos
from app.tools.agenda import atualizar_evento, cancelar_evento, criar_evento
from app.tools.db import get_conn
from app.tools.financeiro import add_transaction, update_transaction

USER_ID = "teste_etapa8_unittest"
MARCADOR = f"T8-{uuid.uuid4().hex[:8]}"


def _config(turno_id: str | None = None) -> dict:
    configuravel = {"user_id": USER_ID}
    if turno_id:
        configuravel["turno_id"] = turno_id
    return {"configurable": configuravel}


# ---------------------------------------------------------------------------
# Fronteira e veredito do juiz, sem banco e sem LLM
# ---------------------------------------------------------------------------

class _Escrita:
    def __init__(self):
        self.confirmada = 0
        self.desfeita = 0

    def confirmar(self):
        self.confirmada += 1

    def desfazer(self):
        self.desfeita += 1


class _ConexaoFalsa:
    def __init__(self):
        self.chamadas = []

    def commit(self):
        self.chamadas.append("commit")

    def rollback(self):
        self.chamadas.append("rollback")

    def close(self):
        self.chamadas.append("close")


class TestFronteiraDeEscrita(unittest.TestCase):
    def setUp(self):
        self.turno = uuid.uuid4().hex

    def test_sem_turno_confirma_na_hora(self):
        escrita = _Escrita()
        pendente = fronteira.adiar_escrita(_config(None), escrita.confirmar, escrita.desfazer)
        self.assertFalse(pendente)
        self.assertEqual((escrita.confirmada, escrita.desfeita), (1, 0))

    def test_com_turno_espera_o_veredito(self):
        escrita = _Escrita()
        pendente = fronteira.adiar_escrita(_config(self.turno), escrita.confirmar, escrita.desfazer)
        self.assertTrue(pendente)
        self.assertEqual((escrita.confirmada, escrita.desfeita), (0, 0))

    def test_aprovado_confirma_uma_unica_vez(self):
        escrita = _Escrita()
        fronteira.adiar_escrita(_config(self.turno), escrita.confirmar, escrita.desfazer)
        self.assertEqual(fronteira.confirmar_escritas(self.turno), 1)
        self.assertEqual(fronteira.confirmar_escritas(self.turno), 0)
        self.assertEqual(fronteira.descartar_escritas(self.turno), 0)
        self.assertEqual((escrita.confirmada, escrita.desfeita), (1, 0))

    def test_recusado_desfaz_e_nao_confirma(self):
        escrita = _Escrita()
        fronteira.adiar_escrita(_config(self.turno), escrita.confirmar, escrita.desfazer)
        self.assertEqual(fronteira.descartar_escritas(self.turno), 1)
        self.assertEqual(fronteira.confirmar_escritas(self.turno), 0)
        self.assertEqual((escrita.confirmada, escrita.desfeita), (0, 1))

    def test_turnos_nao_se_misturam(self):
        outro_turno = uuid.uuid4().hex
        recusada, aprovada = _Escrita(), _Escrita()
        fronteira.adiar_escrita(_config(self.turno), recusada.confirmar, recusada.desfazer)
        fronteira.adiar_escrita(_config(outro_turno), aprovada.confirmar, aprovada.desfazer)
        fronteira.descartar_escritas(self.turno)
        fronteira.confirmar_escritas(outro_turno)
        self.assertEqual((recusada.confirmada, recusada.desfeita), (0, 1))
        self.assertEqual((aprovada.confirmada, aprovada.desfeita), (1, 0))

    def test_falha_ao_confirmar_desfaz_o_resto_e_propaga(self):
        primeira, terceira = _Escrita(), _Escrita()

        def falhar():
            raise RuntimeError("conexão caiu")

        fronteira.adiar_escrita(_config(self.turno), primeira.confirmar, primeira.desfazer)
        fronteira.adiar_escrita(_config(self.turno), falhar)
        fronteira.adiar_escrita(_config(self.turno), terceira.confirmar, terceira.desfazer)
        with self.assertRaises(RuntimeError):
            fronteira.confirmar_escritas(self.turno)
        self.assertEqual((primeira.confirmada, primeira.desfeita), (1, 0))
        self.assertEqual((terceira.confirmada, terceira.desfeita), (0, 1))
        self.assertEqual(fronteira.descartar_escritas(self.turno), 0)

    def test_postgres_sem_turno_commita_na_hora(self):
        conn = _ConexaoFalsa()
        self.assertFalse(fronteira.adiar_commit(_config(None), conn))
        self.assertEqual(conn.chamadas, ["commit"])

    def test_postgres_aprovado_commita_e_fecha(self):
        conn = _ConexaoFalsa()
        self.assertTrue(fronteira.adiar_commit(_config(self.turno), conn))
        self.assertEqual(conn.chamadas, [])
        fronteira.confirmar_escritas(self.turno)
        self.assertEqual(conn.chamadas, ["commit", "close"])

    def test_postgres_recusado_faz_rollback_e_fecha(self):
        conn = _ConexaoFalsa()
        fronteira.adiar_commit(_config(self.turno), conn)
        fronteira.descartar_escritas(self.turno)
        self.assertEqual(conn.chamadas, ["rollback", "close"])


def _estado_de_escrita() -> dict:
    return {"messages": [
        HumanMessage(content="Registre 37,90 reais no almoço."),
        AIMessage(content="", tool_calls=[{"name": "add_transaction", "args": {"amount": 37.9}, "id": "1"}]),
        ToolMessage(content='{"status": "ok", "id": 43}', name="add_transaction", tool_call_id="1"),
        AIMessage(content="Lancei R$ 37,90 em comida."),
    ]}


def _resposta_do_jev(choice: str, noul: float):
    resposta = mock.MagicMock()
    resposta.read.return_value = json.dumps(
        {"answers": {"veredito": {"choice": choice}, "apoiada": {"noul": noul}}}
    ).encode()
    resposta.__enter__.return_value = resposta
    return resposta


class TestVereditoDoJuiz(unittest.TestCase):
    """O juiz só autoriza a escrita na resposta apoiada acima do limiar."""

    def _julgar(self, **patches) -> dict:
        with mock.patch.object(juiz, "TYPESAFE_API_KEY", patches.pop("chave", "chave-de-teste")), \
             mock.patch.object(juiz.urllib.request, "urlopen", **patches):
            return juiz.no_juiz(_estado_de_escrita())

    def test_apoiada_autoriza(self):
        saida = self._julgar(return_value=_resposta_do_jev("apoiada", 0.89))
        self.assertTrue(saida["escrita_autorizada"])
        self.assertNotIn("messages", saida)

    def test_sem_apoio_nao_autoriza(self):
        saida = self._julgar(return_value=_resposta_do_jev("sem_apoio", 0.04))
        self.assertFalse(saida["escrita_autorizada"])
        self.assertEqual(saida["messages"][0]["content"], juiz.FRASE_SEGURA)

    def test_apoiada_abaixo_do_limiar_nao_autoriza(self):
        saida = self._julgar(return_value=_resposta_do_jev("apoiada", 0.69))
        self.assertFalse(saida["escrita_autorizada"])

    def test_juiz_sem_chave_nao_autoriza(self):
        saida = self._julgar(chave=None, side_effect=AssertionError("não deveria chamar"))
        self.assertFalse(saida["escrita_autorizada"])
        self.assertEqual(saida["agentes_chamados"], ["juiz:sem_chave"])

    def test_juiz_sem_resposta_nao_autoriza(self):
        saida = self._julgar(side_effect=TimeoutError("timed out"))
        self.assertFalse(saida["escrita_autorizada"])
        self.assertEqual(saida["agentes_chamados"], ["juiz:erro"])

    def test_juiz_ve_o_retorno_da_tool(self):
        with mock.patch.object(juiz, "_julgar_jev", return_value={}) as julgar:
            juiz.no_juiz(_estado_de_escrita())
        evidencia = julgar.call_args.args[2]
        self.assertIn("[add_transaction]", evidencia)
        self.assertIn('"id": 43', evidencia)


# ---------------------------------------------------------------------------
# Tools de escrita e turno completo, com o Postgres e o MongoDB de verdade
# ---------------------------------------------------------------------------

def _sql(consulta: str, parametros: tuple) -> list[tuple]:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(consulta, parametros)
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()


def _transacoes(marcador: str) -> list[tuple]:
    return _sql(
        "SELECT id, amount FROM transactions WHERE description = %s;",
        (marcador,),
    )


def _valor(transacao_id: int) -> float:
    return float(_sql("SELECT amount FROM transactions WHERE id = %s;", (transacao_id,))[0][0])


def _eventos(**filtro) -> list[dict]:
    return list(get_db()["eventos"].find({"user_id": USER_ID, **filtro}))


def _nova_transacao(marcador: str) -> dict:
    return {
        "amount": 12.34,
        "source_text": f"teste {marcador}",
        "type_name": "EXPENSES",
        "category_name": "comida",
        "description": marcador,
    }


def _novo_evento(marcador: str, dia: str) -> dict:
    return {"titulo": marcador, "inicio": f"{dia}T09:00:00-03:00", "fim": f"{dia}T10:00:00-03:00"}


class _BancoReal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            _sql("SELECT 1;", ())
            get_db().command("ping")
        except Exception as exc:
            raise unittest.SkipTest(f"Postgres ou MongoDB indisponível: {exc}")

    @classmethod
    def tearDownClass(cls):
        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute("DELETE FROM transactions WHERE description LIKE %s;", (f"{MARCADOR}%",))
            conn.commit()
        finally:
            cur.close()
            conn.close()
        get_db()["eventos"].delete_many({"user_id": USER_ID})

    def setUp(self):
        self.turno = uuid.uuid4().hex
        self.marcador = f"{MARCADOR}-{self._testMethodName}"


class TestToolsDeEscrita(_BancoReal):
    def test_add_transaction_recusado_nao_permanece(self):
        resultado = add_transaction.invoke(_nova_transacao(self.marcador), config=_config(self.turno))
        self.assertEqual(resultado["status"], "ok")
        self.assertEqual(_transacoes(self.marcador), [], "antes do veredito nada é visível")
        descartar_escritas(self.turno)
        self.assertEqual(_transacoes(self.marcador), [])

    def test_add_transaction_aprovado_permanece_uma_vez(self):
        resultado = add_transaction.invoke(_nova_transacao(self.marcador), config=_config(self.turno))
        confirmar_escritas(self.turno)
        confirmar_escritas(self.turno)
        linhas = _transacoes(self.marcador)
        self.assertEqual(len(linhas), 1)
        self.assertEqual(linhas[0][0], resultado["id"])

    def test_add_transaction_fora_de_turno_confirma_na_hora(self):
        add_transaction.invoke(_nova_transacao(self.marcador))
        self.assertEqual(len(_transacoes(self.marcador)), 1)

    def test_update_transaction_recusado_nao_permanece(self):
        transacao_id = add_transaction.invoke(_nova_transacao(self.marcador))["id"]
        resultado = update_transaction.invoke({"id": transacao_id, "amount": 99.9}, config=_config(self.turno))
        self.assertEqual(resultado["updated"]["amount"], 99.9)
        self.assertEqual(_valor(transacao_id), 12.34, "antes do veredito o valor antigo continua")
        descartar_escritas(self.turno)
        self.assertEqual(_valor(transacao_id), 12.34)

    def test_update_transaction_aprovado_permanece(self):
        transacao_id = add_transaction.invoke(_nova_transacao(self.marcador))["id"]
        update_transaction.invoke({"id": transacao_id, "amount": 99.9}, config=_config(self.turno))
        confirmar_escritas(self.turno)
        self.assertEqual(_valor(transacao_id), 99.9)
        self.assertEqual(len(_transacoes(self.marcador)), 1)

    def test_segundo_update_na_mesma_linha_nao_trava_o_turno(self):
        transacao_id = add_transaction.invoke(_nova_transacao(self.marcador))["id"]
        update_transaction.invoke({"id": transacao_id, "amount": 50.0}, config=_config(self.turno))
        segundo = update_transaction.invoke({"id": transacao_id, "amount": 70.0}, config=_config(self.turno))
        self.assertEqual(segundo["status"], "error")
        confirmar_escritas(self.turno)
        self.assertEqual(_valor(transacao_id), 50.0)

    def test_criar_evento_recusado_nao_permanece(self):
        resultado = criar_evento.invoke(_novo_evento(self.marcador, "2026-12-01"), config=_config(self.turno))
        self.assertEqual(resultado["status"], "ok")
        self.assertEqual(_eventos(titulo=self.marcador), [])
        descartar_escritas(self.turno)
        self.assertEqual(_eventos(titulo=self.marcador), [])

    def test_criar_evento_aprovado_permanece_uma_vez(self):
        resultado = criar_evento.invoke(_novo_evento(self.marcador, "2026-12-02"), config=_config(self.turno))
        confirmar_escritas(self.turno)
        confirmar_escritas(self.turno)
        eventos = _eventos(titulo=self.marcador)
        self.assertEqual(len(eventos), 1)
        self.assertEqual(eventos[0]["_id"], resultado["evento"]["_id"])

    def test_atualizar_evento_recusado_nao_permanece(self):
        evento_id = criar_evento.invoke(_novo_evento(self.marcador, "2026-12-03"), config=_config())["evento"]["_id"]
        resultado = atualizar_evento.invoke({"evento_id": evento_id, "local": "Sala 7"}, config=_config(self.turno))
        self.assertEqual(resultado["evento"]["local"], "Sala 7")
        descartar_escritas(self.turno)
        self.assertIsNone(_eventos(_id=evento_id)[0]["local"])

    def test_atualizar_evento_aprovado_permanece(self):
        evento_id = criar_evento.invoke(_novo_evento(self.marcador, "2026-12-04"), config=_config())["evento"]["_id"]
        atualizar_evento.invoke({"evento_id": evento_id, "local": "Sala 7"}, config=_config(self.turno))
        self.assertIsNone(_eventos(_id=evento_id)[0]["local"])
        confirmar_escritas(self.turno)
        self.assertEqual(_eventos(_id=evento_id)[0]["local"], "Sala 7")

    def test_cancelar_evento_recusado_nao_permanece(self):
        evento_id = criar_evento.invoke(_novo_evento(self.marcador, "2026-12-05"), config=_config())["evento"]["_id"]
        cancelar_evento.invoke({"evento_id": evento_id}, config=_config(self.turno))
        descartar_escritas(self.turno)
        self.assertEqual(_eventos(_id=evento_id)[0]["status"], "ativo")

    def test_cancelar_evento_aprovado_permanece(self):
        evento_id = criar_evento.invoke(_novo_evento(self.marcador, "2026-12-06"), config=_config())["evento"]["_id"]
        cancelar_evento.invoke({"evento_id": evento_id}, config=_config(self.turno))
        self.assertEqual(_eventos(_id=evento_id)[0]["status"], "ativo")
        confirmar_escritas(self.turno)
        self.assertEqual(_eventos(_id=evento_id)[0]["status"], "cancelado")

    def test_evento_inexistente_nao_deixa_pendencia(self):
        resultado = cancelar_evento.invoke({"evento_id": "nao-existe"}, config=_config(self.turno))
        self.assertEqual(resultado["status"], "error")
        self.assertEqual(descartar_escritas(self.turno), 0)


class _GrafoFalso:
    """Faz o papel do grafo: chama a tool real e devolve o veredito pedido."""

    def __init__(self, marcador: str, escrita_autorizada: bool, falhar: bool = False):
        self.marcador = marcador
        self.escrita_autorizada = escrita_autorizada
        self.falhar = falhar

    def invoke(self, estado, config):
        add_transaction.invoke(_nova_transacao(self.marcador), config=config)
        if self.falhar:
            raise RuntimeError("nó caiu depois da tool")
        rotulo = "juiz:apoiada noul=0.90" if self.escrita_autorizada else "juiz:sem_apoio noul=0.10"
        return {
            "messages": [AIMessage(content="Registrei sua despesa.")],
            "agentes_chamados": ["roteador", "financeiro", "orquestrador", rotulo],
            "escrita_autorizada": self.escrita_autorizada,
            "rota": "financeiro",
        }


class TestTurnoCompleto(_BancoReal):
    def _turno(self, grafo: _GrafoFalso):
        with mock.patch.object(app.graph, "fluxo_agentes", grafo), \
             mock.patch.object(app.graph, "salvar_mensagem"):
            return app.graph.executar_fluxo_assessor("teste", f"etapa8-{self.marcador}", USER_ID)

    def test_turno_recusado_nao_grava_e_avisa(self):
        resposta, _ = self._turno(_GrafoFalso(self.marcador, escrita_autorizada=False))
        self.assertEqual(_transacoes(self.marcador), [])
        self.assertTrue(resposta.endswith(FRASE_ESCRITA_DESFEITA))
        self.assertEqual(listar_turnos()[-1]["status"], "ok")

    def test_turno_aprovado_grava_uma_vez(self):
        resposta, _ = self._turno(_GrafoFalso(self.marcador, escrita_autorizada=True))
        self.assertEqual(len(_transacoes(self.marcador)), 1)
        self.assertEqual(resposta, "Registrei sua despesa.")

    def test_excecao_no_turno_nao_grava(self):
        with self.assertRaises(RuntimeError):
            self._turno(_GrafoFalso(self.marcador, escrita_autorizada=True, falhar=True))
        self.assertEqual(_transacoes(self.marcador), [])
        self.assertEqual(listar_turnos()[-1]["status"], "erro")


if __name__ == "__main__":
    unittest.main()
