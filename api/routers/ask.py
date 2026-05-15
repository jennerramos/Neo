"""Ask router — RAG Q&A endpoint."""
from fastapi import APIRouter
from api.services.ask_service import handle_ask
from api.schemas.ask import AskRequest, AskResponse

router = APIRouter(prefix="/ask", tags=["ask"])


@router.post("", response_model=AskResponse)
def ask(req: AskRequest):
    """Submit a natural language question to Neo's RAG pipeline."""
    return handle_ask(req)
