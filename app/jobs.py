import os
import re
import io
import base64
import httpx
from typing import Dict, Any, List, Optional
from PIL import Image
from sqlalchemy import text
from dotenv import load_dotenv
import math
from PIL import Image, ImageDraw, ImageFont

load_dotenv()

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_KEY     = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL   = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash-image")
AGENT_MODEL        = os.getenv("AGENT_MODEL", "qwen/qwen-2.5-72b-instruct")
SITE_URL           = os.getenv("SITE_URL", "https://quinchau.com")


def _call_openrouter(messages: list, max_tokens: int = 150, temperature: float = 0.0) -> str:
    """Llamada síncrona a OpenRouter. Lanza excepción si falla."""
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "HTTP-Referer": SITE_URL,
        "X-Title": "Quinchau Agent",
        "Content-Type": "application/json",
    }
    payload = {
        "model": AGENT_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    response = httpx.post(OPENROUTER_API_URL, json=payload, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()


def _get_db():
    """Importación lazy del engine para no romper el módulo si MySQL no está disponible"""
    from app.database import get_db_connection
    return get_db_connection()


# ---------------------------------------------------------------
# JOB: classify_user_preference
# ---------------------------------------------------------------

def job_classify_user_preference(data: dict) -> dict:
    """
    Clasifica el nivel de interés de un usuario y guarda en MySQL.

    data keys: user_id, log_count, product_pages, cart_pages, avg_time, last_visit_days_ago
    """
    system_prompt = """Eres un clasificador experto de preferencias de usuarios para e-commerce.
Responde ÚNICAMENTE con este formato exacto y nada más:

CATEGORIA: ALTA_INTERES | INTERES_MEDIO | BAJO_INTERES | SIN_INTERES
CONFIANZA: XX (número entre 0 y 100)
JUSTIFICACION: (máximo 15 palabras)

Reglas:
- >= 9 logs y visitas a productos o carrito -> ALTA_INTERES
- 4-8 logs -> INTERES_MEDIO
- < 4 logs -> BAJO_INTERES o SIN_INTERES
- Tiempo promedio > 60 segundos puede subir el nivel"""

    user_content = (
        f"Clasifica la preferencia del usuario:\n\n"
        f"ID: {data.get('user_id')}\n"
        f"Logs últimos 7 días: {data.get('log_count', 0)}\n"
        f"Páginas de productos: {data.get('product_pages', 0)}\n"
        f"Páginas del carrito: {data.get('cart_pages', 0)}\n"
        f"Tiempo promedio: {data.get('avg_time', 0)} segundos"
    )

    content = _call_openrouter(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_content},
        ],
        max_tokens=80,
        temperature=0.0,
    )

    result = _parse_classification(content)

    # Persistir en MySQL
    try:
        with _get_db() as conn:
            conn.execute(
                text("""
                    UPDATE users
                    SET preference_category    = :category,
                        preference_confidence  = :confidence,
                        last_classification_at = NOW()
                    WHERE id = :user_id
                """),
                {
                    "category":   result["category"],
                    "confidence": result.get("confidence", 70),
                    "user_id":    data["user_id"],
                },
            )
            conn.commit()
        print(f"[job] usuario {data['user_id']} → {result['category']} ({result.get('confidence')}%)")
    except Exception as e:
        print(f"[job] error BD: {e}")
        result["db_error"] = str(e)

    return result


# ---------------------------------------------------------------
# JOB: general_chat
# ---------------------------------------------------------------

def job_general_chat(data: dict) -> dict:
    """
    Chat general. No persiste en BD.

    data keys: message, chat_history (list, opcional)
    """
    message      = data.get("message", "")
    chat_history = data.get("chat_history", [])

    response = _call_openrouter(
        messages=chat_history + [{"role": "user", "content": message}],
        max_tokens=300,
        temperature=0.7,
    )

    return {"status": "success", "response": response}


# ---------------------------------------------------------------
# JOB: edit_image (para edición de imágenes de productos)
# ---------------------------------------------------------------

async def job_edit_image(request: Dict[str, Any]) -> Dict[str, Any]:
    """
    Procesa la edición de imagen según la operación solicitada.
    """
    image_base64 = request['image_base64']
    operation = request['operation']

    if operation == 'dimensions':
        dimensions = request.get('dimensions', [])
        if not dimensions:
            return {'success': True, 'image_base64': image_base64, 'format': 'jpeg'}
        try:
            SUPERSAMPLE = 3

            img_orig = Image.open(io.BytesIO(base64.b64decode(image_base64))).convert('RGB')
            w, h     = img_orig.size
            img_big  = img_orig.resize((w * SUPERSAMPLE, h * SUPERSAMPLE), Image.LANCZOS)
            draw     = ImageDraw.Draw(img_big)

            scale    = max(w, h) / 1000
            line_w   = max(3,  int(4  * scale)) * SUPERSAMPLE
            arrow_sz = max(25, int(30 * scale)) * SUPERSAMPLE

            # ── Paso 1: líneas y flechas en espacio 3x ──────────────
            for dim in dimensions:
                x1 = int(dim['x1'] * w) * SUPERSAMPLE
                y1 = int(dim['y1'] * h) * SUPERSAMPLE
                x2 = int(dim['x2'] * w) * SUPERSAMPLE
                y2 = int(dim['y2'] * h) * SUPERSAMPLE
                color = dim.get('color', '#000000')

                draw.line([(x1, y1), (x2, y2)], fill=color, width=line_w)
                _draw_arrowhead(draw, x1, y1, x2, y2, size=arrow_sz, color=color)
                _draw_arrowhead(draw, x2, y2, x1, y1, size=arrow_sz, color=color)

            # ── Paso 2: reducir a tamaño original → anti-aliasing ───
            img_final = img_big.resize((w, h), Image.LANCZOS)

            font_sz_text = max(22, int(30 * scale))
            try:
                font_final = ImageFont.truetype(
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_sz_text)
            except Exception as e:
                print(f"[debug] font failed: {e}")
                font_final = ImageFont.load_default()

            # ── Paso 3: texto rotado sobre la línea en espacio 1x ───
            for dim in dimensions:
                ox1 = int(dim['x1'] * w)
                oy1 = int(dim['y1'] * h)
                ox2 = int(dim['x2'] * w)
                oy2 = int(dim['y2'] * h)
                text  = dim['text']
                color = dim.get('color', '#000000')
                mid_x = (ox1 + ox2) // 2
                mid_y = (oy1 + oy2) // 2

                angle_rad = math.atan2(oy2 - oy1, ox2 - ox1)
                angle_deg = math.degrees(angle_rad)

                bbox = ImageDraw.Draw(Image.new('RGBA', (1, 1))).textbbox((0, 0), text, font=font_final)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                pad_t = max(4, int(6 * scale))

                perp_x = -math.sin(angle_rad) * (th / 2 + pad_t + line_w // SUPERSAMPLE)
                perp_y =  math.cos(angle_rad) * (th / 2 + pad_t + line_w // SUPERSAMPLE)

                txt_img  = Image.new('RGBA', (tw + pad_t * 2, th + pad_t * 2), (255, 255, 255, 0))
                txt_draw = ImageDraw.Draw(txt_img)
                txt_draw.text((pad_t, pad_t), text, fill=color, font=font_final)

                txt_rotated = txt_img.rotate(-angle_deg, expand=True)

                paste_x = int(mid_x + perp_x) - txt_rotated.width  // 2
                paste_y = int(mid_y + perp_y) - txt_rotated.height // 2

                img_final.paste(txt_rotated, (paste_x, paste_y), txt_rotated)

            out = io.BytesIO()
            img_final.save(out, format='JPEG', quality=95)
            return {
                'success':      True,
                'image_base64': base64.b64encode(out.getvalue()).decode(),
                'format':       'jpeg',
            }
        except Exception as e:
            print(f"[job] Error Pillow dimensions: {e}")
            return {'success': False, 'error': str(e)}

    # Remove background: LLM
    try:
        prompt = _build_prompt(operation, request, image_base64)

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                'https://openrouter.ai/api/v1/chat/completions',
                headers={
                    'Authorization': f'Bearer {OPENROUTER_KEY}',
                    'Content-Type': 'application/json'
                },
                json={
                    'model': OPENROUTER_MODEL,
                    'messages': [
                        {
                            'role': 'user',
                            'content': [
                                {'type': 'text', 'text': prompt},
                                {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_base64}'}}
                            ]
                        }
                    ]
                }
            )

        if response.status_code != 200:
            return {'success': False, 'error': f'OpenRouter error: {response.status_code}'}

        data = response.json()
        result_base64 = _extract_image_from_response(data)

        if not result_base64:
            return {'success': False, 'error': 'No se pudo extraer la imagen de la respuesta'}

        img_format = _detect_format_from_base64(result_base64)

        return {
            'success': True,
            'image_base64': result_base64,
            'format': img_format
        }

    except httpx.TimeoutException:
        return {'success': False, 'error': 'Timeout de 60 segundos excedido'}
    except Exception as e:
        print(f"[job] Error en edit_image: {e}")
        return {'success': False, 'error': str(e)}

def _draw_arrowhead(draw: ImageDraw.ImageDraw, from_x: int, from_y: int,
                    to_x: int, to_y: int, size: int = 12, color: str = '#000000') -> None:
    """Punta de flecha con triángulo apuntando hacia afuera y línea perpendicular."""
    angle = math.atan2(to_y - from_y, to_x - from_x)

    # Triángulo apunta HACIA AFUERA (away from line), invertido respecto al original
    tip_x = from_x - size * math.cos(angle)
    tip_y = from_y - size * math.sin(angle)
    left  = (
        from_x + size * math.cos(angle + math.radians(150)),
        from_y + size * math.sin(angle + math.radians(150)),
    )
    right = (
        from_x + size * math.cos(angle - math.radians(150)),
        from_y + size * math.sin(angle - math.radians(150)),
    )
    draw.polygon([(tip_x, tip_y), left, right], fill=color)

    # Línea perpendicular en la base del triángulo
    perp_len = size * 1.2
    perp_x = -math.sin(angle)
    perp_y =  math.cos(angle)
    p1 = (from_x + perp_x * perp_len, from_y + perp_y * perp_len)
    p2 = (from_x - perp_x * perp_len, from_y - perp_y * perp_len)
    line_w = max(2, size // 8)
    draw.line([p1, p2], fill=color, width=line_w)


def _build_prompt(operation: str, request: Dict[str, Any], image_base64: str) -> str:
    """Construye el prompt según la operación, convirtiendo coordenadas si es necesario."""
    
    if operation == 'remove_bg':
        return """Remove the background from this product image completely.

CRITICAL INSTRUCTIONS:
1. Identify the MAIN PRODUCT in the image (the central object being sold)
2. REMOVE EVERYTHING that is NOT the product:
   - Remove background completely and replace with SOLID WHITE (#FFFFFF)
   - Remove any stands, shelves, mannequins, or supports behind/under the product
   - Remove any shadows, reflections, or floor surfaces
   - Remove any decorative elements, frames, borders, or text that is not on the product
   - Remove any watermarks, logos, or branding that is not part of the product
3. KEEP ONLY the product itself, centered in the image
4. Keep text, labels, and details that are PHYSICALLY ON the product EXACTLY as they appear
5. OUTPUT FORMAT: SQUARE - JPEG with solid white background, minimal margin
6. Do NOT add any new elements"""
    
    if operation == 'dimensions':
        dimensions = request.get('dimensions', [])
        if not dimensions:
            return "Return the image unchanged."
        
        # Convertir coordenadas normalizadas a píxeles absolutos
        try:
            img = Image.open(io.BytesIO(base64.b64decode(image_base64)))
            w, h = img.size
        except Exception as e:
            print(f"[job] Error al abrir imagen: {e}")
            w, h = 1000, 1000
        
        dims_text = []
        for dim in dimensions:
            x1 = int(dim['x1'] * w)
            y1 = int(dim['y1'] * h)
            x2 = int(dim['x2'] * w)
            y2 = int(dim['y2'] * h)
            dims_text.append(f"- From pixel ({x1}, {y1}) to pixel ({x2}, {y2}): {dim['text']}")
        
        dims_str = "\n".join(dims_text)
        
        return f"""You are a technical illustration assistant.
Draw dimension annotations on this product image.

For each dimension below, draw a professional dimension line
with arrows at both ends and the measurement text centered above the line.

Dimensions to draw:
{dims_str}

Use thin black lines with solid arrowheads. Text in sans-serif font.
Do not modify the product. Return only the annotated image."""
    
    return "Return the image unchanged."


def _extract_image_from_response(response_data: dict) -> str:
    """Extrae la imagen base64 de la respuesta de OpenRouter."""
    try:
        choices = response_data.get('choices', [])
        if not choices:
            return ""
        
        message = choices[0].get('message', {})
        
        # Buscar en images array (formato de Gemini con imágenes)
        images = message.get('images', [])
        if images:
            for img in images:
                if isinstance(img, dict):
                    url = img.get('image_url', {}).get('url', '')
                    if url and url.startswith('data:image'):
                        return url.split(',', 1)[1]
        
        # Fallback: buscar en content (formato texto plano)
        content = message.get('content', '')
        if isinstance(content, str) and content.startswith('data:image'):
            return content.split(',', 1)[1]
        
        # Fallback: si content es una lista
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    url = item.get('image_url', {}).get('url', '')
                    if url and url.startswith('data:image'):
                        return url.split(',', 1)[1]
        
        return ""
    except Exception as e:
        print(f"[job] Error extrayendo imagen: {e}")
        return ""


def _detect_format_from_base64(base64_str: str) -> str:
    """Detecta si el base64 corresponde a PNG o JPEG por los primeros bytes."""
    if not base64_str:
        return 'jpeg'
    
    prefix = base64_str[:40]
    if prefix.startswith('iVBOR') or prefix.startswith('iVBO'):
        return 'png'
    if prefix.startswith('/9j/') or prefix.startswith('/9j'):
        return 'jpeg'
    
    return 'jpeg'


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------

def _parse_classification(content: str) -> dict:
    valid = {"ALTA_INTERES", "INTERES_MEDIO", "BAJO_INTERES", "SIN_INTERES"}

    cat_m  = re.search(r"CATEGORIA:\s*(\w+)", content, re.IGNORECASE)
    conf_m = re.search(r"CONFIANZA:\s*(\d+)", content)
    just_m = re.search(r"JUSTIFICACION:\s*(.+)", content)

    category = "INTERES_MEDIO"
    if cat_m and cat_m.group(1).upper() in valid:
        category = cat_m.group(1).upper()

    confidence = 70
    if conf_m:
        try:
            confidence = max(0, min(100, int(conf_m.group(1))))
        except ValueError:
            pass

    justification = "Clasificación automática"
    if just_m:
        justification = just_m.group(1).strip()

    return {
        "status":        "success",
        "category":      category,
        "confidence":    confidence,
        "justification": justification,
    }