# Adicione no início do código (junto com os outros imports)
from PIL import Image
import shutil

# Adicione esta função para gerar thumbnails
async def generate_thumbnail(file_path, output_path="thumbnail.jpg"):
    try:
        cmd = [
            "ffmpeg",
            "-i", file_path,
            "-ss", "00:00:01",
            "-vframes", "1",
            output_path
        ]
        subprocess.run(cmd, check=True)
        return output_path if os.path.exists(output_path) else None
    except Exception as e:
        logger.error(f"Erro ao gerar thumbnail: {e}")
        return None

# Modifique a função handle_links para incluir thumbnails e metadados
@app.on_message(filters.text & ~filters.command(["start"]))
@handle_flood_wait
async def handle_links(client, message: Message):
    global START_TIME
    START_TIME = time.time()

    if message.from_user.id != Config.OWNER_ID:
        return await message.reply("❌ Acesso restrito ao dono do bot!")

    url = message.text.strip()
    if not url.startswith(('http://', 'https://')):
        return await message.reply("❌ Formato de link inválido!")

    msg = await message.reply("🔍 Iniciando processamento...")
    file_path = os.path.join(Config.DOWNLOAD_LOCATION, f"dl_{message.id}.mp4")

    try:
        # Download do vídeo
        await msg.edit("⬇️ Baixando vídeo...")
        if not await download_with_ytdlp(url, file_path):
            return await msg.edit("❌ Falha no download")

        # Verificação do arquivo
        if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
            raise Exception("Arquivo inválido após download")

        # Extrair metadados
        metadata = get_video_metadata(file_path)
        if not metadata:
            raise Exception("Falha ao extrair metadados")

        # Gerar thumbnail
        thumb_path = await generate_thumbnail(file_path)
        if not thumb_path:
            thumb_path = None  # O Telegram gerará uma thumbnail automática

        # Enviar vídeo com todos os metadados
        await msg.edit("⬆️ Enviando vídeo...")
        await client.send_video(
            chat_id=message.chat.id,
            video=file_path,
            duration=metadata['duration'],
            width=metadata['width'],
            height=metadata['height'],
            thumb=thumb_path,
            supports_streaming=True,
            progress=progress_callback,
            progress_args=(msg,)
        )

    except Exception as e:
        logger.error(f"Erro: {e}")
        await msg.edit(f"⚠️ Falha: {str(e)}")
    finally:
        # Limpeza
        for file in [file_path, "thumbnail.jpg"]:
            if os.path.exists(file):
                os.remove(file)
        try:
            await msg.delete()
        except:
            pass