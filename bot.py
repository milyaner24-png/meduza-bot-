import os
import asyncio
import uuid
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, FSInputFile
from gtts import gTTS
from playwright.async_api import async_playwright

# TOKENni xavfsizroq qilish uchun o'zgaruvchiga oldik (almashtirib qo'yishingiz mumkin)
TOKEN = os.getenv("BOT_TOKEN", "8754760907:AAHHdgKiTaoqyQjA1d9ZVLfaM2mYW9KLXlc")

bot = Bot(token=TOKEN)
dp = Dispatcher()

@dp.message(F.text == "/start")
async def start_cmd(message: Message):
    await message.answer("Salom! Men Meduza botiman. Menga istalgan matn yuboring, men uni ovozga aylantirib va 3D animatsiya qilib beraman!")

@dp.message()
async def handle_text(message: Message):
    text = message.text
    # Har bir foydalanuvchi uchun unikal ID (fayllar to'qnashib ketmasligi uchun)
    uid = str(uuid.uuid4())
    audio_path = f"voice_{uid}.mp3"
    html_path = f"index_{uid}.html"
    screenshot_path = f"screenshot_{uid}.png"
    
    processing_msg = await message.answer("⏳ Matn qayta ishlanmoqda va 3D animatsiya yaratilmoqda...")
    
    try:
        # 1. gTTS orqali ovoz yaratish (tilni avtomatik aniqlash yoki 'uz' ga moslash mumkin, hozircha 'uz' yoki 'en')
        # Matn turiga qarab lang='uz' yoki 'en' qilishingiz mumkin
        tts = gTTS(text=text, lang='uz')
        tts.save(audio_path)
        
        # 2. HTML to'g'rilandi (ortiqcha </main> olib tashlandi)
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{
                    background: linear-gradient(135deg, #1a1a2e, #16213e);
                    color: white;
                    height: 100vh;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    font-family: Arial, sans-serif;
                    overflow: hidden;
                    margin: 0;
                }}
                .card {{
                    background: rgba(255, 255, 255, 0.1);
                    padding: 40px;
                    border-radius: 20px;
                    backdrop-filter: blur(10px);
                    box-shadow: 0 15px 35px rgba(0,0,0,0.5);
                    transform: perspective(1000px) rotateX(10deg) rotateY(-10deg);
                    animation: float 4s ease-in-out infinite;
                    max-width: 600px;
                    text-align: center;
                }}
                @keyframes float {{
                    0%, 100% {{ transform: perspective(1000px) rotateX(5deg) rotateY(-5deg) translateY(0); }}
                    50% {{ transform: perspective(1000px) rotateX(-5deg) rotateY(5deg) translateY(-15px); }}
                }}
                h1 {{ font-size: 32px; margin-bottom: 20px; color: #00ffcc; }}
                p {{ font-size: 18px; line-height: 1.5; word-break: break-word; }}
            </style>
        </head>
        <body>
            <div class="card">
                <h1>Meduza Animation</h1>
                <p>{text}</p>
            </div>
        </body>
        </html>
        """
        
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_content)
            
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 720})
            await page.goto(f"file://{os.path.abspath(html_path)}")
            await asyncio.sleep(2) 
            await page.screenshot(path=screenshot_path)
            await browser.close()
            
        # 3. Fayllarni xavfsiz yuborish (aiogram 3 usulida FSInputFile ishlatiladi)
        photo = FSInputFile(screenshot_path)
        voice = FSInputFile(audio_path)
        
        await message.answer_photo(photo=photo, caption="Mana sizning 3D animatsiya kadringiz!")
        await message.answer_voice(voice=voice, caption="Mana ovozli xabaringiz!")
        
        # Jarayon xabarini o'chirish
        await processing_msg.delete()
        
    except Exception as e:
        await message.answer(f"❌ Xatolik yuz berdi: {e}")
    
    finally:
        # 4. Vaqtinchalik fayllarni o'chirish (disk to'lib qolmasligi uchun)
        for path in [audio_path, html_path, screenshot_path]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except:
                    pass

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

