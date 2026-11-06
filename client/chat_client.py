"""A test client of the service, over the chat interface (chat.md revision 6).

    python3 client/chat_client.py <host:port> "<description of the video>" [--out DIR]

It reads the capabilities, opens `Connect`, sends the description, waits for the plan,
sends "Render it", waits for the message with the files, downloads them with
`DownloadFile`, checks their SHA3-256 hashes and writes them to DIR. It prints one line
for each event that matters and a JSON summary at the end.
"""

import argparse
import hashlib
import json
import os
import queue
import sys
import threading
import time
import uuid

import grpc

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import chat_pb2 as pb  # noqa: E402
from app import chat_pb2_grpc as rpc  # noqa: E402

FINAL = (pb.MESSAGE_STATUS_COMPLETED, pb.MESSAGE_STATUS_FAILED, pb.MESSAGE_STATUS_CANCELLED)


def log(text):
    print("[%7.1f] %s" % (time.monotonic() - T0, text), flush=True)


T0 = time.monotonic()


class Conversation:
    def __init__(self, address):
        self.channel = grpc.insecure_channel(address)
        self.chat = rpc.ChatServiceStub(self.channel)
        self.files = rpc.FileServiceStub(self.channel)
        self.messages = {}  # message_id -> Message
        self.title = None
        self.events = queue.Queue()
        self.outgoing = queue.Queue()
        self.lock = threading.Lock()

    def wait_ready(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self.chat.GetCapabilities(pb.GetCapabilitiesRequest(), timeout=10)
            except grpc.RpcError as error:
                if time.monotonic() > deadline:
                    raise
                log("waiting for the service (%s)" % error.code().name)
                time.sleep(3)

    def connect(self):
        def requests():
            yield pb.ClientEvent(subscribe=pb.SubscribeRequest(after_sequence=0), event_id="sub")
            while True:
                item = self.outgoing.get()
                if item is None:
                    return
                yield item

        def read():
            try:
                for event in self.chat.Connect(requests()):
                    self._apply(event)
            except grpc.RpcError as error:
                log("stream ended: %s" % error.code().name)

        threading.Thread(target=read, daemon=True).start()

    def _apply(self, event):
        kind = event.WhichOneof("event")
        with self.lock:
            if kind == "message_created":
                m = event.message_created.message
                self.messages[m.message_id] = m
            elif kind == "message_delta":
                d = event.message_delta
                m = self.messages[d.message_id]
                if m.content and m.content[-1].WhichOneof("kind") == "text":
                    m.content[-1].text.text += d.text_append
                else:
                    m.content.add().text.text = d.text_append
                if d.text_append.startswith("• "):
                    log("progress: " + d.text_append.strip()[2:])
            elif kind == "message_status_changed":
                c = event.message_status_changed
                m = self.messages[c.message_id]
                m.status = c.status
                if c.error:
                    m.error = c.error
                m.suggestions.extend(c.suggestions)
            elif kind == "conversation_title_changed":
                self.title = event.conversation_title_changed.title
                log("title: %s" % self.title)
            elif kind == "subscription_ready":
                log("subscribed (latest %d)" % event.subscription_ready.latest_sequence)
            elif kind == "error":
                log("protocol error: %s %s" % (event.error.code, event.error.message))
        self.events.put(kind)

    def send(self, text):
        key = uuid.uuid4().hex[:16]
        self.outgoing.put(pb.ClientEvent(event_id="e" + key, send_message=pb.SendMessageRequest(
            content=[pb.ContentPart(text=pb.TextContent(text=text))], idempotency_key=key)))
        log("sent: %s" % text)
        return key

    def answers_to(self, key, count, timeout):
        """Wait until the user message of `key` has `count` final answers."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                user = next((m for m in self.messages.values() if m.idempotency_key == key), None)
                if user:
                    answers = sorted((m for m in self.messages.values()
                                      if m.from_service and m.reply_to_message_id == user.message_id),
                                     key=lambda m: m.sequence)
                    if len(answers) >= count and all(a.status in FINAL for a in answers[:count]):
                        return [pb.Message.FromString(a.SerializeToString()) for a in answers]
                    if any(a.status == pb.MESSAGE_STATUS_FAILED for a in answers):
                        return [pb.Message.FromString(a.SerializeToString()) for a in answers]
            try:
                self.events.get(timeout=5)
            except queue.Empty:
                pass
        raise TimeoutError("no answer in %d s" % timeout)

    def download(self, ref, path):
        sha3 = next(h for h in ref.hashes if h.type == hashlib.sha3_256(b"").digest())
        data = b"".join(c.chunk for c in self.files.DownloadFile(
            pb.DownloadFileRequest(hash=sha3), timeout=120))
        assert len(data) == ref.size_bytes, (len(data), ref.size_bytes)
        assert hashlib.sha3_256(data).digest() == sha3.value, "hash mismatch"
        with open(path, "wb") as f:
            f.write(data)
        return len(data)


def text(message):
    return "".join(p.text.text for p in message.content if p.WhichOneof("kind") == "text")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("address")
    parser.add_argument("description")
    parser.add_argument("--out", default="out")
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)
    c = Conversation(args.address)
    caps = c.wait_ready(600)
    log("capabilities: accepted=%s max_file=%d cancel=%s" % (list(caps.accepted_mime_types),
                                                             caps.max_file_bytes, caps.supports_cancel))
    c.connect()
    summary = {"description": args.description}
    t = time.monotonic()
    key = c.send(args.description)
    plan = c.answers_to(key, 1, 900)[0]
    summary["plan_s"] = round(time.monotonic() - t, 1)
    log("plan (%s, %.0f s):\n%s" % (pb.MessageStatus.Name(plan.status), summary["plan_s"], text(plan)))
    log("suggestions: %s" % list(plan.suggestions))
    if plan.status != pb.MESSAGE_STATUS_COMPLETED:
        summary["error"] = plan.error
        print(json.dumps(summary))
        return 1
    t = time.monotonic()
    key = c.send(plan.suggestions[0] if plan.suggestions else "Render it")
    answers = c.answers_to(key, 2, args.timeout)
    summary["render_wall_s"] = round(time.monotonic() - t, 1)
    progress = answers[0]
    if progress.status != pb.MESSAGE_STATUS_COMPLETED or len(answers) < 2:
        summary["error"] = progress.error or "no files message"
        log("render failed: %s" % summary["error"])
        print(json.dumps(summary))
        return 1
    files = answers[1]
    log("files message:\n%s" % text(files))
    summary["metadata"] = dict(files.metadata)
    summary["title"] = c.title
    for part in files.content:
        kind = part.WhichOneof("kind")
        ref = getattr(part, kind).file if kind in ("image", "audio") else (part.file if kind == "file" else None)
        if ref is None:
            continue
        name = ref.filename or ("%s.bin" % kind)
        size = c.download(ref, os.path.join(args.out, name))
        summary.setdefault("files", []).append({"kind": kind, "mime": ref.mime_type, "name": name, "bytes": size})
        log("downloaded %s %s (%d bytes, hash ok)" % (kind, name, size))
    c.outgoing.put(None)
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
