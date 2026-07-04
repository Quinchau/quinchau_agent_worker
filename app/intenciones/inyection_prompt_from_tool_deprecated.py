"""
Módulo base para inyección de contexto en respuestas del LLM
Reutilizable por cualquier handler que quiera generar respuestas naturales
"""
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class ContextInjector:
    """
    Clase base para inyectar contexto y generar respuestas naturales con el LLM
    """
    
    # Directorio base de prompts
    PROMPTS_DIR = Path(__file__).parent.parent / "contextos"
    
    @classmethod
    def cargar_prompt(cls, nombre_prompt: str, variables: Dict[str, str] = None) -> str:
        """
        Carga un prompt desde archivo y reemplaza variables
        
        Args:
            nombre_prompt: Nombre del archivo de prompt (sin extensión)
            variables: Diccionario con variables a reemplazar {nombre: valor}
        
        Returns:
            Prompt con variables reemplazadas
        """
        prompt_path = cls.PROMPTS_DIR / f"{nombre_prompt}.txt"
        variables = variables or {}
        
        try:
            with open(prompt_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Reemplazar variables {clave} por su valor
            for key, value in variables.items():
                content = content.replace(f'{{{key}}}', str(value) if value else '')
            
            logger.info(f"✅ Prompt cargado: {nombre_prompt} ({len(content)} caracteres)")
            return content
            
        except FileNotFoundError:
            logger.error(f"❌ Prompt no encontrado: {prompt_path}")
            # Fallback genérico con variables
            fallback = f"Información para {nombre_prompt}: "
            for key, value in variables.items():
                fallback += f"{key}={value}, "
            return fallback
    
    @classmethod
    def recargar_prompt(cls, nombre_prompt: str) -> str:
        """
        Recarga un prompt desde el archivo sin reiniciar el servicio
        """
        prompt_path = cls.PROMPTS_DIR / f"{nombre_prompt}.txt"
        try:
            with open(prompt_path, 'r', encoding='utf-8') as f:
                content = f.read()
            logger.info(f"🔄 Prompt recargado: {nombre_prompt} ({len(content)} caracteres)")
            return content
        except FileNotFoundError:
            logger.error(f"❌ Prompt no encontrado: {prompt_path}")
            return ""
    
    @staticmethod
    def generar_respuesta_con_contexto(
        tool_output: str,
        user_message: str,
        contact_id: str,
        first_name: str,
        history: str,
        client,
        model: str = "openai/gpt-4o-mini",
        temperature: float = 0.7,
        max_tokens: int = 500
    ) -> str:
        """
        Genera una respuesta natural usando el tool_output como contexto
        """
        logger.info(f"🔄 Generando respuesta natural desde contexto inyectado")
        
        # Construir el mensaje para el LLM
        messages = [
            {"role": "system", "content": tool_output}
        ]
        
        # Agregar historial si existe
        if history:
            messages.append({
                "role": "user", 
                "content": f"Historial de la conversación:\n{history}\n\nMensaje actual de {first_name}: {user_message}"
            })
        else:
            messages.append({
                "role": "user",
                "content": f"Mensaje de {first_name}: {user_message}"
            })
        
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens
            )
            
            respuesta = response.choices[0].message.content
            logger.info(f"✅ Respuesta generada: {respuesta[:100]}...")
            return respuesta
            
        except Exception as e:
            logger.error(f"❌ Error generando respuesta natural: {e}")
            # Fallback genérico
            return f"Hola {first_name}, he procesado tu consulta. ¿Necesitas ayuda con algo más?"
    
    @staticmethod
    def crear_contexto_desde_archivo(
        prompt_path: str,
        variables: Dict[str, str]
    ) -> str:
        """
        Carga un prompt desde archivo y reemplaza variables
        
        Args:
            prompt_path: Ruta al archivo de prompt
            variables: Diccionario con variables a reemplazar {nombre: valor}
        
        Returns:
            Prompt con variables reemplazadas
        """
        try:
            with open(prompt_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Reemplazar variables
            for key, value in variables.items():
                content = content.replace(f'{{{key}}}', value or '')
            
            return content
        except FileNotFoundError:
            logger.error(f"❌ Prompt no encontrado: {prompt_path}")
            return f"Información base: {variables}"
    
    @staticmethod
    def procesar_inyeccion(
        ctx,
        tool_output: str,
        user_message: str,
        first_name: str,
        history: str,
        client,
        contact_id: str,
        channel: str,
        state_manager,
        intencion: str,
        enviar_mensaje_fn,  # Función para enviar mensaje
        estado_adicional: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Procesa el flujo completo de inyección de contexto
        """
        logger.info(f"🔄 Procesando inyección de contexto para: {intencion}")
        
        # Generar respuesta natural
        respuesta_natural = ContextInjector.generar_respuesta_con_contexto(
            tool_output=tool_output,
            user_message=user_message,
            contact_id=contact_id,
            first_name=first_name,
            history=history,
            client=client
        )
        
        # Enviar mensaje
        logger.info(f"📤 Enviando respuesta generada: {respuesta_natural[:100]}...")
        enviar_mensaje_fn(contact_id, respuesta_natural, channel)
        
        # Preparar estado
        estado_base = {
            'ultima_respuesta': respuesta_natural,
            'tool_output_usado': True,
            'respuesta_generada_por_llm': True,
            f'ultima_respuesta_{intencion}': datetime.now().isoformat(),
        }
        
        if estado_adicional:
            estado_base.update(estado_adicional)
        
        # Actualizar estado
        state_manager.update_state(contact_id, estado_base)
        
        # Limpiar flags
        state_manager.update_state(contact_id, {
            'product_found': False,
            'model_found': False,
            'esperando_confirmacion': False,
            'esperando_respuesta': False,
            'intentos_resolucion': 0,
        })
        
        logger.info(f"✅ Respuesta generada y enviada para: {intencion}")
        
        return {
            "success": True,
            "response": respuesta_natural,
            "contact_id": contact_id,
            "intencion": intencion,
            "processed_at": datetime.now().isoformat(),
            "tool_output_usado": True
        }


# ============================================================================
# ALIAS PARA COMPATIBILIDAD
# ============================================================================

# Alias para mantener compatibilidad con código que usa PromptInjector
PromptInjector = ContextInjector


# ============================================================================
# FUNCIONES GLOBALES PARA USO DIRECTO
# ============================================================================

def cargar_prompt(nombre_prompt: str, variables: Dict[str, str] = None) -> str:
    """
    Función global para cargar un prompt desde archivo.
    Útil para usar en handlers sin necesidad de importar la clase.
    
    Ejemplo:
        from app.intenciones.inyection_prompt_from_tool import cargar_prompt
        prompt = cargar_prompt('prompt_intencion_compra_al_mayoreo', {'user_name': 'Juan'})
    """
    return ContextInjector.cargar_prompt(nombre_prompt, variables)


def inyectar_y_generar(
    tool_output: str,
    user_message: str,
    first_name: str,
    history: str,
    client,
    **kwargs
) -> str:
    """
    Función global para inyectar prompt y generar respuesta.
    
    Ejemplo:
        from app.intenciones.inyection_prompt_from_tool import inyectar_y_generar
        respuesta = inyectar_y_generar(
            tool_output=prompt,
            user_message=message,
            first_name=first_name,
            history=history,
            client=client
        )
    """
    return ContextInjector.generar_respuesta_con_contexto(
        tool_output=tool_output,
        user_message=user_message,
        contact_id="",
        first_name=first_name,
        history=history,
        client=client,
        **kwargs
    )


# ============================================================================
# VERSIÓN SIMPLIFICADA PARA USO RÁPIDO
# ============================================================================

class QuickInjector:
    """
    Versión simplificada para casos de uso rápido
    """
    
    @staticmethod
    def inyectar(
        nombre_prompt: str,
        variables: Dict[str, str],
        user_message: str,
        first_name: str,
        history: str,
        client
    ) -> str:
        """
        Método rápido: Carga prompt, inyecta y genera respuesta en un paso
        """
        prompt = ContextInjector.cargar_prompt(nombre_prompt, variables)
        return ContextInjector.generar_respuesta_con_contexto(
            tool_output=prompt,
            user_message=user_message,
            contact_id="",
            first_name=first_name,
            history=history,
            client=client
        )


# Exportar clases y funciones principales
__all__ = [
    'ContextInjector',
    'PromptInjector',    # Alias de ContextInjector
    'QuickInjector',
    'cargar_prompt',      # Función global
    'inyectar_y_generar'  # Función global
]