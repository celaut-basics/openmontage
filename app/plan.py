"""The plan of a video: what the LLM writes, what the producer renders.

A plan is a small JSON object (OpenMontage's `script` + `scene_plan` artifacts, cut down
to what a mid-size open model writes reliably):

    {"title": "...", "theme": "flat-motion-graphics", "music": "calm",
     "scenes": [{"visual": "footage", "query": "ocean waves", "on_screen": "...",
                 "stat": "", "narration": "One or two sentences."}, ...]}

`visual` is one of:
- footage: a clip found by `query` in the free archives (Wikimedia Commons, NASA, Internet
  Archive); a photo if no clip is found; a text card if nothing is found;
- photo: a still image found by `query`, animated (Ken Burns);
- title: a title card (`on_screen`);
- stat: a big number (`stat`) with a line of text (`on_screen`);
- text: a card of large text (`on_screen`).
"""

import json
import re

THEMES = ("flat-motion-graphics", "clean-professional", "minimalist-diagram", "anime-ghibli")
MUSIC = ("calm", "upbeat", "dramatic", "none")
VISUALS = ("footage", "photo", "title", "stat", "text")
MIN_SCENES, MAX_SCENES = 3, 8
MAX_NARRATION_WORDS = 130  # about 45 s of Piper speech at length_scale 1.0

SYSTEM = """You are the director of OpenMontage, a video production system. You plan short \
narrated videos (20 to 40 seconds) that are made only from free material: video clips and \
photos from Wikimedia Commons, the NASA image library and the Internet Archive, animated \
title/stat/text cards, a local text-to-speech voice and generated background music.

Answer with ONE JSON object and nothing else, with this shape:
{"title": string (max 60 characters),
 "theme": one of "flat-motion-graphics", "clean-professional", "minimalist-diagram", "anime-ghibli",
 "music": one of "calm", "upbeat", "dramatic", "none",
 "scenes": [ {"visual": one of "footage", "photo", "title", "stat", "text",
              "query": string (2 to 4 plain English words that name something visible, for a search in the archives; empty for title/stat/text),
              "on_screen": string (max 8 words shown on screen; required for title, stat and text),
              "stat": string (a short number like "384,400 km"; only for visual "stat"),
              "narration": string (what the voice says during the scene: one or two short sentences)} ]}

Rules:
- 4 to 6 scenes. Each narration has 12 to 22 words (one or two sentences). All narration \
together has 60 to 100 words, so the video lasts 20 to 40 seconds.
- The first scene is a "title" scene or a strong "footage" scene. At least 3 scenes are "footage" \
or "photo"; at most one scene is "stat" and at most one is "text".
- Queries name concrete, filmable things that public archives have: "moon surface", "ocean waves", \
"city traffic at night", "honey bee flower", "saturn rings". Never names of people, brands or logos.
- Use a "stat" scene only for a real, well-known number. Do not invent facts.
- Narration is plain spoken English: no lists, no emojis, no stage directions, no URLs.
- Write in English, also if the request is in another language (the voice is English)."""

REVISE = """Here is the current plan of the video, as JSON:
{plan}

The user asks for this change: "{request}"

Answer with the complete new plan as ONE JSON object of the same shape, following the same \
rules. If the user describes a different video, write a new plan for it."""

STOPWORDS = set("""a an the and or but of to in on at for with about from by as is are was were be
been being it its this that these those make makes made create video videos short film about
please want would like can could should me my we our you your i film clip seconds second
minute minutes long explain explainer show shows showing narrated narration voice
quick fast how why what when where who which into over under very really just also
every each all some many more most other than then there their they them its has have had
will one two three""".split())


def _clean(text, limit):
    text = " ".join(str(text or "").split())
    return text[:limit].strip()


def _words(text, n):
    words = str(text or "").split()
    return " ".join(words[:n]).rstrip(",;:.") + ("…" if len(words) > n else "")


def normalize(raw):
    """A plan that the producer can render, from the JSON of a model. Raise ValueError if
    it cannot be one."""
    if not isinstance(raw, dict):
        raise ValueError("the plan is not an object")
    scenes = []
    for item in raw.get("scenes") or []:
        if not isinstance(item, dict):
            continue
        visual = str(item.get("visual") or "footage").strip().lower()
        if visual not in VISUALS:
            visual = "footage"
        scene = {
            "visual": visual,
            "query": _clean(item.get("query"), 60),
            "on_screen": _clean(item.get("on_screen"), 60),
            "stat": _clean(item.get("stat"), 24),
            "narration": _clean(item.get("narration"), 400),
        }
        if not scene["narration"] and not scene["on_screen"] and not scene["stat"] and not scene["query"]:
            continue  # an empty scene
        if visual in ("footage", "photo") and not scene["query"]:
            scene["query"] = keywords(scene["narration"] or scene["on_screen"])
        if visual in ("footage", "photo") and not scene["query"]:
            visual = scene["visual"] = "text"
        if visual == "stat" and not scene["stat"]:
            visual = scene["visual"] = "text"
        if visual == "stat" and scene["on_screen"] == scene["stat"]:
            scene["on_screen"] = ""
        if visual in ("title", "text") and not scene["on_screen"]:
            scene["on_screen"] = _words(scene["narration"], 7) or _clean(raw.get("title"), 48)
        if not scene["narration"] and not scene["on_screen"]:
            continue
        scenes.append(scene)
    scenes = scenes[:MAX_SCENES]
    if len(scenes) < 2:
        raise ValueError("the plan has fewer than 2 scenes")
    # Keep the voice-over in the length of a short video.
    words = 0
    for scene in scenes:
        n = len(scene["narration"].split())
        if words + n > MAX_NARRATION_WORDS:
            scene["narration"] = " ".join(scene["narration"].split()[:max(0, MAX_NARRATION_WORDS - words)])
            n = len(scene["narration"].split())
        words += n
    title = _clean(raw.get("title"), 60) or scenes[0]["on_screen"] or "Untitled video"
    theme = str(raw.get("theme") or "").strip().lower()
    music = str(raw.get("music") or "").strip().lower()
    return {"title": title, "theme": theme if theme in THEMES else THEMES[0],
            "music": music if music in MUSIC else "calm", "scenes": scenes}


def keywords(text, n=3):
    """The names and the longest words of a sentence that are not stop words: a search
    query. A capitalized word after the first one is a name ("Moon", "Saturn") and counts
    first."""
    found = list(re.finditer(r"[A-Za-z][A-Za-z'-]+", text or ""))
    words = [(m.group(), m.start() > 0 and m.group()[0].isupper()) for m in found
             if m.group().lower() not in STOPWORDS]
    seen, out = set(), []
    for word, _ in sorted(words, key=lambda w: (w[1], len(w[0])), reverse=True):
        if word.lower() not in seen:
            seen.add(word.lower())
            out.append(word.lower())
        if len(out) == n:
            break
    # Keep the order of the sentence.
    return " ".join(sorted(out, key=lambda w: text.lower().find(w)))


MIN_WORDS = 45  # below this the video is shorter than about 20 s

LONGER = """The narration has only {words} words, so the video is too short. Answer with the \
same plan as ONE JSON object, but give each scene a narration of 12 to 22 words, for 60 to \
100 words in total."""


def plan_with_llm(llm, request, current=None):
    if current is None:
        messages = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": "Plan this video: " + request}]
    else:
        messages = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": REVISE.format(plan=json.dumps(current), request=request)}]
    raw = llm.json(messages)
    plan = normalize(raw)
    words = sum(len(s["narration"].split()) for s in plan["scenes"])
    if words < MIN_WORDS and not re.search(r"(?i)\b(short|shorter|brief|quick)\b", request):
        messages += [{"role": "assistant", "content": json.dumps(raw)},
                     {"role": "user", "content": LONGER.format(words=words)}]
        try:
            longer = normalize(llm.json(messages))
            if sum(len(s["narration"].split()) for s in longer["scenes"]) > words:
                plan = longer
        except ValueError:
            pass
    return plan


def plan_without_llm(request):
    """A plan made by rules, for a service with no LLM: each sentence of the request is
    one scene, said by the voice, over footage found by its key words. A request can also
    be a structured brief, one scene per line:

        Title: The Moon
        footage | moon surface | The Moon is the only natural satellite of Earth.
        stat | 384,400 km | It orbits at an average distance of 384,400 kilometres.
    """
    lines = [l.strip() for l in request.strip().splitlines() if l.strip()]
    title, scenes = "", []
    for line in lines:
        head = re.match(r"(?i)title\s*:\s*(.+)", line)
        if head:
            title = head.group(1)
            continue
        cells = [c.strip() for c in line.split("|")]
        if len(cells) >= 3 and cells[0].lower() in VISUALS:
            visual, value, narration = cells[0].lower(), cells[1], "|".join(cells[2:])
            scene = {"visual": visual, "narration": narration, "query": "", "on_screen": "", "stat": ""}
            if visual in ("footage", "photo"):
                scene["query"] = value
            elif visual == "stat":
                scene["stat"], scene["on_screen"] = value, keywords(narration, 4)
            else:
                scene["on_screen"] = value
            scenes.append(scene)
    if not scenes:
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", " ".join(lines)) if s.strip()]
        title = title or _clean(re.sub(r"(?i)^(make|create|produce)\s+(me\s+)?(a|an)\s+", "", sentences[0]), 60)
        scenes.append({"visual": "title", "on_screen": title, "narration": "", "query": "", "stat": ""})
        for sentence in sentences[:MAX_SCENES - 1]:
            scenes.append({"visual": "footage", "query": keywords(sentence), "narration": sentence,
                           "on_screen": "", "stat": ""})
        if len(scenes) == 2 and not scenes[1]["narration"].strip():
            scenes.append({"visual": "text", "on_screen": title, "narration": "", "query": "", "stat": ""})
    return normalize({"title": title, "scenes": scenes, "theme": THEMES[0], "music": "calm"})


def storyboard(plan):
    """The plan as text for the user."""
    out = ["**%s**  (theme %s, music %s)" % (plan["title"], plan["theme"], plan["music"]), ""]
    for n, scene in enumerate(plan["scenes"], 1):
        what = {"footage": "clip of “%s”" % scene["query"], "photo": "photo of “%s”" % scene["query"],
                "title": "title card “%s”" % scene["on_screen"],
                "stat": "stat card %s — %s" % (scene["stat"], scene["on_screen"]),
                "text": "text card “%s”" % scene["on_screen"]}[scene["visual"]]
        line = "%d. %s" % (n, what)
        if scene["narration"]:
            line += "\n   Voice: %s" % scene["narration"]
        out.append(line)
    words = sum(len(s["narration"].split()) for s in plan["scenes"])
    out += ["", "About %d words of voice-over, roughly %d seconds." % (words, max(15, round(words / 2.6 + 3)))]
    return "\n".join(out)
