"""
Intención: intencion_compra_al_mayoreo
Handler que detecta compras al por mayor y genera respuestas naturales
mediante inyección de contexto al LLM.
"""
import logging
from datetime import datetime

from .context import IntentContext, registrar
from .inyection_prompt_from_tool import PromptInjector

logger = logging.getLogger(__name__)

# Nombre del archivo de prompt (sin extensión)
PROMPT_NOMBRE = "prompt_intencion_compra_al_mayoreo"
MAX_HISTORIAL_MENSAJES = 6  # Número máximo de mensajes en el historial


@registrar("intencion_compra_al_mayoreo")
def handle(ctx: IntentContext) -> dict:
    """
    Handler para compras al mayoreo con validaciones.
    """
    # ============================================================
    # 1. VALIDACIONES Y DATOS DEL USUARIO
    # ============================================================
    if not ctx.contact_id:
        logger.error("❌ contact_id no presente en el contexto")
        return {
            "success": False,
            "error": "contact_id requerido",
            "intencion": ctx.intencion
        }
    
    user_name = ctx.first_name or "cliente"
    mensaje_usuario = ctx.message or ""
    historial_completo = ctx.historial_texto or ""
    
    # ============================================================
    # 2. RECORTAR HISTORIAL A ÚLTIMOS 6 MENSAJES
    # ============================================================
    historial_recortado = ""
    if historial_completo:
        # Dividir el historial por líneas
        lineas = historial_completo.split('\n')
        # Tomar las últimas 12 líneas (6 mensajes de cliente + 6 de asistente)
        lineas_recortadas = lineas[-12:] if len(lineas) > 12 else lineas
        historial_recortado = '\n'.join(lineas_recortadas)
        logger.info(f"📋 Historial recortado: {len(historial_recortado)} caracteres (últimos 6 mensajes)")
    else:
        historial_recortado = "No hay historial reciente"
    
    # ============================================================
    # 3. OBTENER CONTEXTO DEL PRODUCTO DEL ESTADO
    # ============================================================
    estado = ctx.state or {}
    producto = estado.get('producto', '')
    modelo = estado.get('modelo', '')
    ultima_respuesta = estado.get('ultima_respuesta', '')
    
    # Construir contexto del producto
    product_context = ""
    if producto:
        product_context += f"Producto consultado: {producto}\n"
    if modelo:
        product_context += f"Modelo: {modelo}\n"
    if ultima_respuesta:
        product_context += f"Última respuesta del asistente: {ultima_respuesta[:200]}...\n"
    
    # ============================================================
    # 4. VERIFICAR ESTADO Y REPETICIONES
    # ============================================================
    ya_inyectado = estado.get('contexto_mayoreo_inyectado', False)
    veces_inyectado = estado.get('veces_inyectado_mayoreo', 0)
    
    extra_instruccion = ""
    if veces_inyectado >= 3:
        logger.info(f"🔄 Estrategia anti-repetición activada (intento #{veces_inyectado + 1})")
        extra_instruccion = """
⚠️ IMPORTANTE: Este es el mensaje #{veces} del usuario sobre mayoreo.
- NO des la misma respuesta que antes
- Pregunta si ya revisó los listados
- Ofrece ayuda específica sobre productos
- Si no ha revisado, motívalo a hacerlo
"""
    
    # ============================================================
    # 5. CARGAR PROMPT CON VARIABLES
    # ============================================================
    variables = {
        'user_name': user_name,
        'user_message': mensaje_usuario,
        'history': historial_recortado,
        'product_context': product_context or "No hay información de producto previa"
    }
    
    try:
        prompt_final = PromptInjector.cargar_prompt(PROMPT_NOMBRE, variables)
        logger.info(f"📋 Prompt cargado: {len(prompt_final)} caracteres")
        logger.info(f"📝 Prompt completo:\n{prompt_final}")
    except Exception as e:
        logger.error(f"❌ Error cargando prompt: {e}")
        prompt_final = f"""
        Información de mayoreo para {user_name}:
        - Descargar listados: https://quinchau.com/downloader
        - Ofertas: https://quinchau.com/ofertas
        
        Historial reciente: {historial_recortado}
        Contexto producto: {product_context}
        
        Responde de forma natural al mensaje: {mensaje_usuario}
        """
    
    # ============================================================
    # 6. AGREGAR NOTAS ADICIONALES
    # ============================================================
    if ya_inyectado and veces_inyectado > 1:
        prompt_final += f"""
        
## ⚠️ NOTA DE REPETICIÓN:
El usuario ya ha recibido información de mayoreo {veces_inyectado} veces. 
- NO repitas la misma información
- Personaliza tu respuesta según el contexto actual
- Pregunta si necesita ayuda con algo específico o si ya revisó los listados
"""
    
    if extra_instruccion:
        prompt_final += extra_instruccion
    
    # ============================================================
    # 7. ACTUALIZAR ESTADO
    # ============================================================
    try:
        ctx.state_manager.update_state(ctx.contact_id, {
            'ultima_intencion': ctx.intencion,
            'contexto_mayoreo_inyectado': True,
            'veces_inyectado_mayoreo': veces_inyectado + 1,
            'fecha_ultima_inyeccion': datetime.now().isoformat(),
            'ultimo_mensaje_usuario': mensaje_usuario,
            'historial_recortado': historial_recortado[:500],
            'enlaces_mayoreo': {
                'downloader': 'https://quinchau.com/downloader',
                'ofertas': 'https://quinchau.com/ofertas'
            }
        })
    except Exception as e:
        logger.error(f"❌ Error actualizando estado: {e}")

    # ============================================================
    # 8. RETORNAR TOOL_OUTPUT
    # ============================================================
    return {
        "success": True,
        "tool_output": prompt_final,
        "intencion": ctx.intencion,
        "contact_id": ctx.contact_id,
        "user_name": user_name,
        "veces_inyectado": veces_inyectado + 1,
        "processed_at": datetime.now().isoformat()
    }


def reload_prompt() -> str:
    """Recarga el prompt desde el archivo."""
    return PromptInjector.recargar_prompt(PROMPT_NOMBRE)


def get_prompt_info() -> dict:
    """Obtiene información del prompt actual."""
    from pathlib import Path
    prompt_path = PromptInjector.PROMPTS_DIR / f"{PROMPT_NOMBRE}.txt"
    
    return {
        "nombre": PROMPT_NOMBRE,
        "ruta": str(prompt_path),
        "existe": prompt_path.exists(),
        "tamano": prompt_path.stat().st_size if prompt_path.exists() else 0
    }