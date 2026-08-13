import logging
import io
import json
import sqlite3
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from PIL import Image

# Nueva SDK oficial de Google GenAI
from google import genai
from google.genai import types

from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ---------------------------------------------------------------------------
# CONFIGURACIÓN Y VARIABLES DE ENTORNO
# ---------------------------------------------------------------------------
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Zona horaria local
TZ_LOCAL = ZoneInfo("America/Cancun")

# Inicializar cliente de la nueva API de Google GenAI
client = genai.Client(api_key=GEMINI_API_KEY)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# Programador de tareas asíncrono
scheduler = AsyncIOScheduler(timezone=TZ_LOCAL)

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
    conn = sqlite3.connect("citas.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO citas (chat_id, servicio, fecha_hora, colaborador) VALUES (?, ?, ?, ?)",
        (chat_id, servicio, fecha_hora, colaborador)
    )
    conn.commit()
    conn.close()

# ---------------------------------------------------------------------------
# FUNCIONES DE RECORDATORIO Y RECARGA
# ---------------------------------------------------------------------------
async def enviar_recordatorio(app, chat_id, servicio, hora, colaborador):
    """Mensaje que se enviará al usuario cuando llegue la hora agendada"""
    mensaje = (
        f"⏰ **¡RECORDATORIO DE CITA!**\n\n"
        f"🩺 **Servicio:** {servicio}\n"
        f"🕒 **Hora:** {hora}\n"
        f"👤 **Especialista:** {colaborador}"
    )
    await app.bot.send_message(chat_id=chat_id, text=mensaje, parse_mode='Markdown')

def programar_recordatorios(app, chat_id, citas_json):
    """Parsea el JSON extraído, almacena en BD y programa alertas en el Scheduler"""
    mapeo_meses = {
        'Ene': 1, 'Feb': 2, 'Mar': 3, 'Abr': 4, 'May': 5, 'Jun': 6,
        'Jul': 7, 'Ago': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dic': 12
    }

    ahora = datetime.now(TZ_LOCAL)

    for cita in citas_json:
        try:
            partes_fecha = cita['fecha'].split()[-1].split('/')
            dia = int(partes_fecha[0])
            mes_str = partes_fecha[1].capitalize()
            anio = int(partes_fecha[2])
            
            mes = mapeo_meses.get(mes_str, 1)
            hora, minuto = map(int, cita['hora'].split(':'))

            fecha_cita = datetime(anio, mes, dia, hora, minuto, tzinfo=TZ_LOCAL)
            fecha_recordatorio = fecha_cita - timedelta(hours=2)

            guardar_cita(chat_id, cita['servicio'], fecha_cita.strftime("%Y-%m-%d %H:%M"), cita['colaborador'])

            if fecha_recordatorio > ahora:
                scheduler.add_job(
                    enviar_recordatorio,
                    'date',
                    run_date=fecha_recordatorio,
                    args=[app, chat_id, cita['servicio'], cita['hora'], cita['colaborador']]
                )
        except Exception as e:
            logging.error(f"No se pudo programar la cita {cita}: {e}")

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
            fecha_recordatorio = fecha_cita - timedelta(hours=2)

            if fecha_recordatorio > ahora:
                hora_str = fecha_cita.strftime("%H:%M")
                scheduler.add_job(
                    enviar_recordatorio,
                    'date',
                    run_date=fecha_recordatorio,
                    args=[app, chat_id, servicio, hora_str, colaborador]
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
    await update.message.reply_text("¡Hola! 👋 Envíame la captura de pantalla de tus citas y las guardaré para recordártelas.")

async def procesar_imagen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_espera = await update.message.reply_text("⏳ Procesando imagen y programando recordatorios...")
    
    try:
        foto = update.message.photo[-1]
        archivo = await context.bot.get_file(foto.file_id)
        
        byte_stream = io.BytesIO()
        await archivo.download_to_memory(byte_stream)
        byte_stream.seek(0)
        imagen_pil = Image.open(byte_stream)

        prompt = """
        Extrae las citas de la tabla 'Agenda preliminar' en un arreglo JSON de objetos.
        Campos requeridos por cada objeto:
        - "fecha" (ejemplo: "Mar 27/Oct/2026")
        - "hora" (ejemplo: "08:20")
        - "servicio" (ejemplo: "PF Psicología Familiar B")
        - "sesion" (ejemplo: "5 de 5")
        - "colaborador" (ejemplo: "Fanny Celeste")
        """

       # Nueva llamada a la API de GenAI
        respuesta = client.models.generate_content(
            model='gemini-2.5-flash-lite',  # <--- ESTO ESTÁ DANDO EL ERROR
            contents=[prompt, imagen_pil],
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        
        citas_json = json.loads(respuesta.text.strip())

        chat_id = update.effective_chat.id
        programar_recordatorios(context.application, chat_id, citas_json)

        await context.bot.delete_message(chat_id=chat_id, message_id=msg_espera.message_id)
        
        texto_confirmacion = (
            f"✅ **Se han registrado {len(citas_json)} citas con éxito.**\n\n"
            f"🔔 Te enviaré un recordatorio **2 horas antes** de cada cita."
        )
        await update.message.reply_text(texto_confirmacion, parse_mode='Markdown')

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
    app.add_handler(MessageHandler(filters.PHOTO, procesar_imagen))

    print("🤖 Bot iniciado y listo para recibir capturas...")
    app.run_polling()