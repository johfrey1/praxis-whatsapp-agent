"""Crea en Meta las plantillas de WhatsApp para pagos con Wompi.

- pago_cuota: recordatorio con botón "Pagar cuota" (URL dinámica checkout.wompi.co/l/{{1}}).
- comprobante_pago: comprobante con imagen para el número de servicio (fuera de la ventana de 24 h).

- solicitud_estudiante: solicitud de un estudiante (cédula, teléfono, correo y petición) para el
  número de atención a estudiantes (fuera de la ventana de 24 h).

Uso (dentro del contenedor, donde están las variables de entorno). Sin argumentos crea todas;
con nombres, solo esas:
    docker compose exec -T whatsapp-agent python - solicitud_estudiante < scripts/create_payment_templates.py
"""

import io
import os
import sys

import httpx
from PIL import Image, ImageDraw

TOKEN = os.environ["WHATSAPP_ACCESS_TOKEN"]
WABA_ID = os.environ.get("WHATSAPP_BUSINESS_ACCOUNT_ID", "")
GRAPH = f"https://graph.facebook.com/{os.environ.get('WHATSAPP_GRAPH_API_VERSION', 'v21.0')}"

if not WABA_ID:
    sys.exit("Falta WHATSAPP_BUSINESS_ACCOUNT_ID en el entorno")

client = httpx.Client(timeout=30)
auth = {"Authorization": f"Bearer {TOKEN}"}


def sample_png() -> bytes:
    img = Image.new("RGB", (800, 418), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 800, 80], fill=(20, 60, 120))
    draw.text((30, 30), "Praxis School - Comprobante de pago", fill="white")
    draw.text((30, 140), "Pago APROBADO  $150.000", fill="black")
    draw.text((30, 190), "Contrato 12345 - Cuenta 001", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def upload_header_handle(data: bytes) -> str:
    """Sube la imagen de ejemplo con la Resumable Upload API y devuelve el header_handle."""
    debug = client.get(f"{GRAPH}/debug_token", params={"input_token": TOKEN, "access_token": TOKEN}).json()
    app_id = debug.get("data", {}).get("app_id")
    if not app_id:
        sys.exit(f"No pude obtener app_id del token: {debug}")
    session = client.post(
        f"{GRAPH}/{app_id}/uploads",
        params={"file_name": "comprobante.png", "file_length": len(data), "file_type": "image/png"},
        headers=auth,
    ).json()
    if "id" not in session:
        sys.exit(f"Error creando sesión de subida: {session}")
    result = client.post(
        f"{GRAPH}/{session['id']}",
        headers={"Authorization": f"OAuth {TOKEN}", "file_offset": "0"},
        content=data,
    ).json()
    if "h" not in result:
        sys.exit(f"Error subiendo imagen de ejemplo: {result}")
    return result["h"]


def create(template: dict) -> None:
    resp = client.post(f"{GRAPH}/{WABA_ID}/message_templates", headers=auth, json=template)
    print(f"[{template['name']}] HTTP {resp.status_code}: {resp.text}")


def pago_cuota() -> dict:
    return {
        "name": "pago_cuota",
        "language": "es",
        "category": "UTILITY",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "Pago de cuota"},
            {
                "type": "BODY",
                "text": (
                    "Hola {{1}}, tu cuota del contrato {{2}} (cuenta {{3}}) está lista para pagar.\n"
                    "Toca el botón, escribe el valor y elige Nequi, PSE, tarjeta, Bancolombia o Daviplata."
                ),
                "example": {"body_text": [["Juan", "12345", "001"]]},
            },
            {"type": "FOOTER", "text": "Praxis School · Pago seguro con Wompi"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL",
                        "text": "Pagar cuota",
                        "url": "https://checkout.wompi.co/l/{{1}}",
                        "example": ["https://checkout.wompi.co/l/test_AbC123"],
                    }
                ],
            },
        ],
    }

def comprobante_pago() -> dict:
    return {
        "name": "comprobante_pago",
        "language": "es",
        "category": "UTILITY",
        "components": [
            {"type": "HEADER", "format": "IMAGE", "example": {"header_handle": [upload_header_handle(sample_png())]}},
            {
                "type": "BODY",
                "text": "Pago de cuota APROBADO {{1}} · contrato {{2}} · cédula {{3}} · ref {{4}}. Comprobante generado por Praxis School.",
                "example": {"body_text": [["$150.000", "12345", "1020304050", "PRX-ABC123"]]},
            },
        ],
    }


def solicitud_estudiante() -> dict:
    return {
        "name": "solicitud_estudiante",
        "language": "es",
        "category": "UTILITY",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "Solicitud de estudiante"},
            {
                "type": "BODY",
                "text": (
                    "Llegó una nueva solicitud de estudiante por WhatsApp.\n"
                    "Tipo: {{1}}\n"
                    "Nombre: {{2}}\n"
                    "Cédula: {{3}}\n"
                    "Teléfono: {{4}}\n"
                    "Correo: {{5}}\n"
                    "WhatsApp: {{6}}\n"
                    "Petición: {{7}}\n"
                    "Por favor gestiónala y contacta al estudiante."
                ),
                "example": {
                    "body_text": [
                        [
                            "Paz y salvo",
                            "Ana Pérez",
                            "1020304050",
                            "+573001112233",
                            "ana@example.com",
                            "+573001112233",
                            "Necesito el paz y salvo del contrato 12345",
                        ]
                    ]
                },
            },
            {"type": "FOOTER", "text": "Praxis School · Atención a estudiantes"},
        ],
    }


TEMPLATES = {f.__name__: f for f in (pago_cuota, comprobante_pago, solicitud_estudiante)}

for template_name in sys.argv[1:] or list(TEMPLATES):
    create(TEMPLATES[template_name]())
