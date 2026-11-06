"""Render a plan into an MP4 with OpenMontage's free tools, on CPUs, with no paid API.

The steps are OpenMontage's free path ("What You Get With Zero API Keys" in its README),
run in a fixed order instead of by a coding agent:

1. voice: OpenMontage `tools/audio/piper_tts.py` (Piper, offline, CPU), one clip a scene;
2. visuals: OpenMontage `tools/video/stock_sources` (Wikimedia Commons, NASA, Internet
   Archive; Pexels and Pixabay too when their free keys are set), then FFmpeg cuts and
   scales each clip to the length of its scene;
3. music: a bed made by FFmpeg from sine tones (no file, no licence; see `music_bed`);
4. captions: word timings from the length of each voice clip;
5. composition: OpenMontage `remotion-composer` (the `Explainer` composition), rendered
   by Remotion with its headless Chromium, from a bundle made when the image was built;
6. delivery: FFmpeg makes the poster (JPEG) and the sound track (MP3).

`render(plan, report, check)` returns a `Result`. `report(fraction, text)` gets the
progress; `check()` raises to stop (the user cancelled).
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("producer")

UPSTREAM = Path(os.environ.get("OPENMONTAGE_UPSTREAM", "/opt/openmontage"))
if str(UPSTREAM) not in sys.path:
    sys.path.insert(0, str(UPSTREAM))

FPS = 30
WIDTH, HEIGHT = 1280, 720  # the delivered size; the composition is 1920x1080
MAX_DOWNLOAD = 80 << 20
MAX_DOWNLOAD_S = 60  # one file; a slow archive server must not hold the render
# Wikimedia throttles (HTTP 429) clients whose User-Agent has no contact URL
# (meta.wikimedia.org/wiki/User-Agent_policy).
USER_AGENT = "OpenMontage-Celaut/1.0 (+https://github.com/celaut-basics/openmontage) python-requests"
MAX_VIDEO_BYTES = 22 << 20  # below the 24 MiB of GetCapabilities (chat.py MAX_FILE)
SCENE_PAD = 0.45  # seconds of air after the voice of a scene
MIN_SCENE = 2.6
TITLE_SCENE = 3.0
TAIL = 1.2  # seconds after the last scene, for the music fade


@dataclass
class Result:
    video: Path
    poster: Path
    audio: Path
    duration: float
    size: int
    timings: dict
    sources: list = field(default_factory=list)  # (scene, source, title, url, licence, creator)
    fallbacks: int = 0


def run(cmd, log_path, timeout=900, cwd=None, env=None):
    """Run a command; its output goes to a file, never to the service log."""
    with open(log_path, "ab") as out:
        out.write(("\n$ %s\n" % " ".join(map(str, cmd))).encode())
        out.flush()
        proc = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT, timeout=timeout,
                              cwd=cwd, env=env)
    if proc.returncode != 0:
        raise RuntimeError("%s failed (exit %d)" % (Path(str(cmd[0])).name, proc.returncode))


def probe_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)],
                         capture_output=True, text=True, timeout=60)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def wav_seconds(path):
    with wave.open(str(path)) as w:
        return w.getnframes() / float(w.getframerate())


class Producer:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.env = env
        self.work_root = Path(env.get("OPENMONTAGE_WORK_DIR") or "/tmp/openmontage")
        self.voice = env.get("OPENMONTAGE_VOICE_MODEL") or "/opt/piper/en_US-joe-medium.onnx"
        self.voice_speed = float(env.get("OPENMONTAGE_VOICE_LENGTH_SCALE") or 1.0)
        self.bundle = Path(env.get("OPENMONTAGE_REMOTION_BUNDLE") or "/opt/remotion-bundle")
        self.composer = UPSTREAM / "remotion-composer"
        self.concurrency = int(env.get("OPENMONTAGE_RENDER_CONCURRENCY") or os.cpu_count() or 2)
        # Remotion needs integer frame sizes: 0.75 gives 1440x810 (2/3 would give 720.0004),
        # and the final encode scales to 1280x720.
        self.scale = float(env.get("OPENMONTAGE_RENDER_SCALE") or 0.75)
        self.jobs = 0
        self.used = set()  # clip_id of the candidates of this render
        # chooser(scene, kind, descriptions) -> indexes of the fitting candidates, best first
        # (director.py gives one that asks the LLM); None: shared words decide.
        self.chooser = None
        self.chooser_down = False

    # ---- sources -------------------------------------------------------------------

    def sources(self):
        from tools.video.stock_sources.archive_org import ArchiveOrgSource
        from tools.video.stock_sources.nasa import NasaSource
        from tools.video.stock_sources.wikimedia import WikimediaSource
        out = []
        if self.env.get("PEXELS_API_KEY"):
            from tools.video.stock_sources.pexels import PexelsSource
            out.append(PexelsSource())
        if self.env.get("PIXABAY_API_KEY"):
            from tools.video.stock_sources.pixabay_video import PixabayVideoSource
            out.append(PixabayVideoSource())
        out += [WikimediaSource(), NasaSource(), ArchiveOrgSource()]
        return out

    def _download(self, url, path, check):
        import requests
        with requests.get(url, stream=True, timeout=(15, 60),
                          headers={"User-Agent": USER_AGENT}) as r:
            r.raise_for_status()
            size = int(r.headers.get("content-length") or 0)
            if size > MAX_DOWNLOAD:
                raise ValueError("file of %d MB" % (size >> 20))
            got, started = 0, time.monotonic()
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    got += len(chunk)
                    if got > MAX_DOWNLOAD:
                        raise ValueError("file above %d MB" % (MAX_DOWNLOAD >> 20))
                    if time.monotonic() - started > MAX_DOWNLOAD_S:
                        raise ValueError("download slower than %d s" % MAX_DOWNLOAD_S)
                    f.write(chunk)
                    if got % (4 << 20) < (1 << 16):
                        check()
        return path

    def find_visual(self, scene, n, need, job, logs, check):
        """A local clip (mp4) or still (jpg) for the scene, or None. Return (path, kind, info).

        For each kind (video first for footage), the candidates of all sources for the
        query (then for its last two words) go into one pool, and `self.choose` orders
        them by fit: the LLM reads their titles (OpenMontage ranks its corpus with CLIP;
        a title is what a text model can read), or, with no LLM, a score of shared words.
        A candidate that does not fit is never used: the scene becomes a text card."""
        from tools.video.stock_sources.base import SearchFilters
        kinds = ["video", "image"] if scene["visual"] == "footage" else ["image"]
        queries = [scene["query"]]
        words = scene["query"].split()
        if len(words) > 2:
            queries.append(" ".join(words[-2:]))
        deadline = time.monotonic() + float(self.env.get("OPENMONTAGE_SEARCH_BUDGET_S") or 75)
        for kind in kinds:
            for query in queries:
                pool = []
                for source in self.sources():
                    if time.monotonic() > deadline or len(pool) >= 12:
                        break
                    check()
                    if not getattr(source, "supports", {}).get(kind, False):
                        continue
                    filters = SearchFilters(kind=kind, orientation="landscape", min_width=480,
                                            per_page=8, min_duration=(2.0 if kind == "video" else None),
                                            max_duration=(1800.0 if kind == "video" else None))
                    try:
                        candidates = source.search(query, filters)
                    except Exception as error:  # noqa: BLE001 (a source can fail; try the next)
                        logs.write("search %s %s %r: %s\n" % (source.name, kind, query, error))
                        continue
                    logs.write("search %s %s %r: %d\n" % (source.name, kind, query, len(candidates)))
                    pool += [(source, c) for c in candidates[:8]
                             if c.clip_id not in self.used and licence_ok(source.name, c)][:6]
                if not pool:
                    continue
                order = self.choose(scene, kind, [c for _, c in pool], logs)
                for i in order[:3]:
                    source, cand = pool[i]
                    self.used.add(cand.clip_id)  # a clip appears in one scene only
                    try:
                        path = self._fetch(cand, kind, n, need, job, logs, check)
                    except Exception as error:  # noqa: BLE001
                        logs.write("  skip %s: %s\n" % (cand.clip_id, error))
                        continue
                    return path, kind, {"source": getattr(source, "display_name", source.name),
                                        "url": cand.source_url, "license": cand.license or "",
                                        "creator": cand.creator or "", "title": describe(cand, 80),
                                        "query": query}
                if order:
                    break  # the fitting candidates failed to download: try the next kind
        return None

    def choose(self, scene, kind, candidates, logs):
        """The indexes of the candidates that fit the scene, best first."""
        scored = sorted(((overlap(scene, c), i) for i, c in enumerate(candidates)), reverse=True)
        by_words = [i for score, i in scored if score >= 2]  # at least one word of the query
        if self.chooser is None or self.chooser_down:
            return by_words
        try:
            fits = self.chooser(scene, kind, [describe(c, 160) for c in candidates])
        except Exception as error:  # noqa: BLE001 (the LLM is optional here)
            # One failure (an LLM that is down or still loading) sends the rest of this
            # render to the word score, instead of one LLM timeout for each scene.
            self.chooser_down = True
            logs.write("  chooser failed (%s): word score for the rest of the render\n" % error)
            return by_words
        fits = [i for i in fits if 0 <= i < len(candidates)]
        logs.write("  chooser: %s of %d\n" % (fits, len(candidates)))
        return list(dict.fromkeys(fits))

    def _fetch(self, cand, kind, n, need, job, logs, check):
        raw = job / ("raw%d" % n)
        if kind == "image":
            url = cand.download_url
            # Wikimedia gives a 640 px thumbnail; ask for 1280 px instead of the original.
            thumb = cand.thumbnail_url or ""
            if "/thumb/" in thumb and re.search(r"/\d+px-", thumb):
                url = re.sub(r"/\d+px-", "/1280px-", thumb)
            self._download(url, raw, check)
            out = job / "public" / ("scene%d.jpg" % n)
            run(["ffmpeg", "-y", "-v", "error", "-i", raw,
                 "-vf", "scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d" % (WIDTH, HEIGHT, WIDTH, HEIGHT),
                 "-frames:v", "1", "-q:v", "3", out], job / "ffmpeg.log", timeout=120)
            raw.unlink(missing_ok=True)
            return out
        self._download(cand.download_url, raw, check)
        length = probe_duration(raw)
        if length < 1.0:
            raise ValueError("no playable video")
        start = 0.0
        if length > need + 2:
            start = min(length * 0.15, length - need - 0.5)
        out = job / "public" / ("scene%d.mp4" % n)
        loop = ["-stream_loop", "-1"] if length < need + 0.2 else []
        run(["ffmpeg", "-y", "-v", "error", *loop, "-ss", "%.2f" % start, "-i", raw, "-t", "%.2f" % need,
             "-vf", "scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,fps=%d,format=yuv420p"
             % (WIDTH, HEIGHT, WIDTH, HEIGHT, FPS),
             "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", out],
            job / "ffmpeg.log", timeout=300)
        raw.unlink(missing_ok=True)
        return out

    # ---- audio ---------------------------------------------------------------------

    def voice_clip(self, text, path):
        from tools.audio.piper_tts import PiperTTS
        result = PiperTTS().execute({"text": text, "model": self.voice, "output_path": str(path),
                                     "length_scale": self.voice_speed, "sentence_silence": 0.25})
        if not result.success:
            raise RuntimeError("Piper: %s" % (result.error or "failed")[:300])
        return wav_seconds(path)

    def music_bed(self, mood, seconds, path, job):
        """A soft pad from sine tones (an original sound, so no licence question): a chord
        that changes every 4 bars, with a slow swell, low-passed and faded."""
        chords = {
            "calm": [(220.0, 277.18, 329.63), (196.0, 246.94, 293.66), (174.61, 220.0, 261.63), (196.0, 246.94, 329.63)],
            "upbeat": [(261.63, 329.63, 392.0), (293.66, 369.99, 440.0), (220.0, 277.18, 329.63), (246.94, 311.13, 369.99)],
            "dramatic": [(146.83, 174.61, 220.0), (130.81, 155.56, 196.0), (116.54, 146.83, 174.61), (110.0, 138.59, 164.81)],
        }[mood]
        step = 4.0
        terms = []
        for i, chord in enumerate(chords):
            gate = "between(mod(t,%g),%g,%g)" % (step * len(chords), i * step, (i + 1) * step)
            tone = "+".join("sin(2*PI*%g*t)+0.35*sin(2*PI*%g*t)" % (f, 2 * f) for f in chord)
            terms.append("%s*(%s)" % (gate, tone))
        pulse = "(0.75+0.25*sin(2*PI*t/%g))" % (1.0 if mood == "upbeat" else 4.0)
        expr = "0.05*%s*(%s)" % (pulse, "+".join(terms))
        fade = max(0.5, seconds - 3)
        run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
             "aevalsrc='%s':s=44100:d=%.2f" % (expr, seconds),  # quoted: the expression has commas
             "-af", "lowpass=f=1800,aecho=0.8:0.7:60|120:0.25|0.15,afade=t=in:d=2,afade=t=out:st=%.2f:d=3" % fade,
             "-ac", "2", "-c:a", "pcm_s16le", path], job / "ffmpeg.log", timeout=300)

    # ---- the render ----------------------------------------------------------------

    def render(self, plan, report, check):
        self.jobs += 1
        job = self.work_root / ("job%d-%d" % (self.jobs, int(time.time())))
        public = job / "public"
        public.mkdir(parents=True, exist_ok=True)
        logs = open(job / "steps.log", "a", encoding="utf-8")
        timings, t0 = {}, time.monotonic()
        scenes = plan["scenes"]
        self.used = set()
        self.chooser_down = False

        # 1. Voice.
        report(0.02, "Recording the voice-over (Piper, %s)" % Path(self.voice).stem)
        durations = []
        for n, scene in enumerate(scenes):
            check()
            seconds = 0.0
            if scene["narration"]:
                seconds = self.voice_clip(scene["narration"], public / ("voice%d.wav" % n))
            base = TITLE_SCENE if scene["visual"] == "title" else MIN_SCENE
            durations.append(max(base, seconds + SCENE_PAD if seconds else base))
        timings["voice_s"] = round(time.monotonic() - t0, 1)
        total = sum(durations) + TAIL

        # 2. Visuals.
        t1 = time.monotonic()
        visuals, sources, fallbacks = {}, [], 0
        wanted = [n for n, s in enumerate(scenes) if s["visual"] in ("footage", "photo")]
        for i, n in enumerate(wanted):
            scene = scenes[n]
            report(0.08 + 0.42 * i / max(1, len(wanted)),
                   "Finding %s for scene %d: “%s”" % ("footage" if scene["visual"] == "footage" else "a photo",
                                                     n + 1, scene["query"]))
            found = self.find_visual(scene, n, durations[n] + 0.5, job, logs, check)
            if found:
                visuals[n] = found
                sources.append(dict(found[2], scene=n + 1, kind=found[1]))
            else:
                fallbacks += 1
        timings["visuals_s"] = round(time.monotonic() - t1, 1)

        # 3. Music and the voice track.
        check()
        report(0.52, "Making the music bed and the voice track")
        t2 = time.monotonic()
        audio = {}
        voice_track = public / "narration.wav"
        self._voice_track(scenes, durations, total, public, voice_track)
        audio["narration"] = {"src": "narration.wav", "volume": 1.0}
        if plan["music"] != "none":
            self.music_bed(plan["music"], total, public / "music.wav", job)
            audio["music"] = {"src": "music.wav", "volume": 0.5, "fadeInSeconds": 1.5, "fadeOutSeconds": 2.5}
        timings["audio_s"] = round(time.monotonic() - t2, 1)

        # 4. Cuts, captions, props.
        props = self.props(plan, durations, visuals, audio)
        (job / "props.json").write_text(json.dumps(props, indent=1))

        # 5. Remotion.
        check()
        t3 = time.monotonic()
        report(0.55, "Composing %d scenes with Remotion (%.0f s of video)" % (len(scenes), total))
        raw = job / "remotion.mp4"
        self.remotion(job, raw, report, check)
        timings["remotion_s"] = round(time.monotonic() - t3, 1)

        # 6. Delivery files.
        report(0.97, "Making the poster and the sound track")
        video = job / "openmontage.mp4"
        self.deliver(raw, video, job)
        poster = job / "poster.jpg"
        # The poster is a frame of the first scene with media (else of the first scene).
        at = min(1.5, total / 3)
        for n in sorted(visuals):
            at = sum(durations[:n]) + min(1.5, durations[n] / 2)
            break
        run(["ffmpeg", "-y", "-v", "error", "-ss", "%.2f" % at, "-i", video,
             "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4", poster], job / "ffmpeg.log")
        sound = job / "soundtrack.mp3"
        run(["ffmpeg", "-y", "-v", "error", "-i", video, "-vn", "-c:a", "libmp3lame", "-b:a", "128k", sound],
            job / "ffmpeg.log")
        timings["total_s"] = round(time.monotonic() - t0, 1)
        logs.close()
        for name in ("raw", "remotion.mp4"):
            for p in job.glob(name + "*"):
                p.unlink(missing_ok=True)
        shutil.rmtree(public, ignore_errors=True)  # the media are in the video now
        return Result(video=video, poster=poster, audio=sound, duration=probe_duration(video),
                      size=video.stat().st_size, timings=timings, sources=sources, fallbacks=fallbacks)

    def _voice_track(self, scenes, durations, total, public, out):
        """One WAV: the voice of each scene at the start of the scene."""
        rate, width, channels = 22050, 2, 1
        clips = {}
        for n, scene in enumerate(scenes):
            path = public / ("voice%d.wav" % n)
            if path.exists():
                with wave.open(str(path)) as w:
                    rate, width, channels = w.getframerate(), w.getsampwidth(), w.getnchannels()
                    clips[n] = w.readframes(w.getnframes())
        frame = width * channels
        buf = bytearray(int(total * rate) * frame)
        start = 0.0
        for n, seconds in enumerate(durations):
            data = clips.get(n)
            if data:
                at = int((start + 0.15) * rate) * frame
                end = min(len(buf), at + len(data))
                buf[at:end] = data[:end - at]
            start += seconds
        with wave.open(str(out), "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(width)
            w.setframerate(rate)
            w.writeframes(bytes(buf))
        for path in public.glob("voice*.wav"):
            path.unlink()

    def props(self, plan, durations, visuals, audio):
        cuts, overlays, captions = [], [], []
        start = 0.0
        for n, (scene, seconds) in enumerate(zip(plan["scenes"], durations)):
            end = start + seconds
            cut = {"id": "scene%d" % (n + 1), "source": "", "in_seconds": round(start, 3),
                   "out_seconds": round(end, 3)}
            visual = scene["visual"]
            if n in visuals:
                path, kind, _ = visuals[n]
                cut["source"] = path.name
                if kind == "image":
                    cut["animation"] = "ken-burns" if n % 2 else "zoom-in"
                if scene["on_screen"]:
                    overlays.append({"type": "section_title", "in_seconds": round(start + 0.3, 3),
                                     "out_seconds": round(min(end, start + 3.3), 3),
                                     "text": scene["on_screen"], "position": "top-left"})
            elif visual == "title" or (n == 0 and visual in ("footage", "photo")):
                cut.update(type="hero_title", text=scene["on_screen"] or plan["title"])
            elif visual == "stat":
                cut.update(type="stat_card", stat=scene["stat"], subtitle=scene["on_screen"])
            else:
                text = scene["on_screen"] or _short(scene["narration"]) or plan["title"]
                cut.update(type="text_card", text=text)
            cuts.append(cut)
            captions += _captions(scene["narration"], start + 0.15, max(0.5, seconds - SCENE_PAD - 0.15))
            start = end
        # Hold the last frame through the tail.
        cuts[-1]["out_seconds"] = round(start + TAIL, 3)
        return {"theme": plan["theme"], "cuts": cuts, "overlays": overlays, "captions": captions,
                "audio": audio}

    def warm(self):
        """Read Remotion's Chromium and compositor once, in the background, so that their
        first start does not wait for a cold disk (a launch must connect in 25 s)."""
        def read():
            for root in (self.composer / "node_modules" / ".remotion",
                         self.composer / "node_modules" / "@remotion"):
                for path in root.rglob("*"):
                    try:
                        if path.is_file() and path.stat().st_size > (1 << 20):
                            with open(path, "rb") as f:
                                while f.read(1 << 22):
                                    pass
                    except OSError:
                        pass
            log.info("Remotion's browser and compositor are in the page cache")
        import threading
        threading.Thread(target=read, daemon=True).start()

    def remotion(self, job, out, report, check):
        try:
            self._remotion(job, out, report, check)
        except RuntimeError:
            text = (job / "remotion.log").read_bytes()[-4000:]
            if b"trying to connect to the browser" not in text:
                raise
            log.warning("Chromium did not start in time; one more try")
            report(0.55, "The browser was slow to start; trying again")
            shutil.rmtree(job / "bundle", ignore_errors=True)
            self._remotion(job, out, report, check)

    def _remotion(self, job, out, report, check):
        """Render the Explainer composition from the bundle of the image. The bundle is
        copied into the job, and the media of the job go into its public/ folder, so
        `staticFile()` finds them."""
        bundle = job / "bundle"
        shutil.copytree(self.bundle, bundle, symlinks=True)
        dest = bundle / "public"
        dest.mkdir(exist_ok=True)
        for item in (job / "public").iterdir():
            # Copies, so that a second try still has the media.
            shutil.copy2(str(item), dest / item.name)
        remotion = self.composer / "node_modules" / ".bin" / "remotion"
        cmd = [str(remotion), "render", str(bundle), "Explainer", str(out),
               "--props=%s" % (job / "props.json"), "--codec=h264", "--crf=22",
               "--scale=%g" % self.scale, "--concurrency=%d" % self.concurrency,
               "--log=info", "--timeout=120000"]
        env = dict(os.environ, TMPDIR=str(job), HOME=str(job), REMOTION_DISABLE_TELEMETRY="1")
        log_path = job / "remotion.log"
        with open(log_path, "ab") as logf:
            proc = subprocess.Popen(cmd, cwd=self.composer, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, env=env)
            last = 0.0
            try:
                for line in proc.stdout:
                    logf.write(line)
                    m = re.search(rb"Render(?:ed|ing)[^0-9]*(\d+)/(\d+)", line)
                    if m and time.monotonic() - last > 4:
                        done, all_ = int(m.group(1)), max(1, int(m.group(2)))
                        report(0.55 + 0.4 * done / all_, "Rendering frames %d/%d" % (done, all_))
                        last = time.monotonic()
                    try:
                        check()
                    except Exception:
                        proc.kill()
                        raise
                proc.wait(timeout=60)
            finally:
                if proc.poll() is None:
                    proc.kill()
        if proc.returncode != 0 or not out.exists():
            tail = log_path.read_bytes()[-600:].decode("utf-8", "replace")
            log.error("remotion failed: %s", tail)
            raise RuntimeError("the Remotion render failed (exit %s)" % proc.returncode)
        shutil.rmtree(bundle, ignore_errors=True)

    def deliver(self, raw, video, job):
        """The MP4 that the user gets: H.264 + AAC, faststart, below MAX_VIDEO_BYTES."""
        for crf in (23, 27, 31, 35):
            run(["ffmpeg", "-y", "-v", "error", "-i", raw, "-vf", "scale=%d:%d:flags=lanczos" % (WIDTH, HEIGHT),
                 "-c:v", "libx264", "-preset", "medium",
                 "-crf", str(crf), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                 "-movflags", "+faststart", video], job / "ffmpeg.log", timeout=900)
            if video.stat().st_size <= MAX_VIDEO_BYTES:
                return
        raise RuntimeError("the video is too large to send (%d MB)" % (video.stat().st_size >> 20))


def describe(cand, limit):
    """The title (and the start of the description) of a candidate, one line."""
    text = " ".join((cand.source_tags or cand.source_id or "").split())
    return text[:limit]


def licence_ok(source, cand):
    """Use a free licence only. Commons, NASA, Pexels and Pixabay are free by their terms;
    the Internet Archive search of OpenMontage covers `opensource_movies`, where users
    upload anything (VHS rips of TV shows too), so an Archive item needs a Creative
    Commons or public-domain licence URL, or a public-domain collection."""
    if source != "archive_org":
        return True
    text = ("%s %s" % (cand.license, (cand.extra or {}).get("collection", ""))).lower()
    if "verify per item" in text and "creativecommons.org" not in text:
        return False
    return any(k in text for k in ("creativecommons.org", "publicdomain", "public domain (prelinger",
                                   "public domain (archive.org home movies", "prelinger"))


def overlap(scene, cand):
    """How many words of the query (twice) and of the narration the candidate's text has."""
    words = set(re.findall(r"[a-z]{3,}", (cand.source_tags or "").lower()))
    query = set(re.findall(r"[a-z]{3,}", scene["query"].lower()))
    said = set(re.findall(r"[a-z]{4,}", scene["narration"].lower()))
    return 2 * len(query & words) + len(said & words)


def _short(text, words=7):
    parts = (text or "").split()
    return " ".join(parts[:words]) + ("…" if len(parts) > words else "")


def _captions(text, start, seconds):
    """Word captions (OpenMontage CaptionOverlay: word, startMs, endMs), timed by the
    length of each word across the voice of the scene."""
    words = (text or "").split()
    if not words:
        return []
    weights = [len(w) + 2 for w in words]
    unit = seconds / float(sum(weights))
    out, t = [], start
    for i, (word, weight) in enumerate(zip(words, weights)):
        end = t + weight * unit
        out.append({"word": word, "startMs": int(t * 1000), "endMs": int(end * 1000),
                    "pageBreakAfter": bool(re.search(r"[.!?]$", word)) or i == len(words) - 1})
        t = end
    return out

