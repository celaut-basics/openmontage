# openmontage — OpenMontage as a Celaut service

A chat service that makes short narrated videos. Describe the video in a conversation; the
service answers with a plan (a storyboard); say **“Render it”** and it renders an MP4 of
20–40 s at 1280×720, made **only from free material** on the node's CPUs:

- footage and photos from **Wikimedia Commons**, the **NASA** image library and the
  **Internet Archive** (no API key);
- a voice-over by **Piper** (offline TTS, voice `en_US-joe-medium`, dataset CC0);
- a music bed that the service **generates** with FFmpeg (no file, no licence question);
- word captions, title / stat / text cards and Ken Burns motion from OpenMontage's
  **Remotion** composer, rendered by Remotion's headless Chromium.

No paid API is called. The planner is an LLM of your choice behind an **OpenAI-compatible**
endpoint (a local Ollama is enough); with no LLM, the service plans by rules.

It is [calesthio/OpenMontage](https://github.com/calesthio/OpenMontage) (“the first
open-source agentic video production system”), pinned at commit
`d21f5d4eaa08422b09c7091757a05affcbabb35b` (2026-11-03), wrapped in the chat interface of
chatui ([docs/celaut/chat.md](https://github.com/celaut-project/chatui/blob/master/docs/celaut/chat.md),
revision 6, gRPC over h2c).

**Release [v2](https://github.com/celaut-basics/openmontage/releases/tag/v2):** `d5dc1e.celaut.bee`
(linux/amd64), service id `d5dc1e8d18b3269c247d6df8d32c8547720dfbd921b97115b5e24e641b7cc703`
— `nodo import d5dc1e.celaut.bee`. Packed and tested with nodo `dev` a97148dd (2026-11-08):
clean-registry import, then a plan and a 20 s render. Release v1 (`9321d7`) also still works on that dev.

## Source code and licence (AGPL-3.0)

OpenMontage is licensed under the **GNU Affero General Public License v3.0**, and so is this
repository ([LICENSE](LICENSE)). A network service under the AGPL must offer its source to
its users (section 13). The source of every instance of this service is:

- this repository, **https://github.com/celaut-basics/openmontage** (the service id of a
  release names the commit it was packed from, in the release notes);
- OpenMontage itself at the commit above: the submodule [`upstream/`](upstream) and the
  `OPENMONTAGE_COMMIT` argument of [amd64/.service/Dockerfile](amd64/.service/Dockerfile)
  (`tests/test_layout.py` checks that the two agree).

The service says so to its users: the first answer of each conversation starts with the
source notice, and the `prose` of the chat slot in `service.json` (what a client shows
about the service) ends with the source URL.

Other licences in the image: Piper (`piper-tts` 1.8.0) is GPL-3.0-or-later; the voice
dataset is CC0; Remotion is under the [Remotion License](https://www.remotion.dev/license)
(free for individuals and companies of up to 3 people; a larger company that renders with
it needs a company licence); FFmpeg (Debian build) is GPL. Each video lists the media it
used, with its source page, creator and licence.

## Architecture

```
chatui / any client ── gRPC (h2c) ──> app/server.py   chat.md rev 6: ChatService + FileService
                                        app/chat.py     history, streaming, files, title, suggestions
                                        app/director.py the agent loop (plan → revise → render)
                                          ├─ app/llm.py      OpenAI-compatible /v1/chat/completions, JSON mode
                                          ├─ app/plan.py     plan schema, prompts, checks; rule planner without LLM
                                          └─ app/producer.py OpenMontage free path, in a fixed order:
                                               1 Piper           upstream tools/audio/piper_tts.py
                                               2 media search    upstream tools/video/stock_sources (wikimedia, nasa, archive_org)
                                                                 → the LLM ranks the results by title; FFmpeg cuts/scales
                                               3 music bed       FFmpeg sine pad; voice track; word captions
                                               4 composition     upstream remotion-composer “Explainer”, pre-bundled
                                               5 delivery        H.264/AAC MP4 ≤ 22 MB, JPEG poster, MP3 sound track
                                        app/egress.py   the hosts the process may reach (see Networks)
```

**Why a loop of its own.** OpenMontage has no Python orchestrator on purpose: “the agent
IS the intelligence”. Its 12 pipelines are YAML manifests plus ~700 Markdown skill files
that a coding agent (Claude Code, Codex, Cursor…) reads and follows, calling 100+ Python
tools. A Celaut service has no such agent, and a mid-size open model cannot drive that
whole manual reliably. So the service runs **one** pipeline — OpenMontage's free path
(“What You Get With Zero API Keys”: archive footage + Piper + Remotion + FFmpeg) — with a
thin loop: the LLM writes and revises a small JSON plan (OpenMontage's script + scene plan,
cut down), and ranks the search results of each scene (the one tool-use step); everything
else is deterministic and uses OpenMontage's own tools and composer.

**The conversation.** One instance is one conversation (chat.md). The first message gets a
plan, the conversation gets the plan's title, and the answer offers the suggestions
“Render it”, “Make it shorter”, “Use more photos”. Any other message revises the plan.
“Render it” (or go / yes / dale / hazlo…) starts the render: the answer streams progress
lines and `MessageProgress` events; when it is done, a **second assistant message** brings
the files, because a part that is not text cannot be added to an open message (chat.md
5.2):

| Part | Kind | Media type | Why |
|---|---|---|---|
| the poster | `image` | image/jpeg | chatui shows it in the chat |
| the sound track | `audio` | audio/mpeg | chatui plays it |
| the video | `file` | video/mp4 | the deliverable; chatui revision 6 does not show `file` parts yet |

`GetCapabilities`: the user sends text only (`accepted_mime_types` empty); `max_file_bytes`
is 24 MiB (the client caches 24 MiB); the video is encoded to stay ≤ 22 MB.
`cancel_message` stops a render at its next step.

## Environment variables

| Variable | Default | Sensitive | Meaning |
|---|---|---|---|
| `OPENMONTAGE_LLM_BASE_URL` | none (rule planner) | no | OpenAI-compatible endpoint, with or without `/v1`, e.g. `http://192.168.200.1:11434/v1` |
| `OPENMONTAGE_LLM_MODEL` | `qwen2.5:14b-instruct` | no | model name at that endpoint |
| `OPENMONTAGE_LLM_API_KEY` | none | **yes** | Bearer key of the endpoint; sent only to that host, never logged or shown |
| `OPENMONTAGE_LLM_TIMEOUT_S` | 300 | no | one LLM call |
| `OPENMONTAGE_LLM_TEMPERATURE` | 0.4 | no | |
| `PEXELS_API_KEY` | none | **yes** | optional free key: adds Pexels footage (and its hosts to the egress list) |
| `PIXABAY_API_KEY` | none | **yes** | optional free key: adds Pixabay footage |
| `OPENMONTAGE_DNS_SERVERS` | `1.1.1.1 9.9.9.9 8.8.8.8` under nodo | no | resolvers written to /etc/resolv.conf at start |
| `OPENMONTAGE_RENDER_CONCURRENCY` | CPUs of the instance | no | Remotion tabs |
| `OPENMONTAGE_RENDER_SCALE` | 0.75 | no | Remotion scale of the 1920×1080 composition (must give integer sizes) |
| `OPENMONTAGE_VOICE_LENGTH_SCALE` | 1.0 | no | Piper speed (>1 slower) |
| `OPENMONTAGE_SEARCH_BUDGET_S` | 75 | no | media search time for one scene |

Paid providers of OpenMontage (fal.ai/Veo/Kling, ElevenLabs, Google TTS, OpenAI images…)
are **not wired** in this version: no key of theirs is read, so none can be spent.

## Networks

`service.json` declares one network, **`*`** (open egress), and the service narrows it
itself (the approach of celaut-basics/yt-transcript):

- The LLM host is the user's choice, so it cannot be a hostname tag fixed at pack time.
- nodo grants a hostname tag's **addresses** and serves **no DNS** in the guest, so an HTTP
  client fails at the lookup (nodo `docs/NETWORKS.md`, “Hostname tags”). The Internet
  Archive also serves each item from its own host (`ia800000.us.archive.org`).
- `app/egress.py` replaces `socket.getaddrinfo` in the Python process: it refuses every
  name that is not the LLM host or one of `commons.wikimedia.org`, `upload.wikimedia.org`,
  `thumb.wikimedia.org`, `images-api.nasa.gov`, `images-assets.nasa.gov`, `archive.org`,
  `*.archive.org` (plus the Pexels / Pixabay hosts only when their keys are set). Python is
  the only program that opens sockets: FFmpeg, Piper and Chromium read local files only.
- `app/resolver.py` writes public resolvers to /etc/resolv.conf at start (root, before the
  service drops to uid 10001), because nodo writes none.

With egress declared, chatui shows its red warning card before a **sensitive** value
(`OPENMONTAGE_LLM_API_KEY`, a Pexels/Pixabay key) goes to the instance — the case that
card was made for. A local LLM needs no key.

## Run it

**With nodo** (one instance = one conversation):

```sh
nodo pack amd64 --local                       # or: nodo import <release>.celaut.bee
nodo execute <service id> --name om \
    -e OPENMONTAGE_LLM_BASE_URL http://192.168.200.1:11435/v1 \
    -e OPENMONTAGE_LLM_MODEL qwen2.5:14b-instruct
python3 client/chat_client.py <instance ip>:8080 "A 30-second video about the Moon, calm music" --out out/
nodo kill <instance id>
```

`192.168.200.1` is the node itself as seen from a guest. A local Ollama listens on
127.0.0.1 only; expose it to the guests for the test with a forwarder
(`socat TCP-LISTEN:11435,bind=192.168.200.1,fork TCP:127.0.0.1:11434`) instead of changing
Ollama's own config.

**With chatui**: start the service from the app (it is a chat service: the slot has
`http2 / grpc / chat`), set `OPENMONTAGE_LLM_BASE_URL` (and the key, if your endpoint has
one) in the launch review, and chat. The poster and the sound track show in the chat.

**Without a node**: `scripts/docker_build.sh` builds the image in the layout of nodo's
packer (it needs a recent BuildKit for `ADD --checksum`), then
`docker run -p 8080:8080 -e OPENMONTAGE_LLM_BASE_URL=… openmontage:dev /entrypoint.sh`.

**Without an LLM**, each sentence becomes a scene, or write a brief, one scene per line:

```
Title: The Moon
footage | moon surface | The Moon is the only natural satellite of Earth.
stat | 384,400 km | It orbits at an average distance of 384,400 kilometres.
photo | earth and moon | It steadies Earth's axis and drives the tides.
```

## Pipelines of OpenMontage: free or paid

| OpenMontage pipeline | Here | Needs |
|---|---|---|
| documentary-montage, animated-explainer (stock/archive footage + Piper + Remotion) | **yes, the one pipeline of this service** | nothing (an LLM for planning is optional) |
| framework-smoke, `render_demo.py` demos (Remotion cards and charts only) | parts used (cards) | nothing |
| animation / character-animation (HyperFrames, SVG rigs, GSAP) | no | free, but HyperFrames is fetched by `npx` at run time and the rigs are authored by the agent |
| screen-demo, podcast-repurpose, clip-factory, talking-head, localization-dub | no | the user's own recordings; faster-whisper or paid ASR |
| cinematic, hybrid, avatar-spokesperson, AI-image explainers | no | paid image/video generation (`FAL_KEY` for FLUX/Veo/Kling, `ATLASCLOUD_API_KEY`, `KLING_API_KEY`, `RUNWAY_API_KEY`, `HEYGEN_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `XAI_API_KEY`…), premium TTS (`ELEVENLABS_API_KEY`, Google TTS), music (`SUNO_API_KEY`); or a GPU for local Wan/Hunyuan/LTX video |

Free keys (no payment, a sign-up): `PEXELS_API_KEY`, `PIXABAY_API_KEY`, `UNSPLASH_ACCESS_KEY`
— the first two are supported here.

## End-to-end test (2026-11-06)

Node: the Alienware test node (nodo dev `97906c7c`, WSL2 Ubuntu 22.04, Ryzen 12 threads,
x86_64 + KVM), as a client only (free credit, no on-chain payment). LLM: `qwen2.5:14b-instruct`
(Q4_K_M) in Ollama on the host's RTX 3090, reached by the guest at `192.168.200.1:11435`
(a socat forwarder to Ollama's 127.0.0.1:11434). Client: `client/chat_client.py`; harness:
`scripts/e2e_node.sh`.

| Step | Result |
|---|---|
| `nodo pack amd64 --local` | service `9321d7c55be8b65c5b355a5c05e7d619b57506c9e1878190ad2df5bfd6a5ee7c`, 1.9 GiB with blocks; 16–17 min on a quiet host (68 min while another job saturated the disk) |
| first `nodo execute` on a node | 7 min (427 s) quiet, 24 min under disk load: the node builds the 8 GB instance disk from the blocks; charged `BUILD_MU` 10 M once per node |
| prompt | “A 30-second video about the Moon: how it formed and why it matters to Earth, calm music” |
| plan | 9 s (model warm on the GPU), 5 scenes, title “Moon's Origin and Impact”, suggestions Render it / Make it shorter / Use more photos |
| “Render it” → files | 129 s; render 119 s = voice 16 s + media search/download 12 s + Remotion 83 s (3 vCPUs) |
| video | 27.7 s, 1280×720 H.264 30 fps + AAC, 2.1 MB; 2 NASA clips (public domain) from Wikimedia Commons + title/text/stat cards, captions, voice, music |
| files | poster.jpg 14.5 kB, soundtrack.mp3 444 kB, the MP4 2.05 MB — all downloaded with `DownloadFile`, SHA3-256 checked |
| cost | 11,354,302 MU = 10,000,000 build (first start of the service id on the node) + 1,354,302 maintenance for the life of the instance; a later start on the same node pays only the maintenance (about 0.65–1.4 M MU for one plan + render) |
| memory | the VM process peaked at 3.5 GB RSS of its 4 GiB (guest page cache included) |

In Docker on the same host (`--cpus 3 --memory 4g`), the same conversation renders in 103–143 s.

## Size and resources

- Image 1.79 GB: Remotion `node_modules` with its headless Chromium 714 MB, Debian
  libraries (FFmpeg, Chromium deps) ~470 MB, Python packages 232 MB (onnxruntime, numpy,
  grpcio), the voice 61 MB, the Remotion bundle 19 MB.
- `service.json`: 3 CPUs (a node with `MAX_CPU_SHARE` 0.5 of 6 cores allows 3), 4 GiB RAM,
  8 GB disk. Remotion's CPU time is most of the render.

## Limitations

- **One pipeline.** The archive-footage explainer only; OpenMontage's other 11 pipelines
  need its full agent workflow, the user's media, a GPU or paid keys.
- **Media fit.** The service picks clips by their titles (the LLM reads them), not by
  looking at them; OpenMontage's own path builds a CLIP corpus and samples frames. A
  scene with no fitting result becomes a text card. Archive search is broad; Archive.org
  items are used only with a Creative Commons / public-domain licence or from the
  Prelinger / home-movies collections. Check each listed licence before reuse.
- **Voice in English only** (one Piper voice in the image). Captions are timed from word
  lengths, not by forced alignment.
- **CPU only.** A 25 s video takes about 2 minutes on 3 CPUs; the LLM is wherever the user
  points it. A cold 14B model can take minutes to load: the service waits up to
  `OPENMONTAGE_LLM_TIMEOUT_S` (300 s), then plans by rules, and after one failed ranking
  call ranks the rest of a render by shared words.
- **The narration is the LLM's**: facts are not checked (in the test, a 14B model wrote that
  the Moon was “captured by Earth's gravity”). Review the plan before “Render it”.
- **First start on a node is slow** (the node builds an 8 GB instance disk from 1.9 GiB of
  blocks: 7–24 min on the test host).
- **chatui shows the poster and the sound, not the MP4** (`file` parts are not displayed by
  chatui yet); a client that downloads `file` parts gets the video.
- Wikimedia's file servers can answer HTTP 429 to bursts; the service then tries the next
  result or source.

## Next steps

- **GPU**: OpenMontage's local video generation (Wan 2.x, Hunyuan, LTX) and faster
  rendering, when nodo can pass a GPU to a guest.
- **Paid providers**, off by default: read their keys as sensitive variables, show the
  estimated cost in the plan and render only after the user agrees (OpenMontage's
  cost tracker already estimates per tool).
- **distributed-llama as the LLM**: start celaut-basics/distributed-llama as a child
  service and point the planner at it, so the whole thing runs inside Celaut with no
  outside LLM.
- More voices/languages, forced-aligned captions, CLIP ranking of the candidates, a
  DNS service in nodo so the egress can be hostname tags instead of `*`.

## Tests

```sh
python3.11 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/        # chat interface over gRPC, plan, egress, layout
```

The image runs a build-time smoke test (`app/smoke.py`): with no network and no LLM, the
chat answers, Piper speaks and Remotion renders a 3-scene video.
