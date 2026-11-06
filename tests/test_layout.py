"""The pack root, the manifest and the pins agree with the code."""

import json
import os
import re
import subprocess

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def test_pack_root_links_the_app():
    assert os.path.islink(os.path.join(ROOT, "amd64", "app"))
    assert os.readlink(os.path.join(ROOT, "amd64", "app")) == "../app"
    config = json.loads(read("amd64", ".service", "pack_config.json"))
    assert config["include"] == ["app"]


def test_manifest_declares_the_chat_slot_and_the_envs():
    service = json.loads(read("amd64", ".service", "service.json"))
    slot = service["api"][0]
    assert slot["port"] == 8080 and slot["transport"] == "tcp"
    stack = [layer if isinstance(layer, list) else layer["tags"] for layer in slot["protocol_stack"]]
    assert stack == [["http2"], ["grpc"], ["chat"]]
    assert "github.com/celaut-basics/openmontage" in slot["protocol_stack"][2]["prose"]
    assert [n["tags"] for n in service["network"]] == [["*"]]
    used = set()
    for name in os.listdir(os.path.join(ROOT, "app")):
        if name.endswith(".py") and not name.endswith("_pb2.py") and not name.endswith("_grpc.py"):
            used |= set(re.findall(r"\b((?:OPENMONTAGE|PEXELS|PIXABAY)_[A-Z_]+)\b", read("app", name)))
    internal = {"OPENMONTAGE_UPSTREAM", "OPENMONTAGE_UPSTREAM_COMMIT", "OPENMONTAGE_REMOTION_BUNDLE",
                "OPENMONTAGE_VOICE_MODEL", "OPENMONTAGE_WORK_DIR"}
    assert used - internal == set(service["envs"]), sorted(used - internal ^ set(service["envs"]))


def test_dockerfile_pins_the_upstream_of_the_submodule():
    pinned = re.search(r"ARG OPENMONTAGE_COMMIT=([0-9a-f]{40})", read("amd64", ".service", "Dockerfile")).group(1)
    assert pinned in read("README.md")
    try:
        tree = subprocess.run(["git", "-C", ROOT, "ls-tree", "HEAD", "upstream"],
                              capture_output=True, text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        return  # not a git checkout (the packed tree): nothing to compare
    if tree:
        assert tree[1] == "commit" and tree[2] == pinned, tree
