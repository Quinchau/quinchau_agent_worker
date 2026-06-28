from openai import OpenAI
from dotenv import load_dotenv
import os
from typing import Dict, Any, Optional

load_dotenv()

class OpenRouterAgent:
    def __init__(self):
        self.client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY")
        )
        self.default_model = "qwen/qwen-2.5-72b-instruct"

    def classify_user_preference(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Clasifica la preferencia del usuario usando OpenRouter"""
        
        system_prompt = '''Eres un clasificador experto de preferencias de usuarios para e-commerce. 
Responde ÚNICAMENTE con este formato exacto y nada más:

CATEGORIA: ALTA_INTERES | INTERES_MEDIO | BAJO_INTERES | SIN_INTERES
CONFIANZA: XX (número entre 0 y 100)
JUSTIFICACION: (máximo 15 palabras)

Reglas:
- ≥ 9 logs y visitas a productos o carrito → ALTA_INTERES
- 4-8 logs → INTERES_MEDIO
- < 4 logs → BAJO_INTERES o SIN_INTERES
- Tiempo promedio > 45 segundos puede subir el nivel'''

        user_content = f"""Clasifica la preferencia del usuario:

ID: {data.get('user_id')}
Logs últimos 7 días: {data.get('log_count', 0)}
Páginas de productos: {data.get('product_pages', 0)}
Páginas del carrito: {data.get('cart_pages', 0)}
Tiempo promedio: {data.get('avg_time', 0)} segundos"""

        try:
            response = self.client.chat.completions.create(
                model=self.default_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                temperature=0.0,
                max_tokens=80
            )
            
            content = response.choices[0].message.content.strip()
            
            # Parse simple
            lines = content.split('\n')
            category = 'INTERES_MEDIO'  # fallback
            confidence = 70
            justification = 'Clasificación automática'

            for line in lines:
                if line.startswith('CATEGORIA:'):
                    category = line.replace('CATEGORIA:', '').strip()
                elif line.startswith('CONFIANZA:'):
                    try:
                        confidence = int(line.replace('CONFIANZA:', '').strip())
                    except:
                        pass
                elif line.startswith('JUSTIFICACION:'):
                    justification = line.replace('JUSTIFICACION:', '').strip()

            return {
                "status": "success",
                "category": category,
                "confidence": confidence,
                "justification": justification
            }

        except Exception as e:
            print(f"Error llamando a OpenRouter: {e}")
            return {
                "status": "error",
                "category": "INTERES_MEDIO",
                "confidence": 50,
                "justification": "Error en clasificación"
            }

    def general_chat(self, message: str, chat_history: Optional[list] = None) -> str:
        """Chat general con el agente"""
        if chat_history is None:
            chat_history = []

        try:
            response = self.client.chat.completions.create(
                model=self.default_model,
                messages=chat_history + [{"role": "user", "content": message}],
                temperature=0.7,
                max_tokens=300
            )
            return response.choices[0].message.content
        except Exception as e:
            return f"Error: {str(e)}"

# Instancia global
agent = OpenRouterAgent()