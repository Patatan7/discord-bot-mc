from dotenv import load_dotenv
import discord
from discord.ext import tasks
import asyncio
import socket
import struct
import json
import os

load_dotenv()


# ==================== CONFIGURACIÓN DEL BOT ====================
# Pegar aquí el Token de tu bot de Discord
DISCORD_TOKEN = os.getenv("TOKEN")

# ID del canal de Discord donde se mostrará el tablero de estado
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

# IP y Puerto servidor de Minecraft
MC_SERVER_HOST = "127.0.0.1" #Cambiar a x.x.x.x para la Maquina / Cambiara x.x.x.x en local
MC_SERVER_PORT = 25565

# Intervalo de actualización del status (en segundos). 15 o 30 segundos es ideal.
UPDATE_INTERVAL_SECONDS = 20

# Archivo local donde el bot recordará el mensaje para no duplicarlo si se reinicia
MESSAGE_ID_FILE = "message_id.txt"
# ===============================================================

intents = discord.Intents.default()
client = discord.Client(intents=intents)

# Variables globales para controlar la memoria del estado del servidor
was_online = False
restarting_cycles_left = 0

# Funciones auxiliares para escribir y leer el formato VarInt oficial de Minecraft
def write_varint(value):
    out = b""
    while True:
        byte = value & 0x7F
        value >>= 7
        if value != 0:
            byte |= 0x80
        out += struct.pack("B", byte)
        if value == 0:
            break
    return out

async def read_varint(reader):
    total = 0
    shift = 0
    while True:
        byte = await reader.read(1)
        if not byte:
            raise ConnectionError("Conexión cerrada inesperadamente al leer VarInt")
        b = byte[0]
        total |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return total

# Función para realizar el ping asíncrono robusto
async def ping_server(host, port):
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=3.0
        )
    except Exception:
        return None

    try:
        # 1. Enviar paquete de Handshake
        host_bytes = host.encode('utf-8')
        payload = write_varint(0x00)  # Packet ID 0 para Handshake
        payload += write_varint(767)   # Protocolo de Minecraft (1.21.1)
        payload += write_varint(len(host_bytes)) + host_bytes
        payload += struct.pack('>H', port)
        payload += write_varint(1)     # Siguiente estado: 1 (status)

        writer.write(write_varint(len(payload)) + payload)

        # 2. Enviar solicitud de estado
        status_request = write_varint(1) + write_varint(0x00)
        writer.write(status_request)
        await writer.drain()

        # 3. Leer respuesta del servidor de forma controlada para evitar fragmentaciones
        packet_len = await read_varint(reader)
        packet_id = await read_varint(reader)
        if packet_id != 0x00:
            raise ValueError("ID de paquete inválido retornado por el servidor")

        json_len = await read_varint(reader)
        
        # Leemos el buffer en bucle hasta tener el JSON completo
        json_bytes = b""
        while len(json_bytes) < json_len:
            chunk = await reader.read(json_len - len(json_bytes))
            if not chunk:
                raise ConnectionError("Conexión perdida antes de completar el JSON")
            json_bytes += chunk

        writer.close()
        await writer.wait_closed()

        # Decodificamos la respuesta JSON del servidor
        json_data = json.loads(json_bytes.decode('utf-8', errors='ignore'))
        
        # Extraer MOTD/Descripción de forma segura
        motd_data = json_data.get("description", "Better MC Server")
        if isinstance(motd_data, dict):
            motd = motd_data.get("text", "")
            if "extra" in motd_data:
                motd += "".join([part.get("text", "") for part in motd_data["extra"]])
        else:
            motd = str(motd_data)

        # Extraer lista de nombres de jugadores conectados
        players_data = json_data.get("players", {})
        players_sample = players_data.get("sample", [])
        player_names = []
        if isinstance(players_sample, list):
            for player in players_sample:
                name = player.get("name")
                if name:
                    player_names.append(name)

        return {
            "online": True,
            "version": json_data.get("version", {}).get("name", "1.21.1"),
            "players_online": players_data.get("online", 0),
            "players_max": players_data.get("max", 12),
            "player_names": player_names,
            "motd": motd
        }
    except Exception:
        try:
            writer.close()
        except:
            pass
        return None

# Función principal para obtener el estado intentando el host configurado y el de respaldo
async def get_minecraft_status(host, port):
    # Intentamos primero con la IP configurada
    res = await ping_server(host, port)
    if res is not None:
        return res

    # Si falla, intentamos automáticamente con la IP de ZeroTier de San Antonio como respaldo
    fallback_host = "10.143.110.223" if host == "127.0.0.1" else "127.0.0.1"
    res = await ping_server(fallback_host, port)
    if res is not None:
        return res

    return {"online": False}

# Tarea repetitiva para actualizar el Embed y la Actividad del Bot en Discord
@tasks.loop(seconds=UPDATE_INTERVAL_SECONDS)
async def update_status_embed():
    global was_online, restarting_cycles_left

    channel = client.get_channel(CHANNEL_ID)
    if not channel:
        print(f"[Error] No se encontró el canal con ID {CHANNEL_ID}. Verifica los permisos del bot.")
        return

    # Consultamos el estado real del servidor de Minecraft
    status = await get_minecraft_status(MC_SERVER_HOST, MC_SERVER_PORT)
    
    # Creamos el diseño estético del Embed
    embed = discord.Embed()
    embed.set_author(name="MiMiMi Status", icon_url="")

    if status["online"]:
        # El servidor está activo y respondiendo
        was_online = True
        restarting_cycles_left = 0  # Reseteamos el contador de reinicio

        # Formatear la lista de nombres de jugadores sin negrita
        if status["players_online"] > 0:
            if status["player_names"]:
                players_list = "\n".join([f"• {name}" for name in status["player_names"]])
            else:
                players_list = "• *Jugando actualmente...*"
        else:
            players_list = "• *Nadie está jugando actualmente*"

        # Formato corregido: Negritas solo en títulos y emojis, valores en texto normal
        embed.title = "🛰️ Actualización de Status"
        embed.description = (
            "Servidor de Minecraft activo y funcionando\n\n"
            "**Modpack Activo** 🎮\n"
            f"Better MC v45 | [Fabric {status['version']}]\n\n"
            "**Jugadores** 👥\n"
            f"| {status['players_online']} / {status['players_max']} conectados\n"
            "──────────────\n"
            f"{players_list}\n\n"
            "**Dirección de Conexión** 🔗\n"
            f"`10.143.110.223` *(ZeroTier Requerido)*\n\n"
            "**Estado del Servidor** 🟢\n"
            "| Online"
        )
        embed.color = 0x3498db  # Celeste/Azul estético (SUMMER_SKY)
        
        # Actualización de la presencia del bot en Discord
        activity_text = f"Better MC ({status['players_online']}/{status['players_max']})"
        await client.change_presence(activity=discord.Game(name=activity_text))

    else:
        # El servidor no respondió. Evaluamos si es una caída o un reinicio en curso.
        if was_online:
            # Acaba de caerse justo después de estar encendido. Activamos modo reinicio.
            was_online = False
            restarting_cycles_left = 3  # 3 ciclos de tolerancia (3 * 20s = 60s)

        if restarting_cycles_left > 0:
            # Mostramos el estado intermedio naranja "Reiniciando..."
            restarting_cycles_left -= 1
            
            embed.title = "🛰️ Actualización de Status"
            embed.description = (
                "El servidor de Minecraft se está reiniciando o aplicando cambios\n\n"
                "**Estado del Servidor** 🔄\n"
                "| Reiniciando / Cargando Mods...\n\n"
                "*El bot está esperando a que el servidor vuelva a estar en línea (Tiempo límite: ~1 min).*"
            )
            embed.color = 0xe67e22  # Color Naranja de advertencia/transición (Reiniciando)
            
            await client.change_presence(activity=discord.Game(name="🔄 Reiniciando..."))
        else:
            # Pasó el tiempo de tolerancia y el servidor sigue apagado. Estado Offline definitivo.
            embed.title = "🛰️ Actualización de Status"
            embed.description = (
                "El servidor de Minecraft se encuentra apagado o en mantenimiento\n\n"
                "**Estado del Servidor** 🔴\n"
                "| Offline\n\n"
                "*Si este apagado no fue programado, por favor revisa la consola de Crafty o avisa al administrador.*"
            )
            embed.color = 0xe74c3c  # Rojo estético de apagado (Offline)
            
            await client.change_presence(activity=discord.Game(name="Servidor Offline"))

    embed.set_footer(text="Actualizado en tiempo real • Sistema de Monitoreo")

    # Intentamos buscar si ya existe un mensaje guardado previamente
    message_id = None
    if os.path.exists(MESSAGE_ID_FILE):
        with open(MESSAGE_ID_FILE, "r") as f:
            try:
                message_id = int(f.read().strip())
            except ValueError:
                pass

    msg = None
    if message_id:
        try:
            # Buscamos el mensaje anterior en Discord para editarlo
            msg = await channel.fetch_message(message_id)
        except discord.NotFound:
            print("[Info] El mensaje anterior fue borrado en Discord. Creando uno nuevo...")
        except Exception as e:
            print(f"[Error] No se pudo obtener el mensaje anterior: {e}")

    if msg:
        # Editamos el mensaje existente en silencio (¡Sin spam ni notificaciones!)
        await msg.edit(embed=embed)
        if status and status.get("online"):
            print(f"[Bot] Status actualizado correctamente. Jugadores: {status.get('players_online', 0)}/{status.get('players_max', 12)}")
        elif restarting_cycles_left > 0:
            print(f"[Bot] Servidor en proceso de reinicio. Tolerancia restante: {restarting_cycles_left} ciclos.")
        else:
            print("[Bot] Servidor detectado como Offline.")
    else:
        # Si es la primera vez o si lo borraron, enviamos un mensaje nuevo
        new_msg = await channel.send(embed=embed)
        # Guardamos su ID de inmediato en el archivo de texto
        with open(MESSAGE_ID_FILE, "w") as f:
            f.write(str(new_msg.id))
        print(f"[Bot] Nuevo mensaje de status creado y registrado con ID: {new_msg.id}")

# Esperamos a que el bot se conecte a Discord por primera vez
@client.event
async def on_ready():
    print(f"[Éxito] Bot conectado como {client.user.name}")
    
    # Iniciamos el bucle repetitivo de actualización de la tarjeta
    if not update_status_embed.is_running():
        update_status_embed.start()

# Ejecutamos el Bot
client.run(DISCORD_TOKEN)