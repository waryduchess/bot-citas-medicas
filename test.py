import os
from dotenv import load_dotenv
from google import genai

# Cargar variables de entorno del archivo .env
load_dotenv()

# Inicializar cliente
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# Hacer una petición con el alias del modelo más reciente
respuesta = client.models.generate_content(
    model='gemini-flash-latest', # <--- Usamos el alias que no caduca
    contents='Hola, responde con "OK" si recibes este mensaje.'
)

print("Respuesta de Gemini:", respuesta.text)