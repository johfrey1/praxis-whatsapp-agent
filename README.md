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
- El cliente de Strapi es de solo lectura; el agente no puede crear ni modificar contratos,
  facturas ni datos de personas. Preguntas sobre contratos/facturas se escalan a un humano.
