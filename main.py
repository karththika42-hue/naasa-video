import os, json, asyncio, subprocess, random, glob, pathlib
import requests, edge_tts

GEMINI_KEY = os.environ["GEMINI_API_KEY"]
VOICE = os.environ.get("VOICE", "en-US-AndrewNeural")
W, H = 1280, 720
OUT = pathlib.Path("out"); OUT.mkdir(exist_ok=True)
TMP = pathlib.Path("tmp"); TMP.mkdir(exist_ok=True)


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def next_topic():
    topics = [t.strip() for t in open("topics.txt", encoding="utf-8") if t.strip()]
    done = set()
    if os.path.exists("done.txt"):
        done = {t.strip() for t in open("done.txt", encoding="utf-8")}
    for t in topics:
        if t not in done:
            return t
    raise SystemExit("topics.txt-ல புது topic இல்ல. சேர்க்கவும்.")


def pick_model():
    forced = os.environ.get("GEMINI_MODEL")
    if forced:
        return forced
    r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                     params={"key": GEMINI_KEY, "pageSize": 200}, timeout=60)
    r.raise_for_status()
    names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])]
    bad = ("lite", "image", "tts", "live", "audio", "embedding", "robotics", "computer", "gemma", "learnlm")
    good = [n for n in names if "flash" in n and not any(b in n for b in bad)]
    stable = [n for n in good if "preview" not in n and "exp" not in n]
    pool = stable or good or names
    if not pool:
        raise SystemExit("Gemini model எதுவும் கிடைக்கல. API key சரியான்னு பாரு.")
    model = sorted(pool)[-1]
    print("Using model:", model)
    return model


def write_script(topic):
    prompt = f"""Write a YouTube space explainer script about: {topic}.
Style: curious, clear, dramatic hook in first sentence, no filler, about 900-1100 words,
plain narration only (no stage directions, no headings).
Return ONLY JSON: {{"title": "...", "narration": "...", "search_terms": ["6 to 8 short NASA video search terms"]}}"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{pick_model()}:generateContent?key={GEMINI_KEY}"
    r = requests.post(url, json={
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }, timeout=180)
    r.raise_for_status()
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


async def make_voice(text, path):
    await edge_tts.Communicate(text, VOICE).save(str(path))


def duration(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                   "-of", "default=nw=1:nk=1", str(path)])
    return float(out.strip())


def find_nasa_clips(terms, max_clips=8):
    urls = []
    for term in terms:
        try:
            r = requests.get("https://images-api.nasa.gov/search",
                             params={"q": term, "media_type": "video"}, timeout=60)
            items = r.json()["collection"]["items"][:5]
        except Exception:
            continue
        random.shuffle(items)
        for it in items:
            try:
                files = requests.get(it["href"], timeout=60).json()
                mp4 = [f for f in files if f.lower().endswith(".mp4")]
                pick = None
                for pref in ("~medium", "~small", "~mobile", "~orig"):
                    pick = next((f for f in mp4 if pref in f), None)
                    if pick:
                        break
                if pick and pick not in urls:
                    urls.append(pick.replace("http://", "https://"))
                    break
            except Exception:
                continue
        if len(urls) >= max_clips:
            break
    return urls


def download(url, path):
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)


def build_video(clips, total, voice_path, final_path):
    seg = total / len(clips)
    parts = []
    for i, c in enumerate(clips):
        p = TMP / f"seg{i}.mp4"
        run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(c), "-t", f"{seg:.2f}",
             "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps=30,format=yuv420p",
             "-an", "-c:v", "libx264", "-preset", "veryfast", str(p)])
        parts.append(p)
    lst = TMP / "list.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    joined = TMP / "joined.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined)])

    music = glob.glob("music/*.mp3")
    cmd = ["ffmpeg", "-y", "-i", str(joined), "-i", str(voice_path)]
    if music:
        cmd += ["-stream_loop", "-1", "-i", random.choice(music),
                "-filter_complex", "[2:a]volume=0.12[m];[1:a][m]amix=inputs=2:duration=first[a]",
                "-map", "0:v", "-map", "[a]"]
    else:
        cmd += ["-map", "0:v", "-map", "1:a"]
    cmd += ["-c:v", "copy", "-c:a", "aac", "-shortest", str(final_path)]
    run(cmd)


def main():
    topic = next_topic()
    print("Topic:", topic)
    data = write_script(topic)
    (OUT / "title.txt").write_text(data["title"], encoding="utf-8")
    (OUT / "script.txt").write_text(data["narration"], encoding="utf-8")

    voice = TMP / "voice.mp3"
    asyncio.run(make_voice(data["narration"], voice))
    total = duration(voice)

    urls = find_nasa_clips(data["search_terms"])
    if not urls:
        raise SystemExit("NASA clips கிடைக்கல")
    clips = []
    for i, u in enumerate(urls):
        p = TMP / f"clip{i}.mp4"
        try:
            download(u, p)
            clips.append(p)
        except Exception as e:
            print("skip", u, e)

    build_video(clips, total, voice, OUT / "final.mp4")
    with open("done.txt", "a", encoding="utf-8") as f:
        f.write(topic + "\n")
    print("Done:", data["title"])


if __name__ == "__main__":
    main()
