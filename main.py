import os
import json
import uuid
import asyncio
import subprocess
import requests
from typing import Optional
from fastapi import FastAPI, BackgroundTasks, Form, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import edge_tts
from google import genai
import replicate

app = FastAPI(title="htn anni video")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

app.mount("/outputs", StaticFiles(directory=OUTPUT_DIR), name="outputs")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
REPLICATE_TOKEN = os.getenv("REPLICATE_API_TOKEN", "")

ai_client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None
os.environ["REPLICATE_API_TOKEN"] = REPLICATE_TOKEN or ""

db = {"tasks": {}}

# --- ၁။ Gemini ဖြင့် မြန်မာဇာတ်ညွှန်း ရေးသားခြင်း ---
def generate_burmese_story_script(story_prompt: str, char_desc: str):
    prompt = f"""
အောက်ပါ ဇာတ်ကောင်နှင့် ဇာတ်လမ်း Prompt အပေါ် အခြေခံပြီး ၂ မိနစ်မှ ၃ မိနစ်ခန့် ကြာမြင့်မည့် မြန်မာစကားပြော ဇာတ်လမ်းပြောပြချက်ကို ရေးပေးပါ။

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

# --- ၄။ Replicate ဖြင့် ၈ စက္ကန့် scene တစ်ခုချင်း Lip-Sync Render လုပ်ခြင်း ---
def run_single_scene_lipsync(face_image_path: str, chunk_audio_path: str, output_chunk_video: str):
    with open(face_image_path, "rb") as img_file, open(chunk_audio_path, "rb") as aud_file:
        output = replicate.run(
            "cjwbw/sadtalker:3aa3fd9411082d7bc22246720d9f16dd19304e209f473edcec400f086f000112",
            input={
                "source_image": img_file,
                "driven_audio": aud_file,
                "enhancer": "gfpgan",
                "preprocess": "full",
                "still": False,
                "expression_scale": 1.15
            }
        )

    res = requests.get(str(output), stream=True, timeout=120)
    with open(output_chunk_video, "wb") as f:
        for chunk in res.iter_content(chunk_size=8192):
            f.write(chunk)

# --- ၅။ ဗီဒီယို အပိုင်းများကို ပြန်ဆက်ခြင်း (FFmpeg Concat) ---
def concatenate_scenes(video_list: list, output_final: str):
    list_file_path = output_final.replace(".mp4", "_list.txt")
    with open(list_file_path, "w") as f:
        for v in video_list:
            f.write(f"file '{os.path.abspath(v)}'\n")

    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", list_file_path, "-c", "copy", output_final
    ]
    subprocess.run(cmd, check=True)
    if os.path.exists(list_file_path):
        os.remove(list_file_path)

# --- Background Task Pipeline ---
def run_video_pipeline(task_id: str, story_prompt: str, char_desc: str, char_image_path: str, voice_gender: str):
    try:
        db["tasks"][task_id]["status"] = "processing"

        # အဆင့် ၁ - Script ရေးခြင်း
        db["tasks"][task_id]["message"] = "AI ဖြင့် မြန်မာဇာတ်ညွှန်းကို ရေးဖွဲ့နေပါသည်..."
        script_data = generate_burmese_story_script(story_prompt, char_desc)
        title = script_data.get("title", "AI Cartoon Story")
        narration = script_data.get("narration", story_prompt)
        db["tasks"][task_id]["title"] = title

        # အဆင့် ၂ - အသံဖိုင်ထုတ်ခြင်း
        db["tasks"][task_id]["message"] = "မြန်မာအသံထွက် Voiceover ကို ဖန်တီးနေပါသည်..."
        full_audio_path = os.path.join(OUTPUT_DIR, f"{task_id}_speech.mp3")
        asyncio.run(generate_burmese_audio(narration, full_audio_path, voice_gender))

        total_audio_len = get_media_duration(full_audio_path)
        scene_duration = 8.0  # ၈ စက္ကန့် scene တစ်ခုစီ ခွဲထုတ်ခြင်း
        num_scenes = int(total_audio_len // scene_duration) + (1 if total_audio_len % scene_duration > 0 else 0)

        rendered_scenes = []

        # အဆင့် ၃ - ၈ စက္ကန့်စီ Scene တွေ ခွဲထုတ်ပြီး Lip-Sync ပြုလုပ်ခြင်း
        for idx in range(num_scenes):
            start_t = idx * scene_duration
            db["tasks"][task_id]["message"] = f"Scene {idx+1}/{num_scenes} (၈ စက္ကန့်) ကို Character မျက်နှာဖြင့် Lip-Sync ဖန်တီးနေပါသည်..."

            chunk_audio = os.path.join(OUTPUT_DIR, f"{task_id}_chunk_{idx}.mp3")
            cmd_cut = [
                "ffmpeg", "-y", "-ss", str(start_t), "-t", str(scene_duration),
                "-i", full_audio_path, "-c", "copy", chunk_audio
            ]
            subprocess.run(cmd_cut, check=True)

            chunk_video = os.path.join(OUTPUT_DIR, f"{task_id}_scene_{idx}.mp4")
            run_single_scene_lipsync(char_image_path, chunk_audio, chunk_video)
            rendered_scenes.append(chunk_video)

            if os.path.exists(chunk_audio):
                os.remove(chunk_audio)

        # အဆင့် ၄ - Scene အားလုံးကို ဗီဒီယိုတစ်ခုတည်းအဖြစ် ဆက်ခြင်း
        db["tasks"][task_id]["message"] = "၈ စက္ကန့် Scene အားလုံးကို အပြီးသတ် ဗီဒီယိုအဖြစ် ပေါင်းစပ်နေပါသည်..."
        final_video_name = f"{task_id}_final.mp4"
        final_video_path = os.path.join(OUTPUT_DIR, final_video_name)
        concatenate_scenes(rendered_scenes, final_video_path)

        for v in rendered_scenes:
            if os.path.exists(v):
                os.remove(v)

        db["tasks"][task_id]["status"] = "completed"
        db["tasks"][task_id]["message"] = f"ဗီဒီယို အောင်မြင်စွာ ဖန်တီးပြီးပါပြီ! (စုစုပေါင်း {num_scenes} Scenes)"
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

