import os
import asyncio
import yt_dlp
import aiohttp
import json
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import MessageNotModified, FloodWait
import logging
import time
from functools import wraps
import subprocess
import re
import os
from dotenv import load_dotenv

load_dotenv()  # Carrega as variáveis do .env

print("Verificando variáveis:")
print("BOT_TOKEN:", bool(os.getenv("BOT_TOKEN")))  # Deve mostrar True
print("API_ID:", bool(os.getenv("API_ID")))        # Deve mostrar True
print("API_HASH:", bool(os.getenv("API_HASH")))    # Deve mostrar True

# Configuração de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Configurações do bot
class Config:
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
    API_ID = int(os.environ.get("API_ID", 0))
    API_HASH = os.environ.get("API_HASH", "")
    DONO_ID = 940793418
    PASTA_DOWNLOAD = "./downloads"
    PASTA_THUMB = "./thumb_cache"
    TAMANHO_MAXIMO = 2000 * 1024 * 1024  # 2GB
    INTERVALO_ATUALIZACAO = 5  # Segundos entre atualizações de progresso
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"

app = Client(
    "bot_upload_video",
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    bot_token=Config.BOT_TOKEN
)

# Variáveis globais
ULTIMO_TEMPO_ATUALIZACAO = 0
TEMPO_INICIO = 0
DOWNLOAD_CANCELADO = False
UPLOAD_CANCELADO = False
TAMANHO_TOTAL_ARQUIVO = 0
LOOP = None  # Para armazenar o event loop principal

# Dicionário para armazenar o status de cada download/upload
STATUS_PROCESSOS = {}

def eh_comentario_canal(mensagem: Message) -> bool:
    """Verifica se a mensagem é um comentário em um canal"""
    return (mensagem.chat.type == enums.ChatType.CHANNEL and 
            mensagem.reply_to_message is not None)

async def apagar_url_se_permitido(client: Client, mensagem: Message, eh_resposta: bool):
    """
    Tenta apagar a URL conforme as permissões
    - Funciona em grupos, canais e comentários de canais
    - Não apaga em chats privados
    """
    try:
        if eh_resposta or mensagem.chat.type in [enums.ChatType.SUPERGROUP, enums.ChatType.CHANNEL, enums.ChatType.GROUP]:
            await asyncio.sleep(2)
            await mensagem.delete()
            logger.info(f"URL removida com sucesso (ID: {mensagem.id})")
    except Exception as e:
        logger.error(f"Falha ao remover URL: {str(e)}")

def converter_bytes(tamanho):
    """Converte bytes para formato legível (KB, MB, GB)"""
    unidades = ["B", "KB", "MB", "GB", "TB"]
    tamanho = float(tamanho)
    i = 0
    while tamanho >= 1024 and i < len(unidades)-1:
        tamanho /= 1024
        i += 1
    return f"{tamanho:.2f} {unidades[i]}"

def criar_barra_progresso(percentual):
    """Gera barra de progresso visual"""
    preenchido = int(percentual/10)
    return f"[{'■' * preenchido}{'□' * (10 - preenchido)}]"

def extrair_metadados_video(caminho_arquivo):
    """Extrai metadados do vídeo (duração, dimensões, thumbnail)"""
    try:
        if not os.path.exists(caminho_arquivo):
            raise Exception("Arquivo não encontrado")

        if os.path.getsize(caminho_arquivo) == 0:
            raise Exception("Arquivo vazio")

        # Comandos para extrair duração e dimensões
        comando_duracao = [
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            caminho_arquivo
        ]
        comando_dimensoes = [
            'ffprobe', '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height',
            '-of', 'csv=p=0',
            caminho_arquivo
        ]

        duracao = float(subprocess.check_output(comando_duracao).decode('utf-8').strip())
        dimensoes = subprocess.check_output(comando_dimensoes).decode('utf-8').strip().split(',')

        # Gerar thumbnail
        caminho_thumbnail = os.path.join(Config.PASTA_THUMB, f"thumb_{os.path.basename(caminho_arquivo)}.jpg")
        if os.path.exists(caminho_thumbnail):
            os.remove(caminho_thumbnail)

        tempo_busca = min(30, float(duracao) - 1)

        subprocess.run([
            'ffmpeg', '-y', '-ss', str(tempo_busca), '-i', caminho_arquivo,
            '-vframes', '1', '-q:v', '2', caminho_thumbnail
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        return {
            'duracao': int(duracao),
            'largura': int(dimensoes[0]),
            'altura': int(dimensoes[1]),
            'caminho_thumbnail': caminho_thumbnail if os.path.exists(caminho_thumbnail) else None
        }

    except Exception as e:
        logger.error(f"Erro ao extrair metadados: {str(e)}")
        return None

def extrair_metadados_detalhados(video_path):
    """Extrai metadados detalhados usando FFprobe"""
    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration:stream=width,height,bit_rate',
            '-of', 'json',
            video_path
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        data = json.loads(result.stdout)
        
        duration = float(data['format']['duration'])
        video_stream = next(s for s in data['streams'] if s['codec_type'] == 'video')
        
        return {
            'duration': duration,
            'width': int(video_stream['width']),
            'height': int(video_stream['height']),
            'bitrate': int(video_stream.get('bit_rate', 0)) // 1000 if video_stream.get('bit_rate') else None
        }
    except Exception as e:
        logger.error(f"Erro ao extrair metadados detalhados: {str(e)}")
        return None

async def reduzir_video_adicional(input_path, current_size, duration, msg_status):
    """Reduz ainda mais o vídeo se necessário"""
    TAMANHO_ALVO = 1.90 * 1024 * 1024 * 1024
    
    # Calcular novo bitrate (reduzir 20%)
    with open(input_path, 'rb') as f:
        content = f.read()
        bitrate_actual = (len(content) * 8) / (duration * 1000)  # kbps
    
    new_bitrate = int(bitrate_actual * 0.8)
    new_maxrate = int(new_bitrate * 1.4)
    new_bufsize = int(new_bitrate * 2)
    
    await msg_status.edit(f"⚠️ Ajustando bitrate para {new_bitrate}kbps...")
    
    output_path = input_path + ".reduced.mp4"
    
    cmd = [
        'ffmpeg', '-y', '-i', input_path,
        '-c:v', 'libx264',
        '-b:v', f'{new_bitrate}k',
        '-maxrate', f'{new_maxrate}k',
        '-bufsize', f'{new_bufsize}k',
        '-preset', 'fast',
        '-profile:v', 'main',
        '-pix_fmt', 'yuv420p',
        '-movflags', '+faststart',
        '-c:a', 'aac',
        '-b:a', '64k',  # Reduzir áudio também
        '-ac', '2',
        output_path
    ]
    
    process = await asyncio.create_subprocess_exec(*cmd)
    await process.wait()
    
    if process.returncode == 0 and os.path.exists(output_path):
        os.replace(output_path, input_path)
    else:
        raise Exception("Falha ao reduzir adicionalmente")

async def enviar_video_convertido(client, mensagem, video_path, msg_status):
    """Envia o vídeo convertido com os parâmetros adequados"""
    metadados = extrair_metadados_detalhados(video_path)
    if not metadados:
        raise Exception("Não foi possível obter metadados do vídeo convertido")
    
    # Gerar thumbnail
    thumb_path = os.path.join(Config.PASTA_THUMB, f"thumb_{os.path.basename(video_path)}.jpg")
    if os.path.exists(thumb_path):
        os.remove(thumb_path)
    
    subprocess.run([
        'ffmpeg', '-y', '-ss', '00:00:05', '-i', video_path,
        '-vframes', '1', '-q:v', '2', thumb_path
    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    
    await msg_status.edit("⬆️ Enviando vídeo convertido...")
    
    params = {
        'chat_id': mensagem.chat.id,
        'video': video_path,
        'duration': int(metadados['duration']),
        'width': metadados['width'],
        'height': metadados['height'],
        'thumb': thumb_path if os.path.exists(thumb_path) else None,
        'supports_streaming': True,
        'progress': callback_progresso,
        'progress_args': (msg_status,)
    }
    
    if mensagem.reply_to_message:
        params['reply_to_message_id'] = mensagem.reply_to_message.id
    
    await client.send_video(**params)
    await msg_status.delete()

def tratar_flood_wait(func):
    """Decorator para tratamento de FloodWait"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except FloodWait as e:
            logger.warning(f"FloodWait: Esperando {e.x} segundos")
            await asyncio.sleep(e.x)
            return await func(*args, **kwargs)
    return wrapper

# Função modificada para executar no loop principal
def progresso_download(d, mensagem_status):
    """Callback de progresso do yt-dlp que executa corretamente no event loop"""
    global DOWNLOAD_CANCELADO, TAMANHO_TOTAL_ARQUIVO, LOOP

    if DOWNLOAD_CANCELADO:
        raise Exception("Download cancelado pelo usuário")

    if d['status'] == 'downloading':
        baixado = d.get('downloaded_bytes', 0)
        total = d.get('total_bytes') or d.get('total_bytes_estimate') or TAMANHO_TOTAL_ARQUIVO
        
        if total > 0 and LOOP:
            # Criar uma future para executar no loop principal
            asyncio.run_coroutine_threadsafe(
                atualizar_progresso_download(baixado, total, mensagem_status),
                LOOP
            )

async def baixar_com_ytdlp(url, caminho_arquivo, mensagem_status):
    """Download usando yt-dlp com configurações especiais para XVideos e YouTube"""
    global DOWNLOAD_CANCELADO, TAMANHO_TOTAL_ARQUIVO, LOOP

    # Armazenar o loop principal para uso na função de progresso
    LOOP = asyncio.get_running_loop()

    opcoes_ydl = {
        'outtmpl': caminho_arquivo,
        'quiet': True,
        'no_warnings': True,
        'geo_bypass': True,
        'noplaylist': True,
        'restrictfilenames': True,
        'retries': 3,
        'fragment_retries': 3,
        'continue_dl': True,
        'socket_timeout': 30,
        'progress_hooks': [lambda d: progresso_download(d, mensagem_status)],
    }

    # Configurações específicas para XVideos
    if 'xvideos.com' in url:
        opcoes_ydl.update({
            'format': 'best',
            'headers': {
                'User-Agent': Config.USER_AGENT,
                'Referer': 'https://www.xvideos.com/',
                'Accept': '*/*',
            },
            'extractor_args': {
                'generic': {
                    'no-check-certificate': True,
                    'prefer-insecure': True
                }
            }
        })
    # Configurações para YouTube
    elif 'youtube.com' in url or 'youtu.be' in url:
        opcoes_ydl.update({
            'format': 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]',
            'merge_output_format': 'mp4',
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4'
            }],
            'youtube_include_dash_manifest': False,
            'youtube_include_hls_manifest': False,
        })
    # Configuração padrão para outros sites
    else:
        opcoes_ydl.update({
            'format': 'best[ext=mp4]/best',
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4'
            }]
        })

    try:
        with yt_dlp.YoutubeDL(opcoes_ydl) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=False)
            TAMANHO_TOTAL_ARQUIVO = info.get('filesize') or info.get('total_bytes')
            if TAMANHO_TOTAL_ARQUIVO is None:
                TAMANHO_TOTAL_ARQUIVO = 0
                logger.warning("Não foi possível determinar o tamanho total do arquivo antes do download.")

            await asyncio.to_thread(ydl.download, [url])

        # Verifica se o arquivo foi baixado corretamente
        if not os.path.exists(caminho_arquivo):
            # Tenta encontrar o arquivo pelo nome padrão do yt-dlp
            filename = ydl.prepare_filename(info)
            if os.path.exists(filename):
                os.rename(filename, caminho_arquivo)
            else:
                return False

        return True
    except Exception as e:
        logger.error(f"Erro ao baixar com yt-dlp: {str(e)}")
        # Tentar fallback mais simples
        try:
            with yt_dlp.YoutubeDL({'format': 'best', 'outtmpl': caminho_arquivo, 'progress_hooks': [lambda d: progresso_download(d, mensagem_status)]}) as ydl:
                await asyncio.to_thread(ydl.download, [url])
            return os.path.exists(caminho_arquivo)
        except Exception as e2:
            logger.error(f"Fallback também falhou: {str(e2)}")
            return False

async def download_arquivo_generico(url, caminho_arquivo, mensagem_status):
    """Download de qualquer tipo de arquivo genérico"""
    global DOWNLOAD_CANCELADO, TAMANHO_TOTAL_ARQUIVO
    baixado = 0
    try:
        headers = {'User-Agent': Config.USER_AGENT}
        if 'xvideos.com' in url:
            headers.update({
                'Referer': 'https://www.xvideos.com/',
                'Accept': '*/*'
            })

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url) as response:
                if response.status == 200:
                    TAMANHO_TOTAL_ARQUIVO = int(response.headers.get('Content-Length', 0))
                    with open(caminho_arquivo, 'wb') as f:
                        async for chunk in response.content.iter_chunked(1024*1024):  # 1MB chunks
                            if DOWNLOAD_CANCELADO:
                                logger.info("Download cancelado pelo usuário.")
                                return False
                            f.write(chunk)
                            baixado += len(chunk)
                            await atualizar_progresso_download(baixado, TAMANHO_TOTAL_ARQUIVO, mensagem_status)
                    return True
                else:
                    logger.error(f"Erro HTTP {response.status} ao baixar arquivo")
                    return False
    except Exception as e:
        logger.error(f"Erro ao baixar arquivo: {e}")
        return False

@tratar_flood_wait
async def atualizar_progresso_download(baixado, total, mensagem):
    """Atualiza a mensagem de progresso do download"""
    global ULTIMO_TEMPO_ATUALIZACAO, TEMPO_INICIO

    agora = time.time()
    if agora - ULTIMO_TEMPO_ATUALIZACAO < Config.INTERVALO_ATUALIZACAO:
        return

    ULTIMO_TEMPO_ATUALIZACAO = agora
    percentual = (baixado / total) * 100 if total > 0 else 0
    tempo_decorrido = agora - TEMPO_INICIO
    velocidade = baixado / tempo_decorrido if tempo_decorrido > 0 else 0
    tempo_restante = (total - baixado) / velocidade if velocidade > 0 else 0

    try:
        texto = (
            f"⬇️ **Progresso do Download**\n"
            f"📦 Tamanho Total: {converter_bytes(total)}\n"
            f"{criar_barra_progresso(percentual)} {percentual:.1f}%\n"
            f"⚡ {converter_bytes(velocidade)}/s\n"
            f"⏱️ {tempo_restante:.0f}s restantes"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data="cancelar_download")]])
        await mensagem.edit(texto, reply_markup=keyboard)
    except MessageNotModified:
        pass
    except Exception as e:
        logger.warning(f"Falha ao atualizar progresso do download: {e}")

@tratar_flood_wait
async def callback_progresso(atual, total, mensagem):
    """Callback de progresso com controle de flood"""
    global ULTIMO_TEMPO_ATUALIZACAO, UPLOAD_CANCELADO

    if UPLOAD_CANCELADO:
        raise Exception("Upload cancelado pelo usuário")

    agora = time.time()
    if agora - ULTIMO_TEMPO_ATUALIZACAO < Config.INTERVALO_ATUALIZACAO:
        return

    ULTIMO_TEMPO_ATUALIZACAO = agora
    percentual = (atual / total) * 100
    tempo_decorrido = agora - TEMPO_INICIO
    velocidade = atual / tempo_decorrido if tempo_decorrido > 0 else 0
    tempo_restante = (total - atual) / velocidade if velocidade > 0 else 0

    try:
        texto = (
            f"📤 **Progresso do Upload**\n"
            f"📦 Tamanho Total: {converter_bytes(total)}\n"
            f"{criar_barra_progresso(percentual)} {percentual:.1f}%\n"
            f"⚡ {converter_bytes(velocidade)}/s\n"
            f"⏱️ {tempo_restante:.0f}s restantes"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data="cancelar_upload")]])
        await mensagem.edit(texto, reply_markup=keyboard)
    except MessageNotModified:
        pass
    except Exception as e:
        logger.warning(f"Falha ao atualizar progresso: {e}")

@app.on_message(filters.command(["start", "help"]))
@tratar_flood_wait
async def comando_start(client, mensagem: Message):
    """Handler do comando /start e /help"""
    await mensagem.reply(
        "✅ **Bot de Upload de Arquivos Ativo!**\n\n"
        "📌 **Como usar:**\n"
        "• Envie uma URL de vídeo/imagem\n"
        "• Ou use /up <URL>\n"
        "• Para legenda direta: /leg <URL> <texto>\n"
        "• Para adicionar legenda depois: responda com /leg <texto>\n"
        "• Para converter vídeos grandes: /conv <URL> ou responda um vídeo com /conv\n\n"
        "💡 **Suporte a:** YouTube, XVideos e centenas de outros sites\n"
        "💡 **Em canais:** Responda a postagens com os comandos para enviar como comentário"
    )

@app.on_message(filters.command(["up", "leg"]))
@tratar_flood_wait
async def comando_upload(client, mensagem: Message):
    """Manipula os comandos /up e /leg"""
    global TEMPO_INICIO, DOWNLOAD_CANCELADO, UPLOAD_CANCELADO, TAMANHO_TOTAL_ARQUIVO
    TEMPO_INICIO = time.time()
    DOWNLOAD_CANCELADO = False
    UPLOAD_CANCELADO = False
    TAMANHO_TOTAL_ARQUIVO = 0

    eh_resposta = mensagem.reply_to_message is not None
    mensagem_original = mensagem.reply_to_message if eh_resposta else None

    if mensagem.command[0] == "leg" and len(mensagem.command) > 1:
        padrao_url = re.compile(r'(https?://\S+)')
        match = padrao_url.search(mensagem.text)

        if match:
            url = match.group(1)
            # Pega todo o texto após o comando, remove a URL e limpa os espaços
            legenda = mensagem.text.replace(match.group(0), "").replace("/leg ", "").strip()
        elif eh_resposta:
            if len(mensagem.command) < 2:
                await mensagem.reply("❌ Use /leg <URL> <texto> ou responda uma mídia com /leg <texto>")
                return

            legenda = mensagem.text.split(maxsplit=1)[1]

            try:
                if mensagem_original.caption is not None:
                    await mensagem_original.edit_caption(legenda)
                else:
                    await mensagem_original.edit_caption(caption=legenda)
                await mensagem.delete()
                return
            except Exception as e:
                logger.error(f"Erro ao adicionar legenda: {str(e)}")
                await mensagem.reply(f"⚠️ Erro ao adicionar legenda: {str(e)}")
                return
        else:
            await mensagem.reply("❌ Formato incorreto. Use: /leg http://exemplo.com/video.mp4 sua legenda aqui")
            return
    elif mensagem.command[0] == "up" and len(mensagem.command) > 1:
        url = mensagem.text.split(maxsplit=1)[1]
        legenda = None
    else:
        await mensagem.reply("❌ Use /up <URL> ou /leg <URL> <texto>")
        return

    msg_status = await mensagem.reply("🔍 Iniciando processamento...")

    # Determinar extensão baseada na URL ou tipo de conteúdo
    extensao = '.mp4'  # Padrão para vídeos
    if any(ext in url.lower() for ext in ['.jpg', '.jpeg', '.png', '.gif']):
        extensao = os.path.splitext(url.split('?')[0])[1].lower()

    caminho_arquivo = os.path.join(Config.PASTA_DOWNLOAD, f"dl_{mensagem.id}{extensao}")

    try:
        # Limpar arquivos antigos com o mesmo nome
        if os.path.exists(caminho_arquivo):
            os.remove(caminho_arquivo)

        await msg_status.edit("⬇️ Baixando arquivo...")

        # Tentar primeiro com yt-dlp para qualquer URL - MODIFICAÇÃO PRINCIPAL
        try:
            # Testar se o yt-dlp reconhece a URL como site compatível
            with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
                info_dict = await asyncio.to_thread(ydl.extract_info, url, download=False, process=False)
            
            # Se chegou aqui, é compatível com yt-dlp
            sucesso = await baixar_com_ytdlp(url, caminho_arquivo, msg_status)
        except Exception as e:
            logger.info(f"URL não compatível com yt-dlp: {str(e)}")
            # Se falhar, usar o método genérico para URLs diretas
            sucesso = await download_arquivo_generico(url, caminho_arquivo, msg_status)

        if not sucesso or not os.path.exists(caminho_arquivo):
            await msg_status.edit("❌ Falha no download do arquivo")
            return

        tamanho_arquivo = os.path.getsize(caminho_arquivo)
        if tamanho_arquivo > Config.TAMANHO_MAXIMO:
            os.remove(caminho_arquivo)
            await msg_status.edit(f"❌ Arquivo muito grande ({converter_bytes(tamanho_arquivo)})")
            return

        await msg_status.edit("📊 Processando vídeo...")

        # Preparar parâmetros de envio
        params = {
            'caption': legenda,
            'progress': callback_progresso,
            'progress_args': (msg_status,)
        }

        if eh_resposta:
            params['reply_to_message_id'] = mensagem_original.id

        await msg_status.edit("⬆️ Enviando arquivo...")

        # Verificar tipo de arquivo e enviar
        if extensao in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']:
            await client.send_photo(
                chat_id=mensagem.chat.id,
                photo=caminho_arquivo,
                **params
            )
        elif extensao in ['.mp4', '.mkv', '.avi', '.mov', '.webm']:
            metadados = extrair_metadados_video(caminho_arquivo)
            if metadados:
                await client.send_video(
                    chat_id=mensagem.chat.id,
                    video=caminho_arquivo,
                    duration=metadados['duracao'],
                    width=metadados['largura'],
                    height=metadados['altura'],
                    thumb=metadados['caminho_thumbnail'] or None,
                    supports_streaming=True,
                    **params
                )
            else:
                await client.send_document(
                    chat_id=mensagem.chat.id,
                    document=caminho_arquivo,
                    **params
                )
        else:
            await client.send_document(
                chat_id=mensagem.chat.id,
                document=caminho_arquivo,
                **params
            )

        await msg_status.delete()
        await apagar_url_se_permitido(client, mensagem, eh_resposta)

    except Exception as e:
        logger.error(f"Erro no processamento: {str(e)}")
        await msg_status.edit(f"⚠️ Erro: {str(e)[:200]}")
    finally:
        # Limpeza de arquivos temporários
        if os.path.exists(caminho_arquivo):
            os.remove(caminho_arquivo)
        thumb_path = os.path.join(Config.PASTA_THUMB, f"thumb_{os.path.basename(caminho_arquivo)}.jpg")
        if os.path.exists(thumb_path):
            os.remove(thumb_path)

@app.on_message(filters.command("conv"))
@tratar_flood_wait
async def comando_converter_avancado(client, mensagem: Message):
    """Handler avançado para o comando /conv com cálculo dinâmico de bitrate"""
    global TEMPO_INICIO, DOWNLOAD_CANCELADO, UPLOAD_CANCELADO
    
    if len(mensagem.command) < 2 and not mensagem.reply_to_message:
        await mensagem.reply("❌ Use /conv <URL> ou responda a um vídeo com /conv")
        return

    TEMPO_INICIO = time.time()
    DOWNLOAD_CANCELADO = False
    UPLOAD_CANCELADO = False
    
    msg_status = await mensagem.reply("🔍 Analisando vídeo...")
    original_path = None
    converted_path = None
    
    try:
        # Obter o arquivo de origem (URL ou resposta)
        if mensagem.reply_to_message:
            if mensagem.reply_to_message.video:
                file_id = mensagem.reply_to_message.video.file_id
            elif mensagem.reply_to_message.document:
                file_id = mensagem.reply_to_message.document.file_id
            else:
                await msg_status.edit("❌ Responda a um vídeo ou arquivo para converter")
                return
            
            original_path = await client.download_media(
                file_id,
                file_name=os.path.join(Config.PASTA_DOWNLOAD, f"orig_{mensagem.id}.mp4"),
                progress=progresso_download,
                progress_args=(msg_status,)
            )
        else:
            url = mensagem.text.split(maxsplit=1)[1]
            original_path = os.path.join(Config.PASTA_DOWNLOAD, f"orig_{mensagem.id}.mp4")
            sucesso = await baixar_com_ytdlp(url, original_path, msg_status) or \
                     await download_arquivo_generico(url, original_path, msg_status)
            if not sucesso:
                raise Exception("Falha no download")

        # Verificar tamanho original
        tamanho_original = os.path.getsize(original_path)
        if tamanho_original <= Config.TAMANHO_MAXIMO:
            await msg_status.edit("ℹ️ O vídeo já está dentro do tamanho máximo. Enviando original...")
            await enviar_video_convertido(client, mensagem, original_path, msg_status)
            return

        # Extrair metadados detalhados
        metadados = extrair_metadados_detalhados(original_path)
        if not metadados:
            raise Exception("Falha ao extrair metadados")

        duracao_segundos = metadados['duration']
        if duracao_segundos <= 0:
            raise Exception("Duração inválida do vídeo")

        # Calcular bitrate de vídeo ideal para ~1.90GB
        TAMANHO_ALVO = 1.90 * 1024 * 1024 * 1024  # ~1.90GB
        BITRATE_AUDIO = 96  # kbps
        BITRATE_AUDIO_BYTES = BITRATE_AUDIO * 1000 / 8  # bytes por segundo
        
        # Calcular espaço disponível para vídeo
        espaco_audio = BITRATE_AUDIO_BYTES * duracao_segundos
        espaco_video = TAMANHO_ALVO - espaco_audio
        
        if espaco_video <= 0:
            raise Exception("Vídeo muito longo para o tamanho alvo")
        
        # Calcular bitrate de vídeo em kbps
        bitrate_video_kbps = int((espaco_video * 8) / (1000 * duracao_segundos))
        
        # Ajustar bitrate máximo e buffer
        maxrate = int(bitrate_video_kbps * 1.4)  # 40% acima do bitrate médio
        bufsize = int(bitrate_video_kbps * 2)    # Tamanho do buffer
        
        # Limites de segurança
        bitrate_video_kbps = max(500, min(bitrate_video_kbps, 8000))  # Entre 500kbps e 8000kbps
        maxrate = max(700, min(maxrate, 10000))
        bufsize = max(1000, min(bufsize, 16000))
        
        await msg_status.edit(f"🔄 Convertendo vídeo (bitrate: {bitrate_video_kbps}kbps)...")
        
        # Preparar caminhos
        converted_path = os.path.join(Config.PASTA_DOWNLOAD, f"conv_{mensagem.id}.mp4")
        
        # Comando FFmpeg otimizado
        cmd = [
            'ffmpeg', '-y', '-i', original_path,
            '-c:v', 'libx264', 
            '-b:v', f'{bitrate_video_kbps}k',
            '-maxrate', f'{maxrate}k',
            '-bufsize', f'{bufsize}k',
            '-preset', 'slow',
            '-profile:v', 'main',
            '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart',
            '-c:a', 'aac',
            '-b:a', f'{BITRATE_AUDIO}k',
            '-ac', '2',
            '-f', 'mp4',
            converted_path
        ]
        
        # Executar conversão
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        # Monitorar progresso
        last_update = time.time()
        while True:
            await asyncio.sleep(2)
            
            if process.returncode is not None:
                break
                
            if time.time() - last_update > Config.INTERVALO_ATUALIZACAO:
                if os.path.exists(converted_path):
                    current_size = os.path.getsize(converted_path)
                    percent = (current_size / TAMANHO_ALVO) * 100
                    await msg_status.edit(
                        f"🔄 Convertendo vídeo...\n"
                        f"🎞️ Bitrate: {bitrate_video_kbps}kbps\n"
                        f"📊 Progresso: {percent:.1f}%\n"
                        f"📦 Tamanho estimado: ~1.90GB"
                    )
                last_update = time.time()

        # Verificar resultado
        if process.returncode != 0 or not os.path.exists(converted_path):
            stderr = await process.stderr.read()
            raise Exception(f"Falha na conversão: {stderr.decode()[:200]}")
        
        # Verificar tamanho final
        tamanho_final = os.path.getsize(converted_path)
        if tamanho_final > Config.TAMANHO_MAXIMO:
            # Se ainda for grande, tentar reduzir mais
            await reduzir_video_adicional(converted_path, tamanho_final, duracao_segundos, msg_status)
        
        # Enviar vídeo convertido
        await enviar_video_convertido(client, mensagem, converted_path, msg_status)

    except Exception as e:
        logger.error(f"Erro na conversão avançada: {str(e)}")
        await msg_status.edit(f"❌ Erro: {str(e)[:200]}")
        
    finally:
        # Limpeza
        for path in [original_path, converted_path]:
            if path and os.path.exists(path):
                os.remove(path)
        
        thumb_path = os.path.join(Config.PASTA_THUMB, f"thumb_{os.path.basename(original_path)}.jpg")
        if os.path.exists(thumb_path):
            os.remove(thumb_path)

@app.on_message(filters.text & ~filters.command(["start", "help", "up", "leg", "conv"]))
@tratar_flood_wait
async def lidar_com_links_automaticos(client, mensagem: Message):
    """Handler para links automáticos (sem comando)"""
    global TEMPO_INICIO, DOWNLOAD_CANCELADO, UPLOAD_CANCELADO, TAMANHO_TOTAL_ARQUIVO
    TEMPO_INICIO = time.time()
    DOWNLOAD_CANCELADO = False
    UPLOAD_CANCELADO = False
    TAMANHO_TOTAL_ARQUIVO = 0

    eh_resposta = mensagem.reply_to_message is not None
    mensagem_original = mensagem.reply_to_message if eh_resposta else None

    url = mensagem.text.strip()
    if not url.startswith(('http://', 'https://')):
        return

    msg_status = await mensagem.reply("🔍 Processando link automaticamente...")
    caminho_arquivo = os.path.join(Config.PASTA_DOWNLOAD, f"dl_{mensagem.id}.mp4")

    try:
        if os.path.exists(caminho_arquivo):
            os.remove(caminho_arquivo)

        await msg_status.edit("⬇️ Baixando vídeo...")

        # Tentar primeiro com yt-dlp para qualquer URL - MODIFICAÇÃO PRINCIPAL
        try:
            # Testar se o yt-dlp reconhece a URL como site compatível
            with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
                info_dict = await asyncio.to_thread(ydl.extract_info, url, download=False, process=False)
            
            # Se chegou aqui, é compatível com yt-dlp
            sucesso = await baixar_com_ytdlp(url, caminho_arquivo, msg_status)
        except Exception as e:
            logger.info(f"URL não compatível com yt-dlp: {str(e)}")
            # Se falhar, usar o método genérico para URLs diretas
            sucesso = await download_arquivo_generico(url, caminho_arquivo, msg_status)

        if not sucesso or not os.path.exists(caminho_arquivo):
            await msg_status.edit("❌ Falha no download do vídeo")
            return

        tamanho_arquivo = os.path.getsize(caminho_arquivo)
        if tamanho_arquivo > Config.TAMANHO_MAXIMO:
            os.remove(caminho_arquivo)
            await msg_status.edit(f"❌ Arquivo muito grande ({converter_bytes(tamanho_arquivo)})")
            return

        await msg_status.edit("📊 Processando vídeo...")
        metadados = extrair_metadados_video(caminho_arquivo)
        if not metadados:
            await msg_status.edit("❌ Falha ao extrair metadados do vídeo")
            os.remove(caminho_arquivo)
            return

        await msg_status.edit("⬆️ Enviando vídeo...")

        params = {
            'chat_id': mensagem.chat.id,
            'video': caminho_arquivo,
            'duration': metadados['duracao'],
            'width': metadados['largura'],
            'height': metadados['altura'],
            'thumb': metadados['caminho_thumbnail'] or None,
            'supports_streaming': True,
            'progress': callback_progresso,
            'progress_args': (msg_status,)
        }

        if eh_resposta:
            params['reply_to_message_id'] = mensagem_original.id

        await client.send_video(**params)

        await msg_status.delete()
        await apagar_url_se_permitido(client, mensagem, eh_resposta)

    except Exception as e:
        logger.error(f"Erro no processamento automático: {str(e)}")
        await msg_status.edit(f"⚠️ Erro: {str(e)[:200]}")
    finally:
        if os.path.exists(caminho_arquivo):
            os.remove(caminho_arquivo)
        thumb_path = os.path.join(Config.PASTA_THUMB, f"thumb_{os.path.basename(caminho_arquivo)}.jpg")
        if os.path.exists(thumb_path):
            os.remove(thumb_path)
        try:
            await msg_status.delete()
        except:
            pass

@app.on_callback_query(filters.regex("cancelar_download"))
async def cancelar_download_callback(client, callback_query):
    """Cancela o download quando o botão é clicado"""
    global DOWNLOAD_CANCELADO
    DOWNLOAD_CANCELADO = True
    await callback_query.answer("Download cancelado.")
    try:
        await callback_query.edit_message_text("❌ Download cancelado pelo usuário.")
    except:
        pass

@app.on_callback_query(filters.regex("cancelar_upload"))
async def cancelar_upload_callback(client, callback_query):
    """Cancela o upload quando o botão é clicado"""
    global UPLOAD_CANCELADO
    UPLOAD_CANCELADO = True
    await callback_query.answer("Upload cancelado.")
    try:
        await callback_query.edit_message_text("❌ Upload cancelado pelo usuário.")
    except:
        pass

if __name__ == "__main__":
    # Garante que as pastas existam
    os.makedirs(Config.PASTA_DOWNLOAD, exist_ok=True)
    os.makedirs(Config.PASTA_THUMB, exist_ok=True)

    # Limpa arquivos temporários antigos
    for file in os.listdir(Config.PASTA_DOWNLOAD):
        if file.startswith('dl_'):
            try:
                os.remove(os.path.join(Config.PASTA_DOWNLOAD, file))
            except:
                pass

    for file in os.listdir(Config.PASTA_THUMB):
        if file.startswith('thumb_'):
            try:
                os.remove(os.path.join(Config.PASTA_THUMB, file))
            except:
                pass

    logger.info("----- Bot Iniciado -----")
    app.run()
