"""Schemas JSON compartidos para el dominio Realtor (pending_reviews y cool_leads)."""

RAZONAMIENTO_SCHEMA = {
    "type": "string",
    "description": (
        "Análisis MUY CONCISO (máx. 3-4 oraciones). Debe explicar: "
        "1) Qué se solicitó explícitamente. "
        "2) Si existe una tarea abierta con el EXACTO MISMO PROPÓSITO (menciona su ID). "
        "3) Por qué se eligió crear o actualizar."
    ),
}

TEMPERATURA_SCHEMA = {
    "type": "string",
    "enum": ["Caliente", "Tibio", "Frio", "No interesado"],
    "description": (
        "Temperatura del lead según su intención de compra más reciente: "
        "Caliente (listo para actuar), Tibio (interesado sin urgencia), "
        "Frio (baja intención o inactivo), No interesado (rechazo explícito)."
    ),
}

NOTA_BITACORA_SCHEMA = {
    "type": "string",
    "description": (
        "Cuerpo de la bitácora para el historial del contacto en GHL: sintetiza ÚNICAMENTE "
        "los mensajes nuevos que se te muestran en la sección 'VENTANA DE MENSAJES NUEVOS'. "
        "NO tienes ni necesitas ver el historial previo, y no debes inventar ni asumir "
        "contexto anterior al que se te da aquí. Genera una entrada breve (1 a 3 renglones), "
        "en el formato exacto 'DD MES_ABREV_MAYUS YYYY <resumen breve>'. "
        "Las fechas deben salir de los timestamps reales, NUNCA inventadas. "
        "NO incluyas encabezado ni la fecha de hoy. NO repitas el 'razonamiento'."
    ),
}

HITO_RESUELTO_SCHEMA = {
    "type": "boolean",
    "description": (
        "Indica si el tema/objetivo tratado en la ventana de mensajes nuevos ya llegó a "
        "una resolución clara (ej.: el cliente completó un dato pendiente, confirmó una cita, "
        "dejó de responder tras una oferta cerrada). Devuelve true si está resuelto, false "
        "si el intercambio sigue evidentemente abierto (ej.: se hizo una pregunta y aún no "
        "hay respuesta). Solo es relevante evaluar esto cuando la señal de cierre es 'evaluar'."
    ),
}

# ---------------------------------------------------------------------------
# DEPRECADOS (se mantienen SIN CAMBIOS por compatibilidad con otros dominios,
# ej. cool_leads, que puedan seguir importándolos). pending_reviews ya NO usa
# este par — reemplazado por DIAS_OFFSET_SCHEMA / HORAS_OFFSET_SCHEMA más
# abajo, que expresan un plazo relativo a "ahora" en vez de una hora de reloj
# absoluta (evita que una tarea reagendada quede con un vencimiento pasado).
# ---------------------------------------------------------------------------
DIAS_PARA_VENCER_SCHEMA = {
    "type": "integer",
    "minimum": 0,
    "maximum": 14,
    "description": (
        "Cuántos días a partir de HOY debe ejecutarse esta tarea. 0 = hoy, 1 = mañana. "
        "Usa 0-1 para seguimientos urgentes. Usa un número mayor SOLO si el cliente "
        "pidió explícitamente más tiempo. Nunca calcules ni escribas una fecha absoluta."
    ),
}

HORA_LIMITE_SCHEMA = {
    "type": "string",
    "pattern": r"^([01][0-9]|2[0-3]):[0-5][0-9]$",
    "description": (
        "Hora del día (formato 24h HH:MM, hora de Miami) para ejecutar la tarea. "
        "1. Hora EXPLÍCITA del cliente convertida a 24h. "
        "2. Momento aproximado: 'mañana'→09:00, 'mediodía'→12:00, 'tarde'→15:00, 'noche'→18:00. "
        "3. Sin mención: Caliente→lo antes posible en horario laboral; Tibio/Frio→10:00. "
        "Nunca uses horarios fuera de 08:00-18:00 salvo petición explícita del cliente."
    ),
}

# ---------------------------------------------------------------------------
# NUEVOS: duración relativa a "ahora" (no a medianoche). Usados por
# pending_reviews desde la migración a agendamiento por offset. Al ser
# siempre una suma hacia adelante sobre el momento actual, es matemáticamente
# imposible que el sistema calcule una fecha de vencimiento en el pasado.
# ---------------------------------------------------------------------------
DIAS_OFFSET_SCHEMA = {
    "type": "integer",
    "minimum": 0,
    "maximum": 14,
    "description": (
        "Días completos a partir de AHORA MISMO (no de medianoche) para "
        "ejecutar la tarea. Se suma directamente a la hora actual junto con "
        "el campo de horas. 0 = el offset se cuenta solo en horas desde este "
        "momento. Nunca calcules ni escribas una fecha absoluta."
    ),
}

HORAS_OFFSET_SCHEMA = {
    "type": "integer",
    "minimum": 0,
    "maximum": 23,
    "description": (
        "Horas adicionales (0-23) a partir de AHORA MISMO, sumadas al campo "
        "de días. El resultado final es: AHORA + días + horas. Ejemplo: si "
        "son las 07:00 y corresponde vencer en 2 días y medio día más, usa "
        "días=2, horas=12 → vence en 2 días y 12 horas desde ahora (13 a las 19:00)."
    ),
}