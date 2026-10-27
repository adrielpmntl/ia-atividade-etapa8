"""Fluxo LangGraph do Acessor.AI."""

import json
import operator
from typing import Annotated

from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, MessagesState, StateGraph
from app.memory import salvar_mensagem
from app.observabilidade import abrir_turno, fechar_turno, status_do_estado

from app.guardrail import anonimizar_entrada, guardrail_entrada, guardrail_saida
from app.juiz import no_juiz
from app.llms import llm, llm_rapido
from app.memory import recuperar_mensagens_sessao, salvar_mensagens
from app.prompts import (
    AGENDA_PROMPT_COMPLETO,
    FAQ_PROMPT_COMPLETO,
    FINANCEIRO_PROMPT_COMPLETO,
    ORQUESTRADOR_PROMPT_COMPLETO,
    ROUTER_PROMPT_COMPLETO,
)
from app.tools.financeiro import TOOLS
from app.tools.agenda import TOOLS_AGENDA
from app.tools.faq import faq_retriever
from app.tools.memoria import TOOLS_MEMORIA

router_agent = create_agent(model=llm_rapido, system_prompt=ROUTER_PROMPT_COMPLETO, tools=TOOLS_MEMORIA)
financeiro_agent = create_agent(
    model=llm,
    system_prompt=FINANCEIRO_PROMPT_COMPLETO,
    tools=TOOLS + TOOLS_MEMORIA,
)
agenda_agent = create_agent(
    model=llm, system_prompt=AGENDA_PROMPT_COMPLETO, tools=TOOLS_AGENDA + TOOLS_MEMORIA
)
# O retriever do FAQ ainda depende de indexação externa; o agente permanece
# disponível para responder pelas instruções de sistema enquanto ela não ocorre.
faq_agent = create_agent(
    model=llm_rapido, system_prompt=FAQ_PROMPT_COMPLETO, tools=[faq_retriever]
)
orquestrador_app = create_agent(
    model=llm_rapido, system_prompt=ORQUESTRADOR_PROMPT_COMPLETO, tools=[]
)


class Estado(MessagesState):
    agentes_chamados: Annotated[list[str], operator.add]
    rota: str
    mapa_pii: dict[str, str]
    bloqueado: bool


def _texto_mensagem(mensagem: object) -> str:
    conteudo = getattr(mensagem, "content", mensagem)
    return conteudo if isinstance(conteudo, str) else str(conteudo)


def _extrair_json_do_especialista(texto: str) -> dict | None:
    """Extrai o primeiro objeto JSON, mesmo se o modelo usar Markdown."""
    texto = texto.strip()
    if texto.startswith("```"):
        texto = texto.split("\n", 1)[1] if "\n" in texto else ""
        if texto.rstrip().endswith("```"):
            texto = texto.rstrip()[:-3].rstrip()

    decoder = json.JSONDecoder()
    for inicio, caractere in enumerate(texto):
        if caractere != "{":
            continue
        try:
            conteudo, _ = decoder.raw_decode(texto[inicio:])
        except json.JSONDecodeError:
            continue
        if isinstance(conteudo, dict):
            return conteudo
    return None


def _formatar_resposta_especialista(texto: str) -> str:
    """Converte o protocolo interno do especialista em texto para a interface."""
    resultado = _extrair_json_do_especialista(texto)
    if not resultado or not isinstance(resultado.get("resposta"), str):
        return "Não foi possível interpretar a resposta do especialista. Tente novamente."

    partes = [resultado["resposta"].strip()]
    recomendacao = resultado.get("recomendacao")
    if isinstance(recomendacao, str) and recomendacao.strip():
        partes.append(f"Recomendação: {recomendacao.strip()}")

    acompanhamento = resultado.get("esclarecer") or resultado.get("acompanhamento")
    if isinstance(acompanhamento, str) and acompanhamento.strip():
        partes.append(f"Acompanhamento: {acompanhamento.strip()}")

    return "\n\n".join(partes)


def no_guardrail_entrada(estado: Estado) -> dict:
    texto, mapa_pii = anonimizar_entrada(_texto_mensagem(estado["messages"][-1]))
    resultado = guardrail_entrada(texto)
    if resultado["bloqueado"]:
        return {
            "bloqueado": True,
            "mapa_pii": mapa_pii,
            "messages": [AIMessage(content=resultado["mensagem"])],
        }
    return {"bloqueado": False, "mapa_pii": mapa_pii}


def decidir_pos_guardrail_entrada(estado: Estado) -> str:
    return "fim" if estado.get("bloqueado", False) else "roteador"


def no_roteador(estado: Estado, config) -> dict:
    saida = router_agent.invoke({"messages": list(estado["messages"])}, config=config)
    texto = _texto_mensagem(saida["messages"][-1])
    if not texto.strip().startswith("ROUTE="):
        return {"agentes_chamados": ["roteador"], "rota": "fim", "messages": [AIMessage(content=texto)]}

    rota = next(
        (linha.split("=", 1)[1].strip() for linha in texto.splitlines() if linha.startswith("ROUTE=")),
        "fim",
    )
    return {"agentes_chamados": ["roteador"], "rota": rota}


def no_financeiro(estado: Estado, config) -> dict:
    saida = financeiro_agent.invoke({"messages": list(estado["messages"])}, config=config)
    return {"agentes_chamados": ["financeiro"], "messages": [saida["messages"][-1]]}


def no_agenda(estado: Estado, config) -> dict:
    saida = agenda_agent.invoke({"messages": list(estado["messages"])}, config=config)
    return {"agentes_chamados": ["agenda"], "messages": [saida["messages"][-1]]}


def no_faq(estado: Estado, config) -> dict:
    saida = faq_agent.invoke({"messages": list(estado["messages"])}, config=config)
    return {"agentes_chamados": ["faq"], "messages": [saida["messages"][-1]]}


def no_orquestrador(estado: Estado, config: RunnableConfig) -> dict:
    # Pega a última resposta do especialista (última AIMessage com conteúdo)
    ultima_especialista = ""
    for mensagem in reversed(estado["messages"]):
        if mensagem.type == "ai" and mensagem.content:
            ultima_especialista = mensagem.content
            break
    # config segue o mesmo motivo do roteador: sem ele, o modelo deste nó
    # não herda o metadata do grafo (e o registro de observabilidade perde o nó).
    saida = orquestrador_app.invoke({
        "messages": [{"role": "human", "content": ultima_especialista}]
    }, config=config)

    return {
        "agentes_chamados": ["orquestrador"],
        "messages":        [{"role": "assistant", "content": saida["messages"][-1].text}]
    }


def no_guardrail_saida(estado: Estado) -> dict:
    resultado = guardrail_saida(
        _texto_mensagem(estado["messages"][-1]), estado.get("mapa_pii", {})
    )
    return {"messages": [AIMessage(content=resultado["conteudo"])]}


def decidir_especialista(estado: Estado) -> str:
    return estado.get("rota", "fim") if estado.get("rota") in {"financeiro", "agenda", "faq"} else "fim"


grafo = StateGraph(Estado)
grafo.add_node("guardrail_entrada", no_guardrail_entrada)
grafo.add_node("roteador", no_roteador)
grafo.add_node("financeiro", no_financeiro)
grafo.add_node("agenda", no_agenda)
grafo.add_node("faq", no_faq)
grafo.add_node("orquestrador", no_orquestrador)
grafo.add_node("guardrail_saida", no_guardrail_saida)
grafo.add_node("juiz", no_juiz)
grafo.set_entry_point("guardrail_entrada")
grafo.add_conditional_edges("guardrail_entrada", decidir_pos_guardrail_entrada, {"roteador": "roteador", "fim": END})
grafo.add_conditional_edges("roteador", decidir_especialista, {"financeiro": "financeiro", "agenda": "agenda", "faq": "faq", "fim": END})
grafo.add_edge("financeiro",      "orquestrador")
grafo.add_edge("agenda",          "orquestrador")
grafo.add_edge("orquestrador",    "guardrail_saida")
grafo.add_edge("guardrail_saida", "juiz")
grafo.add_edge("faq",             "juiz")
grafo.add_edge("juiz",            END)

# O histórico das sessões já é persistido no MongoDB. Manter checkpoints de
# execução aqui fazia cada nova mensagem reenviar todas as mensagens internas
# dos agentes aos modelos, aumentando a latência a cada turno.
fluxo_agentes = grafo.compile()


def executar_fluxo_assessor(
    pergunta_usuario: str, session_id: str, user_id: str = "usuario_teste"
) -> str:
    pergunta_usuario_anonimizado, _ = anonimizar_entrada(pergunta_usuario)
    estado_inicial = {
        "messages":         [{"role": "human", "content": pergunta_usuario}],
        "agentes_chamados": [],
        "rota":             "",
        "mapa_pii":         {},
        "session_id":       session_id,

    }

    # O registro abre antes do invoke e fecha no finally, inclusive quando
    # o grafo levanta. A rota /monitor só lê o que ficou anotado.
    turno_id = abrir_turno()
    status = "erro"
    rota = ""
    try:
        estado_final = fluxo_agentes.invoke(
            estado_inicial,
            config={"configurable": {"thread_id": session_id, "user_id": user_id}},
        )

        resposta = _texto_mensagem(estado_final["messages"][-1])

        # Salva no MongoDB após cada interação
        salvar_mensagem(session_id, "human",     pergunta_usuario_anonimizado, user_id=user_id)
        salvar_mensagem(session_id, "assistant", resposta, user_id=user_id)

        status = status_do_estado(estado_final)
        rota = estado_final.get("rota") or ""
        return resposta, estado_final.get("agentes_chamados", [])
    finally:
        fechar_turno(turno_id, session_id=session_id, rota=rota, status=status)