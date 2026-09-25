# Praxis WhatsApp Agent

Agente de WhatsApp (ventas + servicio al estudiante) para Praxis English School, impulsado por
Claude (Anthropic) con tool-use. Responde consultas de cursos/horarios/profesores consultando en
vivo la API de Strapi (solo lectura), captura leads, envía documentos (brochures, temarios,
listas de precios) y escala a un asesor humano cuando corresponde.

## Stack

- **FastAPI** — servidor del webhook y API de administración.
- **Anthropic SDK (`anthropic`)** — agente con *tool calling* nativo (Claude decide cuándo
  consultar horarios, guardar un lead, enviar un documento o escalar).
- **WhatsApp Cloud API (Meta)** — canal oficial de mensajería (texto, listas interactivas,
  documentos).
- **SQLAlchemy 2.0 async + PostgreSQL + Alembic** — base de datos propia del agente
  (contactos, conversaciones, mensajes, leads, documentos). No escribe en el Postgres de Strapi.
- **httpx** — cliente hacia Strapi (solo lectura) y hacia la Graph API de Meta.

## Arquitectura

```
WhatsApp usuario  ──►  POST /webhook (Meta Cloud API)
                          │
                          ├─► guarda contacto/conversación/mensaje (Postgres propio)
                          ├─► Claude (tool-use) decide y ejecuta tools:
                          │       - get_class_schedules / get_teachers / get_courses ─► Strapi (solo lectura)
                          │       - list_documents / send_document ─► Postgres propio + Graph API
                          │       - save_lead ─► Postgres propio
                          │       - escalate_to_human ─► notifica a asesores por WhatsApp
                          └─► responde al usuario por WhatsApp
```

Contactos nuevos y escalaciones notifican automáticamente a los números en
`STAFF_NOTIFICATION_NUMBERS` (así se "redirigen" los mensajes de usuarios nuevos a un asesor,
sin dejar de responder de forma inmediata con el agente).

## Configuración inicial

### 1. Meta WhatsApp Cloud API

1. Crea una app en [developers.facebook.com](https://developers.facebook.com/) tipo "Business" y
   agrega el producto **WhatsApp**.
2. En *API Setup* obtén: `Phone number ID`, `WhatsApp Business Account ID` y un **token de acceso
   permanente** (system user token, no el temporal de 24h).
3. En *App Settings > Basic* copia el **App Secret** (`WHATSAPP_APP_SECRET`, se usa para validar
   la firma del webhook).
4. Inventa un `WHATSAPP_VERIFY_TOKEN` (cualquier string) y guárdalo también en `.env`.
5. Despliega el servicio (ver abajo) y en Meta configura el webhook:
   - Callback URL: `https://<tu-dominio-del-agente>/webhook`
   - Verify token: el mismo `WHATSAPP_VERIFY_TOKEN`
   - Suscríbete al campo `messages`.

### 2. Variables de entorno

```bash
cp .env.example .env
# completa ANTHROPIC_API_KEY, WHATSAPP_*, STRAPI_API_TOKEN, ADMIN_API_KEY, STAFF_NOTIFICATION_NUMBERS
```

`STRAPI_API_TOKEN`: en el admin de Strapi (`Settings > API Tokens`) crea uno de tipo **Read-only**
— el agente nunca debe tener permisos de escritura sobre Strapi.

### 3. Levantar en desarrollo

Requiere que la red `praxis_default` ya exista (se crea al levantar el stack principal en
`/srv/praxis`), para que el agente pueda resolver `http://strapi:1337`.

```bash
docker compose up --build
```

La primera vez, el contenedor corre `alembic upgrade head` automáticamente antes de arrancar
`uvicorn`.

### 4. Subir documentos que el agente puede enviar

```bash
curl -X POST http://localhost:8000/admin/documents \
  -H "X-API-Key: $ADMIN_API_KEY" \
  -F "title=Brochure Nivel Básico" \
  -F "category=brochure" \
  -F "file=@/ruta/brochure.pdf"
```

El agente lo verá disponible vía la tool `list_documents` y podrá enviarlo con `send_document`
cuando el usuario lo pida.

### 5. Consultar leads capturados

```bash
curl http://localhost:8000/admin/leads -H "X-API-Key: $ADMIN_API_KEY"
```

### 6. Pago de cuotas con Wompi

Opción **💳 Pagar cuota** del menú (o escribiendo "quiero pagar mi cuota"):

1. El agente pide **cédula**, **número de contrato** y **número de cuenta** y crea en Wompi un
   link de pago de un solo uso **sin monto** (`amount_in_cents` omitido): el estudiante escribe
   el valor de la cuota en el checkout (`https://checkout.wompi.co/l/<id>`).
2. Wompi envía `transaction.updated` a `POST /payments/wompi/events`. Se valida el checksum
   (`SHA256(propiedades + timestamp + WOMPI_EVENTS_SECRET)`), se consulta la transacción en
   `GET /v1/transactions/{id}` y se guarda como evidencia en `payment_events` (payload crudo +
   transacción verificada). El estado final queda en `payment_requests`.
3. Si el pago queda aprobado, el estudiante recibe el comprobante por WhatsApp (valor, medio,
   ID de transacción, referencia) y se avisa a `STAFF_NOTIFICATION_NUMBERS`. Además se genera el comprobante como **imagen PNG**,
   se guarda en `storage/documents/receipts/<referencia>.png` y se envía a
   `PAYMENT_RECEIPT_NUMBERS` (número de servicio). Si es rechazado,
   se le reenvía el link para reintentar.

Configuración:

- Completa las variables `WOMPI_*` de `.env` (llaves del mismo ambiente que `WOMPI_ENVIRONMENT`).
- En el dashboard de Wompi > Desarrolladores, configura la **URL de eventos**:
  `https://wa.academiapraxis.com/payments/wompi/events` (una por ambiente, sandbox y producción).
- Sin `WOMPI_PRIVATE_KEY` y `WOMPI_EVENTS_SECRET` la opción queda deshabilitada y el agente
  ofrece un asesor.

Consultar pagos y evidencia:

```bash
curl "http://localhost:8000/admin/payments?payment_status=approved" -H "X-API-Key: $ADMIN_API_KEY"
curl http://localhost:8000/admin/payments/<id> -H "X-API-Key: $ADMIN_API_KEY"   # incluye eventos crudos
```

Nota: el agente no valida la cédula, el contrato ni la cuenta contra Strapi (no tiene acceso a
contratos); los datos quedan guardados junto al pago para que el área administrativa concilie.

## Integración a producción

Esta plataforma NO despliega Strapi ni el frontend desde un `docker-compose.yml` raíz único:
cada app vive en su propia carpeta (`praxis-backend-academia/`, `praxis-academia-front/`), cada
una con su propio `docker-compose.yml` y su propio proyecto de Docker Compose, unidas entre sí
por la red **externa** `praxis_default` (creada por el proyecto `praxis` que corre Traefik en
`/srv/praxis`). El agente de WhatsApp sigue exactamente el mismo patrón: vive aquí, en
`praxis-whatsapp-agent/`, con su propio `docker-compose.yml` ya configurado con las labels de
Traefik para `wa.academiapraxis.com` — no requiere tocar ningún otro `docker-compose.yml`.

Pasos para desplegar:

1. `cp .env.example .env` y completa las credenciales reales (ver secciones anteriores).
2. Confirma que la red externa ya existe (la crea el proyecto `praxis` de Traefik):
   ```bash
   docker network inspect praxis_default
   ```
3. Levanta el servicio:
   ```bash
   cd /srv/praxis/praxis-whatsapp-agent
   docker compose up -d --build
   ```
   Traefik lo descubre automáticamente por las labels (igual que hace con `strapi` y `web`) y
   emite el certificado TLS para `wa.academiapraxis.com` vía Let's Encrypt.
4. Apunta el DNS de `wa.academiapraxis.com` (registro A) a la IP del servidor — este paso es
   manual, fuera de Docker.
5. En Meta, configura el webhook con `https://wa.academiapraxis.com/webhook`.

`STRAPI_BASE_URL=http://strapi:1337` funciona porque el contenedor `strapi` (definido en
`praxis-backend-academia/docker-compose.yml`) también está unido a `praxis_default` y Docker
resuelve su nombre de servicio como hostname dentro de esa red — se verificó en vivo que
`strapi` resuelve correctamente desde otros contenedores de esa red.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

## Notas de seguridad

- El webhook valida `X-Hub-Signature-256` con `WHATSAPP_APP_SECRET`; peticiones sin firma válida
  se rechazan con 401.
- Los endpoints `/admin/*` requieren header `X-API-Key` (`ADMIN_API_KEY`).
- `/payments/wompi/events` solo acepta eventos con checksum válido (`WOMPI_EVENTS_SECRET`) y el
  estado del pago se toma de la API de Wompi, no solo del evento recibido.
- El cliente de Strapi es de solo lectura; el agente no puede crear ni modificar contratos,
  facturas ni datos de personas. Preguntas sobre contratos/facturas se escalan a un humano.
