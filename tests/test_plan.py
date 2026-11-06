"""The plan (plan.py), the egress guard (egress.py) and the resolver (resolver.py)."""

import os
import socket
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import egress, resolver  # noqa: E402
from app.llm import parse_json  # noqa: E402
from app.plan import keywords, normalize, plan_without_llm, storyboard  # noqa: E402


def test_normalize_fixes_a_loose_plan():
    plan = normalize({"title": "The Moon " * 20, "theme": "neon", "music": "jazz", "scenes": [
        {"visual": "footage", "query": "", "narration": "The Moon orbits the Earth every month."},
        {"visual": "stat", "stat": "", "on_screen": "", "narration": "It is far away."},
        {"visual": "drone", "query": "moon surface", "narration": "Its surface is grey dust."},
        "junk", {"visual": "text"}]})
    assert len(plan["title"]) <= 60 and plan["theme"] == "flat-motion-graphics" and plan["music"] == "calm"
    assert [s["visual"] for s in plan["scenes"]] == ["footage", "text", "footage"]
    assert plan["scenes"][0]["query"] == "moon orbits earth"
    assert plan["scenes"][1]["on_screen"]
    assert "Voice:" in storyboard(plan)


def test_normalize_caps_the_voice_over():
    plan = normalize({"scenes": [{"visual": "text", "on_screen": "x", "narration": "word " * 100}] * 3})
    assert sum(len(s["narration"].split()) for s in plan["scenes"]) <= 130


def test_normalize_refuses_an_empty_plan():
    with pytest.raises(ValueError):
        normalize({"scenes": []})


def test_plan_without_llm_sentences_and_brief():
    plan = plan_without_llm("Make a video about bees. Bees visit flowers. Honey is made in hives.")
    assert plan["scenes"][0]["visual"] == "title"
    assert [s["query"] for s in plan["scenes"][1:]] == ["bees", "bees visit flowers", "honey hives"]
    brief = plan_without_llm("Title: Moon\nfootage | moon surface | The Moon.\nstat | 384,400 km | Far away.")
    assert brief["title"] == "Moon" and brief["scenes"][1]["stat"] == "384,400 km"


def test_keywords():
    assert keywords("The rings of Saturn are made of ice.") == "rings saturn ice"


def test_parse_json():
    assert parse_json('text ```json\n{"a": 1}\n``` more') == {"a": 1}
    with pytest.raises(ValueError):
        parse_json("no json")


def test_egress_allows_the_list_only(monkeypatch):
    calls = []
    monkeypatch.setattr(socket, "getaddrinfo", socket.getaddrinfo)  # restored after the test
    monkeypatch.setattr(egress, "_original", lambda host, *a, **k: calls.append(host) or [])
    hosts = egress.install({"OPENMONTAGE_LLM_BASE_URL": "http://192.168.200.1:11435/v1", "PEXELS_API_KEY": "x"})
    assert "192.168.200.1" in hosts and "api.pexels.com" in hosts and "pixabay.com" not in hosts
    socket.getaddrinfo("upload.wikimedia.org", 443)
    socket.getaddrinfo("ia800300.us.archive.org", 443)
    socket.getaddrinfo("192.168.200.1", 11435)
    for host in ("evil.example.com", "10.0.0.1", "archive.org.evil.com"):
        with pytest.raises(socket.gaierror):
            socket.getaddrinfo(host, 443)
    assert calls == ["upload.wikimedia.org", "ia800300.us.archive.org", "192.168.200.1"]


def test_resolver(tmp_path):
    out = tmp_path / "resolv.conf"
    assert resolver.main({}, path=str(out), node_config=str(tmp_path / "none")) == 0 and not out.exists()
    (tmp_path / "cfg").write_text("x")
    assert resolver.main({}, path=str(out), node_config=str(tmp_path / "cfg")) == 0
    assert "nameserver 1.1.1.1" in out.read_text()
    assert resolver.main({"OPENMONTAGE_DNS_SERVERS": "9.9.9.9, 1.0.0.1"}, path=str(out)) == 0
    assert out.read_text().count("nameserver") == 2
    assert resolver.main({"OPENMONTAGE_DNS_SERVERS": "dns.google"}, path=str(out)) == 1


def test_licence_gate():
    from types import SimpleNamespace as C
    from app.producer import licence_ok
    vhs = C(license="Public Domain / CC (archive.org — verify per item)", extra={"collection": "opensource_movies"})
    cc = C(license="http://creativecommons.org/licenses/by/4.0/", extra={"collection": "opensource_movies"})
    prelinger = C(license="Public Domain (Prelinger Archives)", extra={"collection": "prelinger"})
    assert not licence_ok("archive_org", vhs)
    assert licence_ok("archive_org", cc) and licence_ok("archive_org", prelinger)
    assert licence_ok("wikimedia", vhs)
