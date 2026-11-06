"""The chat interface over real gRPC, with a fake agent (no LLM, no render)."""

import hashlib
import os
import sys
import tempfile
import time

import grpc
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import chat_pb2 as pb  # noqa: E402
from app import chat_pb2_grpc as rpc  # noqa: E402
from app.chat import Chat, SHA3_256_TYPE  # noqa: E402
from app.server import make_server  # noqa: E402


class FakeAgent:
    """Plans by echo; 'render' streams progress and posts one file of each kind."""

    def __init__(self, folder):
        self.folder = folder

    def answer(self, turn):
        if turn.user_text == "render":
            turn.start()
            turn.write("• step one\n")
            turn.progress(0.5, "half")
            turn.write("• step two\n")
            turn.finish()
            paths = []
            for name, data in (("p.jpg", b"\xff\xd8jpeg"), ("s.mp3", b"ID3mp3"), ("v.mp4", b"\x00\x00\x00 ftypmp4")):
                path = os.path.join(self.folder, name)
                with open(path, "wb") as f:
                    f.write(data)
                paths.append(path)
            turn.post("here", files=[("image", paths[0], "image/jpeg", "p.jpg", {"alt_text": "poster"}),
                                     ("audio", paths[1], "audio/mpeg", "s.mp3", {"duration_ms": 1000}),
                                     ("file", paths[2], "video/mp4", "v.mp4", {})],
                      suggestions=["Again", "x" * 100, "Bad\nline"], metadata={"total_s": 1.5})
        elif turn.user_text == "boom":
            raise RuntimeError("boom")
        else:
            turn.set_title("Title: " + turn.user_text)
            turn.write("plan of " + turn.user_text)
            turn.finish(["Render it", "Shorter"])


@pytest.fixture()
def service():
    folder = tempfile.mkdtemp()
    chat = Chat(FakeAgent(folder))
    server, port = make_server(chat, host="127.0.0.1", port=0)
    server.start()
    channel = grpc.insecure_channel("127.0.0.1:%d" % port)
    yield rpc.ChatServiceStub(channel), rpc.FileServiceStub(channel), chat
    server.stop(0)


def send(stub, text, key=None):
    return stub.SendMessage(pb.SendMessageRequest(
        content=[pb.ContentPart(text=pb.TextContent(text=text))], idempotency_key=key))


def settle(stub, count, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        messages = list(stub.GetMessages(pb.GetMessagesRequest()))
        if len(messages) >= count and all(m.status not in (1, 2) for m in messages):
            return messages
        time.sleep(0.05)
    raise AssertionError([(m.sequence, m.status) for m in stub.GetMessages(pb.GetMessagesRequest())])


def test_capabilities(service):
    stub, _, _ = service
    caps = stub.GetCapabilities(pb.GetCapabilitiesRequest())
    assert list(caps.accepted_mime_types) == []
    assert caps.max_file_bytes == 24 << 20 and caps.supports_cancel and not caps.supports_fork


def test_plan_title_and_suggestions(service):
    stub, _, _ = service
    user = send(stub, "moon", "k1")
    assert user.sequence == 1 and user.status == pb.MESSAGE_STATUS_COMPLETED
    messages = settle(stub, 2)
    answer = messages[1]
    assert answer.reply_to_message_id == user.message_id
    assert answer.content[0].text.text == "plan of moon"
    assert list(answer.suggestions) == ["Render it", "Shorter"]
    assert stub.GetConversation(pb.GetConversationRequest()).title == "Title: moon"
    # The same key gives the same message and no new answer.
    again = send(stub, "other", "k1")
    assert again.message_id == user.message_id
    assert len(settle(stub, 2)) == 2


def test_render_posts_files(service):
    stub, files, _ = service
    send(stub, "render", "r1")
    messages = settle(stub, 3)
    progress, posted = messages[1], messages[2]
    assert progress.status == pb.MESSAGE_STATUS_COMPLETED
    assert progress.content[0].text.text == "• step one\n• step two\n"
    assert posted.reply_to_message_id == messages[0].message_id
    assert [p.WhichOneof("kind") for p in posted.content] == ["text", "image", "audio", "file"]
    assert list(posted.suggestions) == ["Again", "x" * 64, "Bad line"]
    assert posted.metadata["total_s"] == "1.5"
    video = posted.content[3].file
    assert video.mime_type == "video/mp4" and video.hashes[0].type == SHA3_256_TYPE
    data = b"".join(c.chunk for c in files.DownloadFile(pb.DownloadFileRequest(hash=video.hashes[0])))
    assert hashlib.sha3_256(data).digest() == video.hashes[0].value and len(data) == video.size_bytes
    answers = list(files.QueryFiles(iter([pb.QueryFilesRequest(hash=video.hashes[0]),
                                          pb.QueryFilesRequest(hash=pb.Hash(type=SHA3_256_TYPE, value=b"\0" * 32))])))
    assert [a.available for a in answers] == [True, False]


def test_failure_has_error(service):
    stub, _, _ = service
    send(stub, "boom")
    answer = settle(stub, 2)[1]
    assert answer.status == pb.MESSAGE_STATUS_FAILED and "boom" in answer.error


def test_errors(service):
    stub, files, _ = service
    with pytest.raises(grpc.RpcError) as error:
        stub.SendMessage(pb.SendMessageRequest(content=[pb.ContentPart(image=pb.ImageContent(
            file=pb.FileReference(hashes=[pb.Hash(type=SHA3_256_TYPE, value=b"1" * 32)],
                                  mime_type="image/png", size_bytes=10)))]))
    assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert dict(error.value.trailing_metadata())["chat-reason"] == "unsupported_media"
    with pytest.raises(grpc.RpcError) as error:
        list(files.DownloadFile(pb.DownloadFileRequest(hash=pb.Hash(value=b"1" * 32))))
    assert dict(error.value.trailing_metadata())["chat-reason"] == "bad_request"
    with pytest.raises(grpc.RpcError) as error:
        files.UploadFile(iter([pb.UploadFileRequest(file=pb.FileReference(
            hashes=[pb.Hash(type=SHA3_256_TYPE, value=b"1" * 32)], mime_type="image/png", size_bytes=3))]))
    assert dict(error.value.trailing_metadata())["chat-reason"] == "unsupported_media"
    with pytest.raises(grpc.RpcError) as error:
        stub.AnswerSecretRequest(pb.AnswerSecretRequestRequest(message_id="x", request_id="y", skipped=True))
    assert error.value.code() == grpc.StatusCode.NOT_FOUND


def test_connect_replay_and_title_event(service):
    stub, _, _ = service
    send(stub, "first", "a")
    settle(stub, 2)
    outgoing = [pb.ClientEvent(subscribe=pb.SubscribeRequest(after_sequence=0))]
    stream = stub.Connect(iter(outgoing + [pb.ClientEvent(event_id="e1", send_message=pb.SendMessageRequest(
        content=[pb.ContentPart(text=pb.TextContent(text="second"))], idempotency_key="b"))]))
    kinds, titles = [], []
    for event in stream:
        kind = event.WhichOneof("event")
        kinds.append(kind)
        if kind == "subscription_ready":
            assert event.subscription_ready.title == "Title: first"
        if kind == "conversation_title_changed":
            titles.append(event.conversation_title_changed.title)
        if kind == "message_status_changed" and event.message_status_changed.status == pb.MESSAGE_STATUS_COMPLETED:
            break
    assert kinds[:3] == ["message_created", "message_created", "subscription_ready"]
    assert titles == ["Title: second"]
