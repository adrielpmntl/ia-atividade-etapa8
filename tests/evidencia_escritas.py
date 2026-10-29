"""
Evidência da Etapa 8: cada tool de escrita passa pelo chat de verdade.

Para cada tool que grava, o roteiro usa um marcador único, anota o estado do
banco antes, manda a mensagem pelo mesmo caminho da rota /chat
(executar_fluxo_assessor), anota o rótulo do juiz, se a tool foi chamada, e o
estado do banco depois. A latência, o status, os tokens e o custo saem do
monitor da Etapa 6.

No fim, apaga só os registros com os marcadores desta execução. Isso é a
limpeza do teste, não faz parte da solução.

Uso, a partir da raiz do projeto:
    python tests/evidencia_escritas.py              juiz real
    python tests/evidencia_escritas.py --sem-chave  juiz sem chave (não responde)
    python tests/evidencia_escritas.py --modelo gemini-flash-latest
        troca só o modelo dos especialistas, para quando a cota diária
        gratuita do gemini-2.5-flash acabou (o fallback Groq não segue o
        protocolo JSON do especialista). O juiz e as tools são os mesmos.
"""

import functools
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import app.llms

if "--modelo" in sys.argv:
    from langchain_google_genai import ChatGoogleGenerativeAI

    from app.config import GEMINI_API_KEY
    from app.observabilidade import coletor

    app.llms.llm = ChatGoogleGenerativeAI(
        model=sys.argv[sys.argv.index("--modelo") + 1],
        temperature=0.7,
        top_p=0.95,
        google_api_key=GEMINI_API_KEY,
        callbacks=[coletor],
    ).with_fallbacks([app.llms.llm_groq])

import app.juiz
from app.graph import executar_fluxo_assessor
from app.memory import get_db
from app.observabilidade import listar_turnos
from app.tools.agenda import TOOLS_AGENDA, criar_evento
from app.tools.db import get_conn
from app.tools.financeiro import TOOLS, add_transaction

USER_ID = "teste_etapa8"
ESCRITAS = {"add_transaction", "update_transaction", "criar_evento", "atualizar_evento", "cancelar_evento"}

_chamadas: list[str] = []


def _espiar(ferramenta) -> None:
    """Anota cada chamada de uma tool de escrita, sem mudar o que ela faz."""
    original = ferramenta.func

    @functools.wraps(original)
    def espia(*args, **kwargs):
        resultado = original(*args, **kwargs)
        status = resultado.get("status") if isinstance(resultado, dict) else "?"
        _chamadas.append(f"{ferramenta.name}:{status}")
        return resultado

    ferramenta.func = espia


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
        "SELECT id, amount FROM transactions WHERE description ILIKE %s OR source_text ILIKE %s ORDER BY id;",
        (f"%{marcador}%", f"%{marcador}%"),
    )


def _valor(transacao_id: int) -> list[tuple]:
    return _sql("SELECT id, amount FROM transactions WHERE id = %s;", (transacao_id,))


def _eventos(**filtro) -> list[dict]:
    return list(get_db()["eventos"].find(
        {"user_id": USER_ID, **filtro},
        {"_id": 1, "titulo": 1, "local": 1, "status": 1},
    ))


def _semear_transacao(marcador: str) -> int:
    resultado = add_transaction.invoke({
        "amount": 10.0,
        "source_text": f"semente {marcador}",
        "type_name": "EXPENSES",
        "category_name": "comida",
        "description": marcador,
    })
    return resultado["id"]


def _semear_evento(marcador: str, dia: str) -> str:
    resultado = criar_evento.invoke(
        {"titulo": marcador, "inicio": f"{dia}T10:00:00-03:00", "fim": f"{dia}T11:00:00-03:00"},
        config={"configurable": {"user_id": USER_ID}},
    )
    return resultado["evento"]["_id"]


def _cenarios(sufixo: str) -> list[dict]:
    m_add = f"E8-ADD-{sufixo}"
    m_usd = f"E8-USD-{sufixo}"
    m_upd = f"E8-UPD-{sufixo}"
    m_evt = f"E8-EVT-{sufixo}"
    m_alt = f"E8-ALT-{sufixo}"
    m_can = f"E8-CAN-{sufixo}"

    id_upd = _semear_transacao(m_upd)
    id_alt = _semear_evento(m_alt, "2026-11-21")
    id_can = _semear_evento(m_can, "2026-11-22")

    return [
        {
            "tool": "add_transaction",
            "marcador": m_add,
            "mensagem": f"Registre uma despesa de 37,90 reais no almoço do restaurante, paga no pix, com a descrição {m_add}.",
            "estado": lambda: _transacoes(m_add),
        },
        {
            "tool": "add_transaction (dólar)",
            "marcador": m_usd,
            "mensagem": f"Gastei 2 dólares num café, registre com a descrição {m_usd}.",
            "estado": lambda: _transacoes(m_usd),
        },
        {
            "tool": "update_transaction",
            "marcador": m_upd,
            "mensagem": f"Altere o valor da transação de id {id_upd} para 45,50 reais.",
            "estado": lambda: _valor(id_upd),
        },
        {
            "tool": "criar_evento",
            "marcador": m_evt,
            "mensagem": f"Crie um compromisso com o título {m_evt} no dia 20/11/2026, das 14h às 15h, horário de Brasília.",
            "estado": lambda: _eventos(titulo={"$regex": m_evt}),
        },
        {
            "tool": "atualizar_evento",
            "marcador": m_alt,
            "mensagem": f"Já confirmo a alteração: no evento de id {id_alt}, mude o local para Sala 7.",
            "estado": lambda: _eventos(_id=id_alt),
        },
        {
            "tool": "cancelar_evento",
            "marcador": m_can,
            "mensagem": f"Já confirmo o cancelamento: cancele o evento de id {id_can}.",
            "estado": lambda: _eventos(_id=id_can),
        },
    ]


def _conclusao(aprovado: bool, chamou: bool, mudou: bool) -> str:
    if not chamou:
        return "tool não chamada"
    if aprovado:
        return "aprovado: escrita permaneceu" if mudou else "aprovado: escrita NÃO permaneceu"
    return "DESENCONTRO: recusado e gravado" if mudou else "recusado: nada permaneceu"


def _limpar(marcadores: list[str]) -> None:
    for marcador in marcadores:
        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute(
                "DELETE FROM transactions WHERE description ILIKE %s OR source_text ILIKE %s;",
                (f"%{marcador}%", f"%{marcador}%"),
            )
            conn.commit()
        finally:
            cur.close()
            conn.close()
    get_db()["eventos"].delete_many({"user_id": USER_ID})
    get_db()["sessoes"].delete_many({"session_id": {"$regex": "^etapa8-"}})


def main() -> None:
    if "--sem-chave" in sys.argv:
        app.juiz.TYPESAFE_API_KEY = None

    for ferramenta in [*TOOLS, *TOOLS_AGENDA]:
        if ferramenta.name in ESCRITAS:
            _espiar(ferramenta)

    sufixo = time.strftime("%H%M%S")
    cenarios = _cenarios(sufixo)
    linhas = []
    try:
        for cenario in cenarios:
            antes = cenario["estado"]()
            _chamadas.clear()
            resposta, agentes = executar_fluxo_assessor(
                cenario["mensagem"], f"etapa8-{cenario['marcador']}", USER_ID
            )
            depois = cenario["estado"]()
            turno = listar_turnos()[-1]
            rotulo = next((a for a in agentes if a.startswith("juiz:")), "juiz:não rodou")
            aprovado = rotulo.startswith("juiz:apoiada noul")
            linhas.append({
                **cenario,
                "antes": antes,
                "depois": depois,
                "chamadas": list(_chamadas),
                "rotulo": rotulo,
                "resposta": resposta.replace("\n", " "),
                "conclusao": _conclusao(aprovado, bool(_chamadas), antes != depois),
                "turno": turno,
            })
    finally:
        _limpar([c["marcador"] for c in cenarios])

    modo = "juiz sem chave" if "--sem-chave" in sys.argv else "juiz real"
    modelo = sys.argv[sys.argv.index("--modelo") + 1] if "--modelo" in sys.argv else "gemini-2.5-flash"
    print(f"\n## Evidência ({modo}, especialistas em {modelo}, sufixo {sufixo})\n")
    for linha in linhas:
        turno = linha["turno"]
        print(f"### {linha['tool']} — marcador {linha['marcador']}")
        print(f"- mensagem: {linha['mensagem']}")
        print(f"- antes: {linha['antes']}")
        print(f"- tool chamada: {', '.join(linha['chamadas']) or 'nenhuma'}")
        print(f"- {linha['rotulo']}")
        print(f"- resposta: {linha['resposta']}")
        print(f"- depois: {linha['depois']}")
        print(f"- conclusão: **{linha['conclusao']}**")
        print(
            f"- monitor: latência {turno['latencia_ms']} ms, status {turno['status']}, "
            f"tokens {turno['tokens_total']}, custo US$ {turno['custo_usd']:.6f}\n"
        )


if __name__ == "__main__":
    main()
