import os
import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from gtts import gTTS
from playwright.async_api import async_playwright

TOKEN = "8754760907:AAHHdgKiTaoqyQjA1d9ZVLfaM2mYW9KLXlc"

bot = Bot(token=TOKEN)
dp = Dispatcher()

@dp.message(F.text == "/start")
async def start_cmd(message: Message):
    await message.answer("Salom! Men Meduza botiman. Menga istalgan matn yuboring, men uni ovozga aylantirib va 3D animatsiya qilib beraman!")

@dp.message()
async def handle_text(message: Message):
    text = message.text
    await message.answer("⏳ Matn qayta ishlanmoqda va 3D animatsiya yaratilmoqda...")
    
    # 1. gTTS orqali ovoz yaratish
    audio_path = "voice.mp3"
    tts = gTTS(text=text, lang='uz')
    tts.save(audio_path)
    
    # 2. Playwright orqali animatsiya render qilish
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
        </style>
    </main>
    <body>
        <div class="card">
            <h1>Meduza Animation</h1>
            <p>{text}</p>
        </div>
    </body>
    </html>
    """
    
    html_path = "index.html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
        
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 720})
        await page.goto(f"file://{os.path.abspath(html_path)}")
        await asyncio.sleep(2) # Animatsiya joylashishi uchun kutish
        await page.screenshot(path="screenshot.png")
        await browser.close()
        
    # Hozircha tayyorlangan rasm va ovozni yuboramiz
    await message.answer_photo(photo=open("screenshot.png", "rb"), caption="Mana sizning 3D animatsiya kadringiz!")
    await message.answer_voice(voice=open(audio_path, "rb"), caption="Mana ovozli xabaringiz!")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
