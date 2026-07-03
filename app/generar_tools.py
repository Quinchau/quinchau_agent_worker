{
  "tools": [
    {
      "name": "intencion_compra",
      "description": "Cliente quiere comprar, consultar disponibilidad o consultar precio de un repuesto o accesorio",
      "parameters": {
        "type": "object",
        "properties": {
          "producto": {
            "type": "string",
            "description": "Producto o repuesto"
          },
          "modelo": {
            "type": "string",
            "description": "Modelo de la moto"
          },
          "cantidad": {
            "type": "integer",
            "description": "Cantidad de unidades"
          }
        },
        "required": [
          "producto",
          "modelo"
        ]
      }
    },
    {
      "name": "consulta_ubicacion_horario",
      "description": "Consulta sobre ubicación y horario de la tienda",
      "parameters": {
        "type": "object",
        "properties": {
          "sucursal": {
            "type": "string",
            "description": "Sucursal preferida"
          },
          "ubicacion": {
            "type": "string",
            "description": "Ubicación física"
          }
        },
        "required": []
      }
    },
    {
      "name": "reporte_incidente",
      "description": "Cliente reporta un incidente o daño en su moto",
      "parameters": {
        "type": "object",
        "properties": {
          "modelo": {
            "type": "string",
            "description": "Modelo de la moto"
          },
          "descripcion_dano": {
            "type": "string",
            "description": "Descripción del daño o incidente"
          },
          "fecha": {
            "type": "string",
            "description": "Fecha (incidente, cita, pedido)",
            "format": "date"
          }
        },
        "required": [
          "modelo"
        ]
      }
    },
    {
      "name": "agendar_cita",
      "description": "Cliente quiere agendar una cita de servicio técnico",
      "parameters": {
        "type": "object",
        "properties": {
          "tipo_servicio": {
            "type": "string",
            "description": "Tipo de servicio técnico"
          },
          "modelo": {
            "type": "string",
            "description": "Modelo de la moto"
          },
          "fecha": {
            "type": "string",
            "description": "Fecha (incidente, cita, pedido)",
            "format": "date"
          },
          "hora": {
            "type": "string",
            "description": "Hora o franja horaria",
            "format": "time"
          },
          "nombre_cliente": {
            "type": "string",
            "description": "Nombre del cliente"
          },
          "telefono_cliente": {
            "type": "string",
            "description": "Teléfono del cliente"
          },
          "sucursal": {
            "type": "string",
            "description": "Sucursal preferida"
          }
        },
        "required": [
          "tipo_servicio",
          "modelo",
          "fecha",
          "nombre_cliente",
          "telefono_cliente"
        ]
      }
    },
    {
      "name": "orden_sin_despacho",
      "description": "Cliente consulta por un pedido no despachado",
      "parameters": {
        "type": "object",
        "properties": {
          "nro_pedido": {
            "type": "string",
            "description": "Número de pedido"
          }
        },
        "required": [
          "nro_pedido"
        ]
      }
    },
    {
      "name": "intencion_compra_al_mayoreo",
      "description": "Cliente quiere comprar repuestos al por mayor o para taller",
      "parameters": {
        "type": "object",
        "properties": {},
        "required": []
      }
    },
    {
      "name": "intencion_saludo",
      "description": "Cliente agradece, saluda o inicia conversación sin intención comercial específica",
      "parameters": {
        "type": "object",
        "properties": {},
        "required": []
      }
    },
    {
      "name": "intencion_envio_por_delivery",
      "description": "Cliente pregunta si hacen envíos por delivery a su ubicación",
      "parameters": {
        "type": "object",
        "properties": {},
        "required": []
      }
    },
    {
      "name": "intencion_retiro_y_pago_personal",
      "description": "Cliente pregunta si puede retirar y pagar personalmente el pedido",
      "parameters": {
        "type": "object",
        "properties": {},
        "required": []
      }
    },
    {
      "name": "intencion_cotizar_envio",
      "description": "Cliente pregunta por el costo de envío a su ubicación",
      "parameters": {
        "type": "object",
        "properties": {},
        "required": []
      }
    }
  ]
}