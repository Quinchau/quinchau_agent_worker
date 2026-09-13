"""
Utilidades de fecha/hora y reglas de negocio de agendamiento, compartidas
entre las tools de pending_reviews (y reutilizables por cool_leads si aplica
en el futuro — este archivo es nuevo, no reemplaza nada existente).
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

try:
    MIAMI_TZ = ZoneInfo("America/New_York")
except Exception:
    from datetime import timezone
    MIAMI_TZ = timezone(timedelta(hours=-4))

MESES_ABREV_ES = {
    1: "ENE", 2: "FEB", 3: "MAR", 4: "ABR", 5: "MAY", 6: "JUN",
    7: "JUL", 8: "AGO", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DIC",
}

DIAS_SEMANA_ES = {
    0: "LUNES", 1: "MARTES", 2: "MIÉRCOLES", 3: "JUEVES",
    4: "VIERNES", 5: "SÁBADO", 6: "DOMINGO",
}

# Piso mínimo de días (relativos a AHORA) según temperatura del lead.
# Aplica solo cuando el plazo se decide por defecto, no cuando el cliente
# pidió explícitamente una fecha/hora concreta (ver aplicar_piso_temperatura).
TEMPERATURA_MIN_DIAS = {
    "Caliente": 0,
    "Tibio": 3,
    "Frio": 7,
    "No interesado": 0,
}

# Jornada laboral: 08:00 a 20:00
JORNADA_INICIO = 8   # 08:00
JORNADA_FIN = 20     # 20:00


def formatear_fecha_hoy(dt: datetime) -> str:
    """Ej: 'JUEVES 08 SEP 2026'."""
    return f"{DIAS_SEMANA_ES[dt.weekday()]} {dt.day:02d} {MESES_ABREV_ES[dt.month]} {dt.year}"


def formatear_fecha_bitacora(dt: datetime) -> str:
    """Ej: '01 SEP 2026'."""
    return f"{dt.day:02d} {MESES_ABREV_ES[dt.month]} {dt.year}"


def _ajustar_a_horario_laboral(fecha_objetivo: datetime) -> datetime:
    """
    Si la fecha cae fuera de la jornada laboral (08:00-20:00), la empuja
    al siguiente bloque laboral disponible:
    - Si hora < 08:00 → 08:00 de ese mismo día
    - Si hora >= 20:00 → 08:00 del siguiente día hábil (saltando fines de semana)
    """
    hora = fecha_objetivo.hour
    
    if hora < JORNADA_INICIO:
        # Antes de la jornada: mover a 08:00 de ese día
        return fecha_objetivo.replace(hour=JORNADA_INICIO, minute=0, second=0, microsecond=0)
    
    elif hora >= JORNADA_FIN:
        # Después de la jornada: mover a 08:00 del siguiente día hábil
        siguiente_dia = (fecha_objetivo + timedelta(days=1)).replace(
            hour=JORNADA_INICIO, minute=0, second=0, microsecond=0
        )
        # Saltar fin de semana (sábado=5, domingo=6)
        while siguiente_dia.weekday() >= 5:
            siguiente_dia += timedelta(days=1)
        return siguiente_dia
    
    else:
        # Dentro de la jornada: no ajustar
        return fecha_objetivo


def calcular_due_date(dias_para_vencer: int, horas_para_vencer: int, ahora_miami: datetime) -> str:
    """
    Fecha límite ISO8601, calculada como AHORA + offset puro.
    Si el resultado cae fuera del horario laboral (08:00-20:00), lo empuja
    al siguiente bloque laboral disponible.
    """
    fecha_objetivo = ahora_miami + timedelta(days=dias_para_vencer, hours=horas_para_vencer)
    
    # Ajustar si cayó fuera de horario laboral
    fecha_ajustada = _ajustar_a_horario_laboral(fecha_objetivo)
    
    if fecha_ajustada != fecha_objetivo:
        logger.warning(
            f"⚠️ due_date calculado ({fecha_objetivo.isoformat()}) cae fuera de horario laboral. "
            f"Ajustando a {fecha_ajustada.isoformat()} | "
            f"jornada={JORNADA_INICIO:02d}:00-{JORNADA_FIN:02d}:00"
        )
    
    return fecha_ajustada.isoformat()


def formatear_due_date_ghl_webhook(dias_para_vencer: int, horas_para_vencer: int, ahora_miami: datetime) -> str:
    """Igual que calcular_due_date pero en el formato 'MM-DD-YYYY HH:MM AM/PM' que espera el webhook/Workflow de GHL."""
    iso = calcular_due_date(dias_para_vencer, horas_para_vencer, ahora_miami)
    dt = datetime.fromisoformat(iso)
    return dt.strftime("%m-%d-%Y %I:%M %p")


def aplicar_piso_temperatura(
    dias_para_vencer: int,
    temperatura: str,
    contact_name: str,
    campo: str = "dias_para_vencer",
) -> int:
    """
    Fuerza el mínimo de días según temperatura (TEMPERATURA_MIN_DIAS).
    No confía en que el LLM haya respetado la regla del prompt: esta es la
    última línea de defensa, server-side.

    NOTA DE DISEÑO: hoy este piso es un mínimo DURO siempre, sin excepción
    por pedido explícito del cliente (ej. un lead Frio que pide "llámenme
    mañana" quedaría igual en el piso de 7 días). Si se quiere respetar un
    pedido explícito del cliente por debajo del piso, hace falta que el LLM
    indique esa intención con un campo booleano nuevo (ej.
    `plazo_explicito_del_cliente`) y que este helper lo reciba para omitir
    el clamp en ese caso. Se deja pendiente hasta definir esa regla de negocio.
    """
    piso = TEMPERATURA_MIN_DIAS.get(temperatura, 0)
    if dias_para_vencer < piso:
        logger.warning(
            f"⚠️ {campo}={dias_para_vencer} por debajo del piso ({piso}) "
            f"para temperatura={temperatura!r} | {contact_name}. Ajustando a {piso}."
        )
        return piso
    return dias_para_vencer


def construir_nota_bitacora(cuerpo: str, ahora_miami: datetime) -> str:
    header = f"Bitacora realizada por AI el {formatear_fecha_bitacora(ahora_miami)}"
    return f"{header}\n\n{cuerpo}" if cuerpo else header