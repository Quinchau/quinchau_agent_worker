from pydantic import BaseModel
from typing import Optional, Dict, Any, List

class ClassifyPreferenceRequest(BaseModel):
    user_id: int
    log_count: int
    product_pages: int
    cart_pages: int
    avg_time: float
    last_visit_days_ago: Optional[int] = None

class AgentResponse(BaseModel):
    status: str = "success"
    task_type: str
    category: Optional[str] = None
    confidence: Optional[int] = None
    justification: Optional[str] = None
    message: str = "Tarea procesada correctamente"

class ChatRequest(BaseModel):
    message: str
    user_id: Optional[int] = None
    chat_history: Optional[list] = None

class EnqueueResponse(BaseModel):
    success: bool
    message: str
    task_id: Optional[str] = None
    queued: bool = True

class DimensionItem(BaseModel):
    """Modelo para una cota individual"""
    x1: float  # coordenada normalizada 0.0-1.0
    y1: float  # coordenada normalizada 0.0-1.0
    x2: float  # coordenada normalizada 0.0-1.0
    y2: float  # coordenada normalizada 0.0-1.0
    text: str  # texto de la medida (ej: "125 mm")

class ImageEditRequest(BaseModel):
    image_base64: str
    operation: str  # 'remove_bg' | 'dimensions' | 'label' | 'replace'
    label_text: Optional[str] = None
    dimensions: Optional[List[DimensionItem]] = None  # Usar DimensionItem en lugar de Dict

class ImageEditResponse(BaseModel):
    success: bool
    image_base64: Optional[str] = None
    format: Optional[str] = None  # 'png', 'jpeg'
    error: Optional[str] = None