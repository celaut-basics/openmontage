"""The agent of the chat: it turns a conversation into a plan, and a plan into a video.

OpenMontage has no Python orchestrator: its pipelines are YAML manifests and Markdown
skills that a coding agent (Claude Code, Codex, Cursor...) reads and follows, with tool
calls. A Celaut service has no such agent, so this module is the thin loop that takes its
place, for one pipeline (OpenMontage's free path: archive footage + Piper + Remotion):

1. A message that describes a video: the LLM writes a plan (plan.py); the service shows it
   as a storyboard, sets the title of the conversation, and offers "Render it".
2. A message that changes the plan ("shorter", "more upbeat", "use photos of Mars"): the
   LLM writes the new plan.
3. "Render it" (or "go", "yes", "dale"...): the producer renders the plan (producer.py),
   with progress in the answer, and a second message brings the files.

With no LLM (`OPENMONTAGE_LLM_BASE_URL` not set), step 1 and 2 use rules
(`plan.plan_without_llm`): each sentence of the message is a scene.
"""

import logging
import re

from .chat import Cancelled
from .llm import LLMError
from .plan import plan_with_llm, plan_without_llm, storyboard

log = logging.getLogger("director")

RENDER = re.compile(r"(?i)^\W*(render( it| the video| now)?|go( ahead)?|yes|yep|ok(ay)?|do it|make it|"
                    r"looks good|lgtm|perfect|great|s[ií]|dale|hazlo|adelante|render[ií]zalo)\W*$")
HELP = re.compile(r"(?i)^\W*(help|ayuda|\?|what can you do\??)\W*$")
RENDER_NOW = re.compile(r"(?i)\b(render (it )?(right )?now|no questions|just make it)\b")

GREETING = """I make short narrated videos (20–40 s) with OpenMontage, from free material only: \
clips and photos from Wikimedia Commons, NASA and the Internet Archive, a local voice (Piper), \
generated music and Remotion animation. Describe the video you want; I answer with a plan, and \
render it when you say “Render it”.

Source code (AGPL-3.0): https://github.com/celaut-basics/openmontage (OpenMontage upstream: \
https://github.com/calesthio/OpenMontage at {upstream})."""

NOTICE = ("_OpenMontage on Celaut. Source (AGPL-3.0): https://github.com/celaut-basics/openmontage, "
          "upstream calesthio/OpenMontage@{upstream}._")

PLAN_SUGGESTIONS = ["Render it", "Make it shorter", "Use more photos"]
DONE_SUGGESTIONS = ["Make another one", "Same video, more upbeat", "Help"]


class Director:
    def __init__(self, llm, producer, upstream_commit):
        self.llm, self.producer, self.upstream = llm, producer, upstream_commit
        self.plan = None
        self.greeted = False
        if llm.configured:
            producer.chooser = self.choose_clip

    def choose_clip(self, scene, kind, descriptions):
        """The LLM ranks the archive results that show the scene (the one tool step of the
        loop): a list of indexes, best first, empty if none fits."""
        listing = "\n".join("%d. %s" % (i, d) for i, d in enumerate(descriptions))
        answer = self.llm.json([
            {"role": "system", "content": "You pick stock media for a video scene. Answer with ONE JSON "
                                          "object: {\"fits\": [numbers of the results that fit, best first]}."},
            {"role": "user", "content": (
                "Scene narration: %s\nWanted: a %s of “%s”.\nSearch results (title and description):\n%s\n\n"
                "Which results most likely SHOW the wanted subject? List at most 3, best first. Leave "
                "out a result about something else that only shares a word, and any TV show, movie, "
                "cartoon or commercial. Answer {\"fits\": []} if none fits."
                % (scene["narration"] or scene["on_screen"], "video clip" if kind == "video" else "photo",
                   scene["query"], listing))}], max_tokens=80)
        fits = answer.get("fits")
        return [int(i) for i in fits if isinstance(i, (int, float))][:3] if isinstance(fits, list) else []

    def answer(self, turn):
        text = turn.user_text.strip()
        if not self.greeted:
            # The first answer of the conversation says where the source is (AGPL-3.0, s. 13).
            self.greeted = True
            turn.write(NOTICE.format(upstream=self.upstream[:12]) + "\n\n")
        if HELP.match(text):
            turn.write(GREETING.format(upstream=self.upstream[:12]) + "\n\n" + self._llm_line())
            turn.finish(["A 30-second video about the Moon", "Bees and flowers, calm", "Render it"]
                        if self.plan else ["A 30-second video about the Moon", "Bees and flowers, calm"])
            return
        if RENDER.match(text) and self.plan:
            self.render(turn)
            return
        if RENDER.match(text):
            turn.write("There is no plan yet. Describe the video first, for example: "
                       "“A 30-second video about the Moon, calm music”.")
            turn.finish(["A 30-second video about the Moon", "Help"])
            return
        self.make_plan(turn, text)
        if RENDER_NOW.search(text) and self.plan:
            self.render(turn, separate=True)  # the plan message is final; the files follow

    def _llm_line(self):
        if self.llm.configured:
            return "Planner: the LLM %s." % self.llm.model
        return ("Planner: no LLM is configured (OPENMONTAGE_LLM_BASE_URL), so each sentence of "
                "your message becomes one scene.")

    def make_plan(self, turn, text):
        turn.start()
        revise = self.plan is not None
        turn.write("Revising the plan…\n\n" if revise else "Planning the video…\n\n")
        turn.progress(0.1, "planning")
        plan = None
        if self.llm.configured:
            try:
                plan = plan_with_llm(self.llm, text, self.plan if revise else None)
            except (LLMError, ValueError) as error:
                turn.check()
                log.warning("LLM plan failed: %s", error)
                turn.write("(The LLM could not plan it: %s. I plan it by rules instead.)\n\n" % error)
        turn.check()
        if plan is None:
            if revise and not self.llm.configured:
                turn.write("Without an LLM I cannot change a plan from a request, so I plan your "
                           "message as a new video.\n\n")
            plan = plan_without_llm(text)
        self.plan = plan
        turn.set_title(plan["title"])
        turn.write(storyboard(plan) + "\n\nSay “Render it” to make the video, or tell me what to change.")
        turn.finish(PLAN_SUGGESTIONS)

    def render(self, turn, separate=False):
        plan = self.plan
        if separate:
            # The plan message is final: the progress goes in the files message only.
            report = lambda fraction, text: None  # noqa: E731
        else:
            turn.start()
            turn.write("Rendering “%s” on this node's CPUs. This takes a few minutes.\n\n" % plan["title"])
            lines = []

            def report(fraction, text):
                turn.progress(fraction, text)
                if not lines or lines[-1] != text:
                    if text.startswith("Rendering frames") and lines and lines[-1].startswith("Rendering frames"):
                        return  # frame counts go in MessageProgress only
                    lines.append(text)
                    turn.write("• %s\n" % text)
        try:
            result = self.producer.render(plan, report, turn.check)
        except Cancelled:
            raise
        except Exception as error:  # noqa: BLE001
            log.exception("render failed")
            if separate:
                turn.post("The render failed: %s" % error, suggestions=["Render it", "Help"])
                return
            turn.fail("the render failed: %s" % error)
            return
        found = len(result.sources)
        summary = ("Done: %.1f s, %.1f MB, rendered in %.0f s (voice %.0f s, media %.0f s, "
                   "Remotion %.0f s)." % (result.duration, result.size / 1e6, result.timings["total_s"],
                                         result.timings["voice_s"], result.timings["visuals_s"],
                                         result.timings["remotion_s"]))
        if not separate:
            turn.write("\n" + summary + "\n")
            turn.finish()
        credits = ["**%s**" % plan["title"], summary, ""]
        if found:
            credits.append("Media (free archives; check each licence before reuse):")
            for s in result.sources:
                who = (" — %s" % s["creator"]) if s["creator"] else ""
                lic = (" (%s)" % s["license"]) if s["license"] else ""
                credits.append("• scene %d, %s: %s%s%s" % (s["scene"], s["source"], s["url"], who, lic))
        if result.fallbacks:
            credits.append("%d scene(s) had no match in the archives and became text cards." % result.fallbacks)
        credits += ["", "Voice: Piper (%s). Music: generated by the service. Composition: OpenMontage "
                    "remotion-composer. Source (AGPL-3.0): https://github.com/celaut-basics/openmontage"
                    % self.producer.voice.rsplit("/", 1)[-1].replace(".onnx", "")]
        files = [
            ("image", str(result.poster), "image/jpeg", "poster.jpg", {"alt_text": plan["title"]}),
            ("audio", str(result.audio), "audio/mpeg", "soundtrack.mp3",
             {"duration_ms": int(result.duration * 1000)}),
            ("file", str(result.video), "video/mp4", _filename(plan["title"]), {}),
        ]
        metadata = {"duration_s": round(result.duration, 2), "bytes": result.size,
                    **{k: v for k, v in result.timings.items()}}
        turn.post("\n".join(credits), files=files, suggestions=DONE_SUGGESTIONS, metadata=metadata)


def _filename(title):
    name = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-").lower()[:48] or "video"
    return name + ".mp4"
