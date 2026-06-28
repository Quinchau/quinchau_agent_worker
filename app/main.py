from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from rq.job import Job
from .models import (
    ClassifyPreferenceRequest, 
    AgentResponse, 
    ChatRequest, 
    EnqueueResponse,
    ImageEditRequest,
    ImageEditResponse
)
from .tasks import TASKS
from .redis_queue import get_redis
from .jobs import job_edit_image
import uvicorn

app = FastAPI(
    title="Quinchau Agent API",
    description="Proveedor central de funciones IA para el ecosistema Quinchau",
    version="1.0.0",
)

# Permite llamadas desde los otros contenedores Docker
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restringir a los dominios internos si es necesario
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {"status": "ok", "service": "quinchau-agent", "version": "1.0.0"}


@app.get("/health")
async def health():
    """Docker healthcheck + estado de Redis"""
    try:
        get_redis().ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    return {"status": "healthy", "redis": "ok" if redis_ok else "error"}


# ---------------------------------------------------------------
# Clasificación — encola en Redis, responde con job_id
# ---------------------------------------------------------------

@app.post("/classify_user_preference", response_model=EnqueueResponse)
async def classify_user_preference(request: ClassifyPreferenceRequest):
    """
    Encola la clasificación en Redis.
    El worker llama a OpenRouter y persiste en MySQL.
    Retorna job_id para consultar el resultado.
    """
    try:
        task_data = {
            "user_id":             request.user_id,
            "log_count":           request.log_count,
            "product_pages":       request.product_pages,
            "cart_pages":          request.cart_pages,
            "avg_time":            request.avg_time,
            "last_visit_days_ago": request.last_visit_days_ago,
        }
        result = await TASKS["classify_user_preference"](task_data)
        return EnqueueResponse(**result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------
# Consultar resultado de un job
# ---------------------------------------------------------------

@app.get("/job/{job_id}")
async def get_job_result(job_id: str):
    """Consulta el resultado de un job. Estados: queued | started | finished | failed"""
    try:
        job = Job.fetch(job_id, connection=get_redis())
        return {
            "job_id": job.id,
            "status": job.get_status().value,
            "result": job.result if job.is_finished else None,
            "error":  str(job.exc_info) if job.is_failed else None,
        }
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Job no encontrado: {e}")


# ---------------------------------------------------------------
# Chat — síncrono (respuesta directa, no pasa por cola)
# ---------------------------------------------------------------

@app.post("/chat", response_model=AgentResponse)
async def chat(request: ChatRequest):
    """Chat directo con el agente"""
    try:
        result = await TASKS["chat"]({
            "message":      request.message,
            "chat_history": request.chat_history or [],
        })
        return AgentResponse(
            task_type="chat",
            message=result.get("response", "Sin respuesta"),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------
# Edición de imágenes (síncrono, sin cola)
# ---------------------------------------------------------------

@app.post('/image/edit', response_model=ImageEditResponse)
async def edit_image(request: ImageEditRequest):
    """Edita imágenes de productos (remove_bg, dimensions, label, replace)"""
    try:
        result = await job_edit_image(request.dict())
        return ImageEditResponse(**result)
    except Exception as e:
        return ImageEditResponse(success=False, error=str(e))


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)