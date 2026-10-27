from fastapi import APIRouter, HTTPException

from app.schemas import PerfilRequest, PerfilResponse
from app.tools.perfil import salvar_perfil

router = APIRouter(tags=["perfil"])


@router.post("/perfil", response_model=PerfilResponse)
def criar_ou_atualizar_perfil(requisicao: PerfilRequest) -> PerfilResponse:
    try:
        perfil = salvar_perfil(requisicao.model_dump())
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Não foi possível salvar o perfil: {exc}") from exc
    return PerfilResponse(**perfil)