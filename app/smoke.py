"""Build-time smoke test (amd64/.service/Dockerfile): with no network and no LLM, the chat
answers over gRPC, Piper speaks and Remotion renders a short video of cards.

Run as `python3 -m app.smoke` from the directory that holds `app/`.
"""

import os
import sys
import time

import grpc

from . import chat_pb2 as pb
from . import chat_pb2_grpc as rpc
from .chat import Chat
from .director import Director
from .llm import LLM
from .producer import Producer
from .server import make_server

BRIEF = """Title: Smoke test
title | OpenMontage | This is a test of the voice.
stat | 3 scenes | It has three scenes.
text | The end | Goodbye."""


def main():
    producer = Producer()
    producer.work_root.mkdir(parents=True, exist_ok=True)
    chat = Chat(Director(LLM({}), producer, "smoke"))
    server, port = make_server(chat, host="127.0.0.1", port=0)
    server.start()
    stub = rpc.ChatServiceStub(grpc.insecure_channel("127.0.0.1:%d" % port))
    send = lambda text, key: stub.SendMessage(pb.SendMessageRequest(  # noqa: E731
        content=[pb.ContentPart(text=pb.TextContent(text=text))], idempotency_key=key))
    send(BRIEF, "plan")
    send("Render it", "render")
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        messages = list(stub.GetMessages(pb.GetMessagesRequest()))
        if len(messages) >= 5 and all(m.status not in (1, 2) for m in messages):
            break
        if any(m.status == pb.MESSAGE_STATUS_FAILED for m in messages):
            break
        time.sleep(2)
    messages = list(stub.GetMessages(pb.GetMessagesRequest()))
    statuses = [(m.sequence, m.from_service, m.status) for m in messages]
    assert len(messages) == 5, statuses
    assert messages[1].status == pb.MESSAGE_STATUS_COMPLETED, messages[1]
    assert messages[3].status == pb.MESSAGE_STATUS_COMPLETED, messages[3].error
    files = messages[4]
    kinds = [p.WhichOneof("kind") for p in files.content]
    assert kinds == ["text", "image", "audio", "file"], kinds
    video = files.content[3].file
    assert video.mime_type == "video/mp4" and video.size_bytes > 10_000, video
    data = b"".join(c.chunk for c in rpc.FileServiceStub(grpc.insecure_channel("127.0.0.1:%d" % port))
                    .DownloadFile(pb.DownloadFileRequest(hash=video.hashes[0])))
    assert len(data) == video.size_bytes
    conversation = stub.GetConversation(pb.GetConversationRequest())
    assert conversation.title == "Smoke test", conversation
    print("smoke test: %s, %d bytes, %s s" % (video.filename, video.size_bytes,
                                              files.metadata.get("total_s")))
    server.stop(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
