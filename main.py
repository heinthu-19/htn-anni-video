import os
import json
import uuid
import time
import base64
import asyncio
import subprocess
import requests
from typing import Optional
from fastapi import FastAPI, BackgroundTasks, Form, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import edge_tts
from google import genai

app = FastAPI(title="htn anni video")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

app.mount("/outputs", StaticFiles(directory=OUTPUT_DIR), name="outputs")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
# Secret key နေရာတွင် အသစ်ရရှိထားသော token ကိုသာ တိုက်ရိုက်ထည့်သွင်းနိုင်ပါသည်
KLING_API_KEY = os.getenv("KLING_ACCESS_KEY", "")
KLING_BASE_URL = "https://api.klingai.com/v1"

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
၄။ ရှင်းလင်းစွာ အသံထွက်နိုင်သော မြန်မာစကားလုံး ၂၅၀ မှ ၃၅၀ ကြား ရေးပေးပါ။

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

# --- ၃။ Kling AI ဖြင့် Video Generate လုပ်ခြင်း (JWT မလိုဘဲ Token တိုက်ရိုက်သုံးခြင်း) ---
def run_kling_image_to_video(image_path: str, motion_prompt: str, output_video_path: str):
    if not KLING_API_KEY:
        raise Exception("KLING_ACCESS_KEY (API KEY) ထည့်သွင်းထားခြင်း မရှိသေးပါ။")

    url = f"{KLING_BASE_URL}/videos/image2video"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {KLING_API_KEY}"
    }

    with open(image_path, "rb") as img_file:
        img_b64 = base64.b64encode(img_file.read()).decode("utf-8")

    payload = {
        "model_name": "kling-v1",
        "image": img_b64,
        "prompt": motion_prompt,
        "cfg_scale": 0.5,
        "mode": "std",
        "duration": 5
    }

    res = requests.post(url, headers=headers, json=payload, timeout=60).json()
    if res.get("code") != 0:
        raise Exception(f"Kling AI Error: {res.get('message')}")

    task_id = res["data"]["task_id"]
    query_url = f"{KLING_BASE_URL}/videos/image2video/{task_id}"

    # Video render ပြီးသည်အထိ စောင့်ဆိုင်းခြင်း
    max_retries = 35
    for _ in range(max_retries):
        time.sleep(10)
        q_res = requests.get(query_url, headers=headers, timeout=60).json()
        if q_res.get("code") != 0:
            continue
        status = q_res["data"]["task_status"]
        if status == "succeed":
            video_url = q_res["data"]["task_result"]["videos"][0]["url"]
            v_download = requests.get(video_url, stream=True, timeout=120)
            with open(output_video_path, "wb") as f:
                for chunk in v_download.iter_content(chunk_size=8192):
                    f.write(chunk)
            return output_video_path
        elif status == "failed":
            raise Exception(f"Kling Generation Failed: {q_res['data'].get('task_status_msg')}")

    raise Exception("Kling AI Video Timeout ဖြစ်သွားပါသည် (အချိန်ကြာမြင့်လွန်းပါသည်)")

# --- ၄။ ဗီဒီယိုနှင့် မြန်မာအသံဖိုင်ကို ပေါင်းစပ်ခြင်း (FFmpeg) ---
def merge_video_with_audio(video_path: str, audio_path: str, final_output_path: str):
    cmd = [
        "ffmpeg", "-y", "-stream_loop", "-1", "-i", video_path,
        "-i", audio_path, "-shortest", "-c:v", "copy", "-c:a", "aac",
        final_output_path
    ]
    subprocess.run(cmd, check=True)

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

        # အဆင့် ၃: Kling AI ဖြင့် Video ဖန်တီးခြင်း
        db["tasks"][task_id]["message"] = "Kling AI ဖြင့် Character ရုပ်ထွက်မြင့် Animation ထုတ်လုပ်နေပါသည် (၁-၂ မိနစ်ခန့် စောင့်ပါ)..."
        kling_video_path = os.path.join(OUTPUT_DIR, f"{task_id}_kling.mp4")
        motion_prompt = f"2.5D animation style, {char_desc}, character talking smoothly, consistent background, high quality anime cinematography"
        run_kling_image_to_video(char_image_path, motion_prompt, kling_video_path)

        # အဆင့် ၄: အသံနှင့် ဗီဒီယို ပေါင်းစပ်ခြင်း
        db["tasks"][task_id]["message"] = "မြန်မာအသံနှင့် ဗီဒီယိုကို ပေါင်းစပ်နေပါသည်..."
        final_video_name = f"{task_id}_final.mp4"
        final_video_path = os.path.join(OUTPUT_DIR, final_video_name)
        merge_video_with_audio(kling_video_path, audio_path, final_video_path)

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
    char_desc: str = Form(default="Anime character"),
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
