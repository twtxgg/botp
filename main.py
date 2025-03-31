import os
import asyncio
import yt_dlp
import aiohttp
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import MessageNotModified, FloodWait
import logging
import time
from functools import wraps
import subprocess
import re
from dotenv import load_dotenv

# ==================== CONFIGURAÇÃO INICIAL ====================
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    API_ID = int(os.getenv("API_ID", 0))
    API_HASH = os.getenv("API_HASH", "")
    DONO_ID = int(os.getenv("DONO_ID", 940793418))
    PASTA_DOWNLOAD = "./downloads"
    PASTA_THUMB = "./thumb_cache"
    TAMANHO_MAXIMO = 2000 * 1024 * 1024  # 2GB
    INTERVALO_ATUALIZACAO = 5
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"

COOKIES_PATH = "./cookies.txt"

# ==================== VERIFICAÇÕES ====================
def verificar_credenciais():
    required = {
        "BOT_TOKEN": Config.BOT_TOKEN,
        "API_ID": Config.API_ID,
        "API_HASH": Config.API_HASH
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        logger.error(f"Credenciais ausentes: {', '.join(missing)}")
        print("\nERRO: Configure o arquivo .env com:")
        print("BOT_TOKEN=seu_token_do_bot")
        print("API_ID=seu_api_id")
        print("API_HASH=seu_api_hash")
        exit(1)

def verificar_cookies():
    if not os.path.exists(COOKIES_PATH):
        return False
    try:
        with open(COOKIES_PATH, 'r') as f:
            return "youtube.com" in f.read()
    except:
        return False

# ==================== FUNÇÕES AUXILIARES ====================
def eh_comentario_canal(mensagem: Message) -> bool:
    return (mensagem.chat.type == enums.ChatType.CHANNEL and 
            mensagem.reply_to_message is not None)

async def apagar_url_se_permitido(client: Client, mensagem: Message, eh_resposta: bool):
    try:
        if eh_resposta or mensagem.chat.type in [enums.ChatType.SUPERGROUP, enums.ChatType.CHANNEL, enums.ChatType.GROUP]:
            await asyncio.sleep(2)
            await mensagem.delete()
    except Exception as e:
        logger.error(f"Erro ao apagar URL: {e}")

def converter_bytes(tamanho):
    unidades = ["B", "KB", "MB", "GB", "TB"]
    tamanho = float(tamanho)
    i = 0
    while tamanho >= 1024 and i < len(unidades)-1:
        tamanho /= 1024
        i += 1
    return f"{tamanho:.2f} {unidades[i]}"

def criar_barra_progresso(percentual):
    preenchido = int(percentual/10)
    return f"[{'■' * preenchido}{'□' * (10 - preenchido)}]"

# ==================== PROCESSAMENTO DE MÍDIA ====================
def extrair_metadados_video(caminho_arquivo):
    try:
        if not os.path.exists(caminho_arquivo) or os.path.getsize(caminho_arquivo) == 0:
            raise Exception("Arquivo inválido")

        duracao = float(subprocess.check_output([
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            caminho_arquivo
        ]).decode('utf-8').strip())

        dimensoes = subprocess.check_output([
            'ffprobe', '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height',
            '-of', 'csv=p=0',
            caminho_arquivo
        ]).decode('utf-8').strip().split(',')

        thumb_path = os.path.join(Config.PASTA_THUMB, f"thumb_{os.path.basename(caminho_arquivo)}.jpg")
        subprocess.run([
            'ffmpeg', '-y', '-ss', str(min(30, duracao - 1)),
            '-i', caminho_arquivo, '-vframes', '1', '-q:v', '2', thumb_path
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        return {
            'duracao': int(duracao),
            'largura': int(dimensoes[0]),
            'altura': int(dimensoes[1]),
            'caminho_thumbnail': thumb_path if os.path.exists(thumb_path) else None
        }
    except Exception as e:
        logger.error(f"Erro nos metadados: {e}")
        return None

# ==================== DOWNLOAD ====================
async def progresso_download(d, mensagem_status):
    global DOWNLOAD_CANCELADO, TAMANHO_TOTAL_ARQUIVO

    if DOWNLOAD_CANCELADO:
        raise Exception("Download cancelado")

    if d['status'] == 'downloading':
        baixado = d.get('downloaded_bytes', 0)
        total = d.get('total_bytes') or d.get('total_bytes_estimate') or TAMANHO_TOTAL_ARQUIVO
        
        if total > 0:
            await atualizar_progresso_download(baixado, total, mensagem_status)

async def baixar_com_ytdlp(url, caminho_arquivo, mensagem_status):
    global TAMANHO_TOTAL_ARQUIVO

    ydl_opts = {
        'outtmpl': caminho_arquivo,
        'quiet': True,
        'progress_hooks': [lambda d: asyncio.create_task(progresso_download(d, mensagem_status))],
        'cookiefile': COOKIES_PATH if os.path.exists(COOKIES_PATH) else None,
        'http_headers': {'User-Agent': Config.USER_AGENT},
        'format': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]',
        'merge_output_format': 'mp4'
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=False)
            TAMANHO_TOTAL_ARQUIVO = info.get('filesize') or info.get('total_bytes') or 0
            await asyncio.to_thread(ydl.download, [url])
        
        return os.path.exists(caminho_arquivo)
    except yt_dlp.utils.DownloadError as e:
        if "Sign in to confirm" in str(e):
            await mensagem_status.edit("⚠️ Cookies do YouTube necessários")
        return False
    except Exception as e:
        logger.error(f"Erro no yt-dlp: {e}")
        return False

async def download_arquivo_generico(url, caminho_arquivo, mensagem_status):
    global TAMANHO_TOTAL_ARQUIVO

    try:
        async with aiohttp.ClientSession(headers={'User-Agent': Config.USER_AGENT}) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return False

                TAMANHO_TOTAL_ARQUIVO = int(response.headers.get('Content-Length', 0))
                with open(caminho_arquivo, 'wb') as f:
                    async for chunk in response.content.iter_chunked(1024*1024):
                        if DOWNLOAD_CANCELADO:
                            return False
                        f.write(chunk)
                        await atualizar_progresso_download(f.tell(), TAMANHO_TOTAL_ARQUIVO, mensagem_status)
                return True
    except Exception as e:
        logger.error(f"Erro no download: {e}")
        return False

# ==================== UPLOAD E PROGRESSO ====================
@tratar_flood_wait
async def atualizar_progresso_download(baixado, total, mensagem):
    global ULTIMO_TEMPO_ATUALIZACAO

    agora = time.time()
    if agora - ULTIMO_TEMPO_ATUALIZACAO < Config.INTERVALO_ATUALIZACAO:
        return

    ULTIMO_TEMPO_ATUALIZACAO = agora
    percentual = (baixado / total) * 100 if total > 0 else 0
    velocidade = baixado / (agora - TEMPO_INICIO) if (agora - TEMPO_INICIO) > 0 else 0
    tempo_restante = (total - baixado) / velocidade if velocidade > 0 else 0

    try:
        texto = (
            f"⬇️ **Download Progress**\n"
            f"{criar_barra_progresso(percentual)} {percentual:.1f}%\n"
            f"📦 {converter_bytes(baixado)}/{converter_bytes(total)}\n"
            f"⚡ {converter_bytes(velocidade)}/s\n"
            f"⏱️ {tempo_restante:.0f}s restantes"
        )
        await mensagem.edit_text(
            texto,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_download")
            ]])
        )
    except MessageNotModified:
        pass
    except Exception as e:
        logger.warning(f"Erro ao atualizar progresso: {e}")

@tratar_flood_wait
async def callback_progresso(atual, total, mensagem):
    global ULTIMO_TEMPO_ATUALIZACAO, UPLOAD_CANCELADO

    if UPLOAD_CANCELADO:
        raise Exception("Upload cancelado")

    agora = time.time()
    if agora - ULTIMO_TEMPO_ATUALIZACAO < Config.INTERVALO_ATUALIZACAO:
        return

    ULTIMO_TEMPO_ATUALIZACAO = agora
    percentual = (atual / total) * 100
    velocidade = atual / (agora - TEMPO_INICIO)
    tempo_restante = (total - atual) / velocidade if velocidade > 0 else 0

    try:
        texto = (
            f"📤 **Upload Progress**\n"
            f"{criar_barra_progresso(percentual)} {percentual:.1f}%\n"
            f"📦 {converter_bytes(atual)}/{converter_bytes(total)}\n"
            f"⚡ {converter_bytes(velocidade)}/s\n"
            f"⏱️ {tempo_restante:.0f}s restantes"
        )
        await mensagem.edit_text(
            texto,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_upload")
            ]])
        )
    except MessageNotModified:
        pass
    except Exception as e:
        logger.warning(f"Erro no progresso: {e}")

# ==================== HANDLERS ====================
@app.on_message(filters.command(["start", "help"]))
@tratar_flood_wait
async def comando_start(client, message: Message):
    await message.reply(
        "🤖 **Bot de Upload de Vídeos**\n\n"
        "📌 **Como usar:**\n"
        "- Envie um link direto\n"
        "- Ou use /up <link>\n"
        "- Para legendas use /leg <link> <texto>\n\n"
        "🔐 Suporta cookies do YouTube (envie cookies.txt)"
    )

@app.on_message(filters.document & filters.private)
async def receber_cookies(client, message: Message):
    if message.document.file_name == "cookies.txt":
        try:
            await message.download(file_name=COOKIES_PATH)
            await message.reply("✅ Cookies configurados!" if verificar_cookies() else "❌ Cookies inválidos")
        except Exception as e:
            await message.reply(f"⚠️ Erro: {e}")

@app.on_message(filters.command(["up", "leg"]) | (filters.text & ~filters.command))
@tratar_flood_wait
async def handle_links(client, message: Message):
    global TEMPO_INICIO, DOWNLOAD_CANCELADO, UPLOAD_CANCELADO, TAMANHO_TOTAL_ARQUIVO
    
    TEMPO_INICIO = time.time()
    DOWNLOAD_CANCELADO = UPLOAD_CANCELADO = False
    TAMANHO_TOTAL_ARQUIVO = 0

    # Extrai URL e legenda
    url = message.text.split()[1] if message.command and len(message.command) > 1 else message.text
    legenda = " ".join(message.text.split()[2:]) if message.command and message.command[0] == "leg" else None

    if not url.startswith(('http://', 'https://')):
        return await message.reply("❌ Link inválido")

    msg_status = await message.reply("🔍 Processando...")
    ext = os.path.splitext(url.split('?')[0])[1].lower() if any(x in url.lower() for x in ['.jpg','.png','.gif']) else '.mp4'
    caminho_arquivo = os.path.join(Config.PASTA_DOWNLOAD, f"dl_{message.id}{ext}")

    try:
        # Download
        await msg_status.edit("⬇️ Baixando...")
        sucesso = await baixar_com_ytdlp(url, caminho_arquivo, msg_status) or \
                 await download_arquivo_generico(url, caminho_arquivo, msg_status)

        if not sucesso or not os.path.exists(caminho_arquivo):
            return await msg_status.edit("❌ Falha no download")

        # Verifica tamanho
        if os.path.getsize(caminho_arquivo) > Config.TAMANHO_MAXIMO:
            os.remove(caminho_arquivo)
            return await msg_status.edit("⚠️ Arquivo muito grande")

        # Processa e envia
        await msg_status.edit("📊 Processando...")
        if ext in ['.jpg','.jpeg','.png','.gif']:
            await client.send_photo(
                chat_id=message.chat.id,
                photo=caminho_arquivo,
                caption=legenda,
                progress=callback_progresso,
                progress_args=(msg_status,)
            )
        else:
            metadata = extrair_metadados_video(caminho_arquivo)
            if metadata:
                await client.send_video(
                    chat_id=message.chat.id,
                    video=caminho_arquivo,
                    duration=metadata['duracao'],
                    width=metadata['largura'],
                    height=metadata['altura'],
                    thumb=metadata['caminho_thumbnail'],
                    caption=legenda,
                    supports_streaming=True,
                    progress=callback_progresso,
                    progress_args=(msg_status,)
                )
            else:
                await client.send_document(
                    chat_id=message.chat.id,
                    document=caminho_arquivo,
                    caption=legenda,
                    progress=callback_progresso,
                    progress_args=(msg_status,)
                )

        await msg_status.delete()
        await apagar_url_se_permitido(client, message, message.reply_to_message is not None)

    except Exception as e:
        logger.error(f"Erro: {e}")
        await msg_status.edit(f"⚠️ Erro: {str(e)[:200]}")
    finally:
        if os.path.exists(caminho_arquivo):
            os.remove(caminho_arquivo)

@app.on_callback_query(filters.regex("cancelar_(download|upload)"))
async def cancelar_operacao(client, callback_query):
    global DOWNLOAD_CANCELADO, UPLOAD_CANCELADO
    
    if "download" in callback_query.data:
        DOWNLOAD_CANCELADO = True
    else:
        UPLOAD_CANCELADO = True
    
    await callback_query.answer("Operação cancelada")
    await callback_query.edit_message_text("❌ Operação cancelada pelo usuário")

# ==================== INICIALIZAÇÃO ====================
app = Client(
    "bot_upload_video",
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    bot_token=Config.BOT_TOKEN
)

async def iniciar_bot():
    await app.start()
    logger.info("✅ Bot iniciado com sucesso!")
    await asyncio.Event().wait()

if __name__ == "__main__":
    verificar_credenciais()
    
    # Prepara diretórios
    os.makedirs(Config.PASTA_DOWNLOAD, exist_ok=True)
    os.makedirs(Config.PASTA_THUMB, exist_ok=True)

    # Limpeza inicial
    for folder in [Config.PASTA_DOWNLOAD, Config.PASTA_THUMB]:
        for file in os.listdir(folder):
            if file.startswith(('dl_', 'thumb_')):
                try:
                    os.remove(os.path.join(folder, file))
                except:
                    pass

    try:
        asyncio.run(iniciar_bot())
    except KeyboardInterrupt:
        logger.info("⏹ Bot encerrado")
    except Exception as e:
        logger.error(f"❌ Erro fatal: {e}")
    finally:
        logger.info("🧹 Limpeza concluída")
