# app/intent_classifier.py

import os
import logging
import json
from openai import OpenAI
from .catalog_cache import CatalogCache

logger = logging.getLogger(__name__)

class IntentClassifier:
    def __init__(self):
        self.cache = CatalogCache()
        self.client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
        )

    def clasificar(self, message: str, state: dict = None, historial: list = None) -> dict:
        """
        Clasifica la intención del mensaje usando LLM con contexto de historial.
        
        Args:
            message (str): Mensaje del usuario
            state (dict): Estado actual del usuario (opcional)
            historial (list): Historial de conversación (opcional)
        
        Returns:
            dict: {
                'intencion': 'nombre_de_la_intencion',
                'confianza': 0.95,
                'entidades_detectadas': {'producto': 'bujia', 'modelo': 'hj250nk'},
                'razon': 'explicación'
            }
        """
        
        # Obtener intenciones disponibles
        intenciones = self.cache.get_intenciones()
        intenciones_texto = ""
        for i in intenciones:
            intenciones_texto += f"- {i['nombre']}: {i['descripcion']}\n"
        
        # CONTEXTO DEL ESTADO ACTUAL
        contexto = ""
        if state:
            if state.get('producto'):
                contexto += f"Producto mencionado anteriormente: {state['producto']}\n"
            if state.get('modelo'):
                contexto += f"Modelo mencionado anteriormente: {state['modelo']}\n"
            if state.get('ultima_intencion'):
                contexto += f"Última intención: {state['ultima_intencion']}\n"
            if state.get('entidades_no_resueltas'):
                contexto += f"Entidades pendientes: {state['entidades_no_resueltas']}\n"
        
        # HISTORIAL DE LA CONVERSACIÓN (últimos 3 turnos)
        historial_texto = ""
        if historial and len(historial) > 0:
            historial_texto = "\nHISTORIAL RECIENTE:\n"
            for turno in historial[-3:]:
                cliente = turno.get('cliente', '')
                asistente = turno.get('asistente', '')
                if cliente or asistente:
                    historial_texto += f"Cliente: {cliente}\n"
                    historial_texto += f"Asistente: {asistente}\n\n"
        
        prompt = f"""
        Eres un clasificador de intenciones para una tienda de motos llamada Quinchau Motos.

        {historial_texto}

        CONTEXTO DE LA CONVERSACIÓN:
        {contexto if contexto else "No hay contexto previo."}

        MENSAJE DEL CLIENTE: "{message}"

        INTENCIONES POSIBLES:
        {intenciones_texto}

        INSTRUCCIONES:
        1. Analiza el mensaje actual EN EL CONTEXTO del historial de la conversación.
        2. Si el cliente está respondiendo a una pregunta anterior, mantén la misma intención.
        3. Si el cliente cambia de tema claramente, detecta la nueva intención.
        4. Si el mensaje es un saludo o no tiene intención comercial, usa "sin_clasificar".
        5. Responde SOLO con un JSON válido.

        RESPUESTA EN JSON:
        {{
            "intencion": "nombre_de_la_intencion",
            "confianza": 0.95,
            "entidades_detectadas": {{
                "producto": "nombre_producto" (si se menciona),
                "modelo": "nombre_modelo" (si se menciona),
                "ubicacion": "ubicacion" (si se menciona)
            }},
            "razon": "breve explicación de por qué elegiste esta intención, considerando el contexto"
        }}
"""
        
        try:
            response = self.client.chat.completions.create(
                model="openai/gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=150,
                response_format={"type": "json_object"}
            )
            
            result = json.loads(response.choices[0].message.content)
            
            logger.info(f"🧠 LLM clasificó: {result.get('intencion')} (confianza: {result.get('confianza', 0):.2f})")
            logger.info(f"   Razón: {result.get('razon', 'N/A')}")
            
            return result
            
        except Exception as e:
            logger.error(f"❌ Error clasificando intención: {e}")
            return {
                'intencion': 'sin_clasificar',
                'confianza': 0.0,
                'entidades_detectadas': {},
                'razon': f'Error: {str(e)}'
            }

    def get_entidades_bloqueantes(self, intencion):
        """
        Obtiene las entidades bloqueantes para una intención, desde cache (Redis,
        TTL 5 min) con fallback a BD si Redis no está disponible.
        """
        if not intencion:
            logger.warning("⚠️ intencion vacía en get_entidades_bloqueantes")
            return []

        bloqueantes_map = self.cache.get_bloqueantes_map()
        return bloqueantes_map.get(intencion, [])

    def validate_entities(self, intencion, state):
        """
        Valida si las entidades bloqueantes están resueltas en el estado ACTUAL.
        
        ⚠️ CRÍTICO: state DEBE ser el estado más reciente de Redis.
        
        Args:
            intencion (str): Nombre de la intención detectada
            state (dict): Estado ACTUAL del usuario (después de EntityResolver)
        
        Returns:
            list: Lista de entidades faltantes (vacío si todas resueltas)
        """
        if not intencion:
            logger.warning("⚠️ intencion vacía en validate_entities")
            return []

        if not state or not isinstance(state, dict):
            logger.warning("⚠️ state inválido en validate_entities")
            return []

        bloqueantes = self.get_entidades_bloqueantes(intencion)

        if not bloqueantes:
            logger.info(f"ℹ️ No hay entidades bloqueantes para '{intencion}'")
            return []

        faltantes = []
        
        logger.info(f"🔍 Validando entidades para '{intencion}' en estado actual:")
        logger.info(f"   - Producto en estado: '{state.get('producto')}'")
        logger.info(f"   - Modelo en estado: '{state.get('modelo')}'")
        logger.info(f"   - Bloqueantes requeridas: {bloqueantes}")
        
        for entidad in bloqueantes:
            valor = state.get(entidad)
            if valor is None or valor == "" or valor == "None":
                faltantes.append(entidad)
                logger.info(f"⚠️ Entidad faltante: {entidad} (valor: {valor})")
            else:
                logger.info(f"✅ Entidad presente: {entidad} = '{valor}'")

        if faltantes:
            logger.info(f"⚠️ Faltan {len(faltantes)} entidades: {faltantes}")
        else:
            logger.info(f"✅ Todas las entidades resueltas para '{intencion}'")

        return faltantes