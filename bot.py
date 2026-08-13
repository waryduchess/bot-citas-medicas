import logging
import io
import json
import sqlite3
import os
import asyncio
import base64
import httpx
import calendar
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from PIL import Image

from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ---------------------------------------------------------------------------
# CONFIGURACIÓN Y VARIABLES DE ENTORNO
# ---------------------------------------------------------------------------
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL = "qwen/qwen3.6-27b"

# Zona horaria local
TZ_LOCAL = ZoneInfo("America/Cancun")

# Anticipaciones de recordatorio: (delta, etiqueta de mensaje)
ANTICIPACIONES = [
    (timedelta(days=7), "1 semana antes"),
    (timedelta(days=1), "1 día antes"),
    (timedelta(hours=2), "2 horas antes"),
    (timedelta(hours=1), "1 hora antes"),
    (timedelta(0), "Es hora de tu cita"),
]

# Meses y días para formatear fechas en español
MESES_ES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 5: "Mayo", 6: "Junio",
    7: "Julio", 8: "Agosto", 9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
}
DIAS_ES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# Programador de tareas asíncrono
scheduler = AsyncIOScheduler(timezone=TZ_LOCAL)

def formatear_fecha(dt: datetime) -> str:
    """Formatea una fecha como 'Martes, 27 de Octubre de 2026' en español."""
    return f"{DIAS_ES[dt.weekday()]}, {dt.day} de {MESES_ES[dt.month]} de {dt.year}"

# ---------------------------------------------------------------------------
# BASE DE DATOS (SQLite)
# ---------------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect("citas.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS citas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            servicio TEXT,
            fecha_hora TEXT,
            colaborador TEXT
        )
    """)
    conn.commit()
    conn.close()

def guardar_cita(chat_id, servicio, fecha_hora, colaborador):
    """Guarda una cita. Devuelve True si se insertó, False si ya existía (duplicada)."""
    conn = sqlite3.connect("citas.db")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM citas WHERE chat_id = ? AND servicio = ? AND fecha_hora = ?",
        (chat_id, servicio, fecha_hora)
    )
    if cursor.fetchone():
        conn.close()
        return False
    cursor.execute(
        "INSERT INTO citas (chat_id, servicio, fecha_hora, colaborador) VALUES (?, ?, ?, ?)",
        (chat_id, servicio, fecha_hora, colaborador)
    )
    conn.commit()
    conn.close()
    return True

# ---------------------------------------------------------------------------
# FUNCIONES DE RECORDATORIO Y RECARGA
# ---------------------------------------------------------------------------
async def enviar_recordatorio(app, chat_id, servicio, hora, colaborador, anticipacion):
    """Mensaje que se enviará al usuario según la anticipación de la cita"""
    textos = {
        "1 semana antes": (
            f"📅 **¡Falta 1 semana para tu cita!**\n\n"
            f"🩺 **Servicio:** {servicio}\n"
            f"🕒 **Hora:** {hora}\n"
            f"👤 **Especialista:** {colaborador}"
        ),
        "1 día antes": (
            f"📅 **¡Mañana tienes tu cita!**\n\n"
            f"🩺 **Servicio:** {servicio}\n"
            f"🕒 **Hora:** {hora}\n"
            f"👤 **Especialista:** {colaborador}"
        ),
        "2 horas antes": (
            f"⏰ **¡Tu cita es en 2 horas!**\n\n"
            f"🩺 **Servicio:** {servicio}\n"
            f"🕒 **Hora:** {hora}\n"
            f"👤 **Especialista:** {colaborador}"
        ),
        "1 hora antes": (
            f"⏰ **¡Tu cita es en 1 hora!**\n\n"
            f"🩺 **Servicio:** {servicio}\n"
            f"🕒 **Hora:** {hora}\n"
            f"👤 **Especialista:** {colaborador}"
        ),
        "Es hora de tu cita": (
            f"🩺 **¡ES HORA DE TU CITA!**\n\n"
            f"**Servicio:** {servicio}\n"
            f"**Hora:** {hora}\n"
            f"**Especialista:** {colaborador}"
        ),
    }
    mensaje = textos.get(anticipacion, textos["Es hora de tu cita"])
    await app.bot.send_message(chat_id=chat_id, text=mensaje, parse_mode='Markdown')

def programar_recordatorios(app, chat_id, citas_json):
    """Parsea el JSON extraído, almacena en BD y programa alertas en el Scheduler"""
    mapeo_meses = {
        'Ene': 1, 'Feb': 2, 'Mar': 3, 'Abr': 4, 'May': 5, 'Jun': 6,
        'Jul': 7, 'Ago': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dic': 12,
        'Jan': 1, 'Apr': 4, 'Aug': 8, 'Dec': 12, 'Agosto': 8,
    }

    ahora = datetime.now(TZ_LOCAL)
    registradas = 0
    duplicadas = 0

    for cita in citas_json:
        try:
            partes_fecha = cita['fecha'].split()[-1].split('/')
            dia = int(partes_fecha[0])
            mes_str = partes_fecha[1].capitalize()
            anio = int(partes_fecha[2])

            mes = mapeo_meses.get(mes_str, 1)
            hora, minuto = map(int, cita['hora'].split(':'))

            fecha_cita = datetime(anio, mes, dia, hora, minuto, tzinfo=TZ_LOCAL)

            if not guardar_cita(chat_id, cita['servicio'], fecha_cita.strftime("%Y-%m-%d %H:%M"), cita['colaborador']):
                duplicadas += 1
                continue
            registradas += 1

            for delta, etiqueta in ANTICIPACIONES:
                fecha_recordatorio = fecha_cita - delta
                if fecha_recordatorio > ahora:
                    scheduler.add_job(
                        enviar_recordatorio,
                        'date',
                        run_date=fecha_recordatorio,
                        args=[app, chat_id, cita['servicio'], cita['hora'], cita['colaborador'], etiqueta]
                    )
        except Exception as e:
            logging.error(f"No se pudo programar la cita {cita}: {e}")

    logging.info(f"📥 Citas procesadas: {registradas} nuevas, {duplicadas} duplicadas.")
    return registradas, duplicadas

def reanalizar_y_cargar_citas_pendientes(app):
    """Recarga las citas de SQLite al Scheduler en caso de reinicio del bot"""
    conn = sqlite3.connect("citas.db")
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id, servicio, fecha_hora, colaborador FROM citas")
    filas = cursor.fetchall()
    conn.close()

    ahora = datetime.now(TZ_LOCAL)
    cargadas = 0

    for chat_id, servicio, fecha_hora_str, colaborador in filas:
        try:
            dt_naive = datetime.strptime(fecha_hora_str, "%Y-%m-%d %H:%M")
            fecha_cita = dt_naive.replace(tzinfo=TZ_LOCAL)

            for delta, etiqueta in ANTICIPACIONES:
                fecha_recordatorio = fecha_cita - delta
                if fecha_recordatorio > ahora:
                    hora_str = fecha_cita.strftime("%H:%M")
                    scheduler.add_job(
                        enviar_recordatorio,
                        'date',
                        run_date=fecha_recordatorio,
                        args=[app, chat_id, servicio, hora_str, colaborador, etiqueta]
                    )
                    cargadas += 1
        except Exception as e:
            logging.error(f"Error recargando cita pendiente: {e}")

    logging.info(f"🔄 Se restauraron {cargadas} recordatorios pendientes desde la BD.")

# ---------------------------------------------------------------------------
# HOOK POST-INIT (Solución para el RuntimeError de asyncio)
# ---------------------------------------------------------------------------
async def post_init(application):
    """Se ejecuta cuando el event loop de asyncio ya está corriendo activamente"""
    reanalizar_y_cargar_citas_pendientes(application)
    scheduler.start()
    logging.info("🚀 Scheduler de recordatorios iniciado correctamente.")

# ---------------------------------------------------------------------------
# HANDLERS DE TELEGRAM
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = (
        "¡Hola! 👋 Soy tu asistente de citas médicas.\n\n"
        "📸 **Envía una captura de pantalla** de tu agenda y guardaré tus citas.\n\n"
        "**Comandos:**\n"
        "🔍 /citas — ver todas tus citas\n"
        "📅 /semana — ver las citas de esta semana\n\n"
        "Te recordaré tus citas **1 semana antes, 1 día antes, 2 horas antes, 1 hora antes y a la hora exacta**."
    )
    await update.message.reply_text(texto, parse_mode='Markdown')

def consultar_citas(chat_id, desde=None, hasta=None):
    """Consulta citas de un chat, opcionalmente en un rango de fechas."""
    conn = sqlite3.connect("citas.db")
    cursor = conn.cursor()
    query = "SELECT servicio, fecha_hora, colaborador FROM citas WHERE chat_id = ?"
    params = [chat_id]
    if desde:
        query += " AND fecha_hora >= ?"
        params.append(desde)
    if hasta:
        query += " AND fecha_hora < ?"
        params.append(hasta)
    query += " ORDER BY fecha_hora ASC"
    cursor.execute(query, params)
    filas = cursor.fetchall()
    conn.close()
    return filas

def formatear_lista_citas(filas):
    """Convierte filas de BD en texto legible para Telegram."""
    if not filas:
        return None
    lineas = []
    for servicio, fecha_hora_str, colaborador in filas:
        dt = datetime.strptime(fecha_hora_str, "%Y-%m-%d %H:%M").replace(tzinfo=TZ_LOCAL)
        lineas.append(
            f"🩺 **{servicio}**\n"
            f"📆 {formatear_fecha(dt)}\n"
            f"🕒 {dt.strftime('%H:%M')}\n"
            f"👤 {colaborador}"
        )
    return "\n\n".join(lineas)

async def mostrar_citas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ahora = datetime.now(TZ_LOCAL).strftime("%Y-%m-%d %H:%M")
    filas = consultar_citas(chat_id, desde=ahora)
    texto = formatear_lista_citas(filas)
    if texto is None:
        await update.message.reply_text("📭 No tienes citas futuras registradas.")
    else:
        await update.message.reply_text(
            f"🗓 **Tus citas registradas:**\n\n{texto}",
            parse_mode='Markdown'
        )

async def mostrar_citas_semana(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ahora = datetime.now(TZ_LOCAL)
    inicio_semana = ahora - timedelta(days=ahora.weekday())
    inicio_semana = inicio_semana.replace(hour=0, minute=0, second=0, microsecond=0)
    fin_semana = inicio_semana + timedelta(days=7)

    filas = consultar_citas(
        chat_id,
        desde=inicio_semana.strftime("%Y-%m-%d %H:%M"),
        hasta=fin_semana.strftime("%Y-%m-%d %H:%M"),
    )
    texto = formatear_lista_citas(filas)
    if texto is None:
        await update.message.reply_text("📭 No tienes citas esta semana.")
    else:
        await update.message.reply_text(
            f"📅 **Citas de esta semana ({inicio_semana.day}/{inicio_semana.month} – {inicio_semana.day + 6}/{inicio_semana.month}):**\n\n{texto}",
            parse_mode='Markdown'
        )

async def procesar_imagen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_espera = await update.message.reply_text("⏳ Procesando imagen y programando recordatorios...")
    
    try:
        foto = update.message.photo[-1]
        archivo = await context.bot.get_file(foto.file_id)
        
        byte_stream = io.BytesIO()
        await archivo.download_to_memory(byte_stream)
        byte_stream.seek(0)
        imagen_pil = Image.open(byte_stream)

        # Redimensionar para reducir tokens de imagen en el tier gratuito
        imagen_pil = imagen_pil.convert("RGB")
        ancho_max = 1280
        if imagen_pil.width > ancho_max:
            ratio = ancho_max / float(imagen_pil.width)
            nuevo_alto = int(imagen_pil.height * ratio)
            imagen_pil = imagen_pil.resize((ancho_max, nuevo_alto), Image.LANCZOS)

        prompt = """
        Extrae TODAS las citas de la tabla 'Agenda preliminar' en un arreglo JSON de objetos.
        IMPORTANTE: Revisa la imagen fila por fila. NO omitas ninguna cita.
        Si la tabla tiene 8 filas, el arreglo debe tener exactamente 8 objetos.
        Campos requeridos por cada objeto:
        - "fecha" (ejemplo: "Mar 27/Oct/2026")
        - "hora" (ejemplo: "08:20")
        - "servicio" (ejemplo: "PF Psicología Familiar B")
        - "sesion" (ejemplo: "5 de 5")
        - "colaborador" (ejemplo: "Fanny Celeste")
        """

        # Convertir la imagen a base64 para Groq (data URL)
        buffer = io.BytesIO()
        imagen_pil.save(buffer, format="PNG")
        imagen_b64 = base64.b64encode(buffer.getvalue()).decode()

        # Llamada a Groq con reintentos por 429/503
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": GROQ_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{imagen_b64}"}},
                    ],
                }
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }

        respuesta = None
        for intento in range(3):
            try:
                r = httpx.post(
                    f"{GROQ_BASE_URL}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=60,
                )
                r.raise_for_status()
                respuesta = r.json()
                break
            except httpx.HTTPStatusError as e:
                codigo = e.response.status_code
                logging.warning(f"Intento {intento + 1}/3 falló (código {codigo}): {e}")
                if codigo in (503, 429) and intento < 2:
                    await asyncio.sleep(2 * (intento + 1))
                else:
                    raise
            except httpx.HTTPError as e:
                codigo = None
                logging.warning(f"Intento {intento + 1}/3 falló (error de red): {e}")
                if intento < 2:
                    await asyncio.sleep(2 * (intento + 1))
                else:
                    raise
        assert respuesta is not None

        contenido = respuesta["choices"][0]["message"]["content"]
        citas_json = json.loads(contenido.strip())

        chat_id = update.effective_chat.id
        registradas, duplicadas = programar_recordatorios(context.application, chat_id, citas_json)

        await context.bot.delete_message(chat_id=chat_id, message_id=msg_espera.message_id)
        
        texto_confirmacion = (
            f"✅ **Se han registrado {registradas} citas con éxito.**\n"
            f"🔔 Te avisaré **1 semana antes, 1 día antes, 2 horas antes, 1 hora antes y a la hora exacta**."
        )
        if duplicadas:
            texto_confirmacion += f"\n\n⚠️ {duplicadas} cita(s) ya estaban registradas y se ignoraron."
        await update.message.reply_text(texto_confirmacion, parse_mode='Markdown')

    except httpx.HTTPStatusError as e:
        codigo = e.response.status_code
        logging.error(f"Error procesando la imagen (código {codigo}): {e}")
        if codigo == 404:
            await update.message.reply_text("❌ El modelo de IA no está disponible. Contacta al administrador.")
        elif codigo == 429:
            await update.message.reply_text("❌ La cuota de la IA se agotó. Intenta de nuevo más tarde.")
        elif codigo == 503:
            await update.message.reply_text("❌ La IA está saturada. Intenta de nuevo en unos minutos.")
        else:
            await update.message.reply_text(f"❌ Ocurrió un error de IA (código {codigo}). Asegúrate de enviar una captura clara.")
    except Exception as e:
        logging.error(f"Error procesando la imagen: {e}")
        await update.message.reply_text("❌ Ocurrió un error al procesar las citas. Asegúrate de enviar una captura clara.")

# ---------------------------------------------------------------------------
# INICIALIZACIÓN
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    init_db()
    
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("citas", mostrar_citas))
    app.add_handler(CommandHandler("semana", mostrar_citas_semana))
    app.add_handler(MessageHandler(filters.PHOTO, procesar_imagen))

    print("🤖 Bot iniciado y listo para recibir capturas...")
    app.run_polling()