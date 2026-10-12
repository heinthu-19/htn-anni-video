import os
import json
import uuid
import asyncio
import subprocess
from typing import Optional
from fastapi import FastAPI, BackgroundTasks, Form, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import edge_tts
from google import genai
from PIL import Image

app = FastAPI(title="htn anni video")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

app.mount("/outputs", StaticFiles(directory=OUTPUT_DIR), name="outputs")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
ai_client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None

db = {"tasks": {}}

# --- ၁။ Gemini ဖြင့် မြန်မာဇာတ်ညွှန်း ရေးသားခြင်း ---
def generate_burmese_story_script(story_prompt: str, char_desc: str):
    prompt = f"""
အောက်ပါ ဇာတ်ကောင်နှင့် ဇာတ်လမ်း Prompt အပေါ် အခြေခံပြီး ၂ မိနစ်ခန့် ကြာမြင့်မည့် မြန်မာစကားပြော ဇာတ်လမ်းပြောပြချက်ကို ရေးပေးပါ။

ဇာတ်ကောင် ဖော်ပြချက်: {char_desc}
ဇာတ်လမ်း အကြောင်းအရာ: {story_prompt}

စည်းကမ်းချက်များ:
၁။ မြန်မာစာလုံးပေါင်း သတ်ပုံကို အတိအကျ သုံးပါ။ အင်္ဂလိပ်စာလုံး လုံးဝ မပါရပါ။
၂။ ဂဏန်းများကို စာဖြင့်သာ ရေးပါ (ဥပမာ- တစ်၊ နှစ်၊ သုံး)။
၃။ ဇာတ်ကောင်နာမည်၊ စင်တင်ညွှန်ကြားချက်များ လုံးဝ မထည့်ပါနှင့်။
၄။ ရှင်းလင်းစွာ အသံထွက်နိုင်သော မြန်မာစကားလုံး ၃၀၀ မှ ၄၀၀ ကြား ရေးပေးပါ။

Return ONLY JSON:
{{
  "title": "ဆွဲဆောင်မှုရှိသော မြန်မာခေါင်းစဉ်",
  "narration": "အသံဖတ်ရန် မြန်မာစကားပြော စာသားအပြည့်အစုံ"
}}
"""
    models = ["gemini-3.8-flash", "gemini-1.5-flash", "gemini-1.5-pro"]
    last_err = None
    for m in models:
        try:
            res = ai_client.models.generate_content(
                model=m,
                contents=prompt,
                config={"response_mime_type": "application/json"}
            )
            return json.loads(res.text)
        except Exception as e:
            last_err = e
            continue
    raise Exception(f"Script Generation Error: {last_err}")

# --- ၂။ မြန်မာအသံထွက် ထုတ်ယူခြင်း (Edge-TTS) ---
async def generate_burmese_audio(text: str, output_path: str, voice_gender: str):
    voice = "my-MM-ThihaNeural" if voice_gender == "male" else "my-MM-NilarNeural"
    communicate = edge_tts.Communicate(text, voice, rate="-4%")
    await communicate.save(output_path)

# --- ၃။ Audio ကြာချိန် စစ်ဆေးခြင်း ---
def get_media_duration(path: str) -> float:
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return float(res.stdout.strip())

# --- ၄။ ပုံကို FFmpeg Zoompan အသုံးပြုရန် Standard Portrait Size သို့ ပြောင်းလဲခြင်း ---
def resize_and_pad_image(input_path: str, output_path: str):
    # Portrait visual format: 720x1280 (သို့မဟုတ် 9:16)
    target_width = 720
    target_height = 1280
    
    with Image.open(input_path) as img:
        img_w, img_h = img.size
        # Aspect ratio ညှိခြင်း
        ratio = min(target_width / img_w, target_height / img_h)
        new_w = int(img_w * ratio)
        new_h = int(img_h * ratio)
        resized_img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        
        # Background black fill
        new_img = Image.new("RGB", (target_width, target_height), (11, 15, 25)) # #0B0F19 background
        offset_x = (target_width - new_w) // 2
        offset_y = (target_height - new_h) // 2
        new_img.paste(resized_img, (offset_x, offset_y))
        new_img.save(output_path, "JPEG", quality=95)

# --- ၅။ FFmpeg Motion Engine (2.5D Camera Work, Zoom-Pan + Dynamic Visual Wave Overlay) ---
def build_cinematic_motion_video(image_path: str, audio_path: str, title: str, subtitle_text: str, final_output: str, duration: float):
    # ယာယီ resize ပုံလုပ်ခြင်း
    scaled_img = image_path.replace(".png", "_scaled.jpg").replace(".jpg", "_scaled.jpg")
    resize_and_pad_image(image_path, scaled_img)
    
    # Text Layer အတွက် subtitles အပိုင်းအဖြတ်များ ထည့်ရန် file စီစဉ်ခြင်း
    # ၈ စက္ကန့်စီ scene dynamic zoom dynamic oscillation ပြုလုပ်ပါမည်
    # filter_complex တွင် zoompan ကို သဘာဝကျကျ အသက်ရှူလှုပ်ရှားမှု overlays ပုံစံ wave visualizer များနှင့် ပေါင်းစပ်ပါသည်
    
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-framerate", "25", "-i", scaled_img,
        "-i", audio_path,
        "-filter_complex", (
            # 2.5D subtle panning, breathing, zooming motion effect
            "[0:v]scale=1280:2276,zoompan=z='min(zoom+0.0006,1.15)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=720x1280, "
            # ဇာတ်လမ်းခေါင်းစဉ် Text Layer
            f"drawtext=text='{title}':fontcolor=white:fontsize=36:x=(w-text_w)/2:y=80:bordercolor=black:borderw=4:font='Arial', "
            # Subtitle (အပြောအသံစာသား) Box အနိမ့်
            f"drawtext=text='{subtitle_text[:40]}':fontcolor=orange:fontsize=24:x=(w-text_w)/2:y=h-160:bordercolor=black:borderw=3:font='Arial', "
            f"drawtext=text='{subtitle_text[40:80]}':fontcolor=orange:fontsize=24:x=(w-text_w)/2:y=h-120:bordercolor=black:borderw=3:font='Arial'[v]; "
            # အောက်ခြေမှ သဘာဝကျကျ လှိုင်းအတက်အကျဖြစ်စေသော Visualizer Effect Overlay
            "[1:a]showwaves=s=720x100:mode=line:rate=25:colors=0x6366F1[wave]; "
            "[v][wave]overlay=x=0:y=H-280:shortest=1[outv]"
        ),
        "-map", "[outv]",
        "-map", "1:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", str(duration),
        "-c:a", "aac", "-b:a", "128k",
        final_output
    ]
    
    subprocess.run(cmd, check=True)
    if os.path.exists(scaled_img):
        os.remove(scaled_img)

# --- Background Task Pipeline ---
def run_video_pipeline(task_id: str, story_prompt: str, char_desc: str, char_image_path: str, voice_gender: str):
    try:
        db["tasks"][task_id]["status"] = "processing"

        # အဆင့် ၁: ဇာတ်ညွှန်းရေးခြင်း
        db["tasks"][task_id]["message"] = "AI ဖြင့် မြန်မာဇာတ်ညွှန်းကို ရေးဖွဲ့နေပါသည်..."
        script_data = generate_burmese_story_script(story_prompt, char_desc)
        title = script_data.get("title", "AI Cartoon Story")
        narration = script_data.get("narration", story_prompt)
        db["tasks"][task_id]["title"] = title

        # အဆင့် ၂: မြန်မာအသံထွက်ဖိုင် ထုတ်ခြင်း
        db["tasks"][task_id]["message"] = "မြန်မာအသံထွက် Voiceover အသံဖိုင် ဖန်တီးနေပါသည်..."
        audio_path = os.path.join(OUTPUT_DIR, f"{task_id}_speech.mp3")
        asyncio.run(generate_burmese_audio(narration, audio_path, voice_gender))

        total_duration = get_media_duration(audio_path)

        # အဆင့် ၃: 2.5D Animation Video render ပြုလုပ်ခြင်း
        db["tasks"][task_id]["message"] = "2.5D Cinematic Motion Video ဖန်တီးနေပါသည် (၁ မိနစ်ခန့် စောင့်ပါ)..."
        final_video_name = f"{task_id}_final.mp4"
        final_video_path = os.path.join(OUTPUT_DIR, final_video_name)
        
        build_cinematic_motion_video(
            char_image_path, audio_path, title, narration, final_video_path, total_duration
        )

        # ပြီးဆုံးခြင်း
        db["tasks"][task_id]["status"] = "completed"
        db["tasks"][task_id]["message"] = "ဗီဒီယို အောင်မြင်စွာ ဖန်တီးပြီးပါပြီ!"
        db["tasks"][task_id]["result_url"] = f"/outputs/{final_video_name}"

    except Exception as e:
        db["tasks"][task_id]["status"] = "failed"
        db["tasks"][task_id]["message"] = f"Error: {str(e)}"

# --- Endpoints ---
@app.post("/api/create-video")
async def create_video(
    background_tasks: BackgroundTasks,
    story_prompt: str = Form(...),
    char_desc: str = Form(default="Main Anime Character"),
    voice_gender: str = Form("male"),
    char_image: UploadFile = File(...)
):
    task_id = str(uuid.uuid4())[:8]
    ext = os.path.splitext(char_image.filename)[-1] or ".png"
    img_path = os.path.join(OUTPUT_DIR, f"{task_id}_character{ext}")
    
    with open(img_path, "wb") as f:
        f.write(await char_image.read())

    db["tasks"][task_id] = {
        "id": task_id,
        "status": "queued",
        "message": "စတင်ရန် တန်းစီနေပါသည်...",
        "title": "",
        "result_url": None
    }

    background_tasks.add_task(run_video_pipeline, task_id, story_prompt, char_desc, img_path, voice_gender)
    return {"task_id": task_id}

@app.get("/api/task/{task_id}")
def get_task(task_id: str):
    task = db["tasks"].get(task_id)
    if not task:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    return task

@app.get("/", response_class=FileResponse)
def index_page():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
