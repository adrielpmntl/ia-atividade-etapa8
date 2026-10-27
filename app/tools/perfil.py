from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from langchain.tools import tool
from langchain_core.runnables import RunnableConfig
from qdrant_client import models

from app.memory import get_db
from app.vectorstore import COLLECTION_PERFIL, garantir_collections, gerar_embedding, qdrant


def salvar_perfil(dados: dict) -> dict:
    agora = datetime.now(timezone.utc)
    documento = {
        "user_id": dados["user_id"],
        "renda_mensal": dados["renda_mensal"],
        "objetivo": dados["objetivo"],
        "tolerancia_risco": dados["tolerancia_risco"],
        "preferencias": dados["preferencias"],
        "atualizado_em": agora,
    }
    colecao = get_db()["perfis_financeiros"]
    colecao.create_index("user_id", unique=True)
    colecao.update_one({"user_id": dados["user_id"]}, {"$set": documento}, upsert=True)

    garantir_collections()
    qdrant.upsert(
        collection_name=COLLECTION_PERFIL,
        points=[models.PointStruct(
            id=str(uuid5(NAMESPACE_URL, f"assessor-perfil:{dados['user_id']}")),
            vector=gerar_embedding(dados["preferencias"]),
            payload={
                "user_id": dados["user_id"],
                "preferencias": dados["preferencias"],
                "atualizado_em": agora.isoformat(),
            },
        )],
    )
    return documento


@tool("consultar_perfil_financeiro")
def consultar_perfil_financeiro(config: RunnableConfig) -> dict:
    """Consulta o perfil do usuário da requisição e a preferência relevante."""
    configuracao = config.get("configurable", {})
    user_id = configuracao.get("user_id")
    busca = configuracao.get("profile_query", "")
    if not user_id:
        return {"status": "error", "message": "Usuário não identificado no contexto."}

    documento = get_db()["perfis_financeiros"].find_one({"user_id": user_id}, {"_id": 0})
    if not documento:
        return {
            "status": "not_found",
            "message": "Não há perfil cadastrado. Oriente o usuário a usar a tela Perfil.",
        }

    preferencia = documento.get("preferencias", "")
    if busca:
        garantir_collections()
        resultados = qdrant.query_points(
            collection_name=COLLECTION_PERFIL,
            query=gerar_embedding(busca),
            query_filter=models.Filter(must=[models.FieldCondition(
                key="user_id", match=models.MatchValue(value=user_id)
            )]),
            limit=1,
            with_payload=True,
        )
        if resultados.points:
            preferencia = resultados.points[0].payload.get("preferencias", preferencia)

    return {
        "status": "ok",
        "user_id": user_id,
        "renda_mensal": documento.get("renda_mensal"),
        "objetivo": documento.get("objetivo"),
        "tolerancia_risco": documento.get("tolerancia_risco"),
        "preferencias_relevantes": preferencia,
    }


TOOLS_PERFIL = [consultar_perfil_financeiro]