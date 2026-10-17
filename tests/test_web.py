"""本機網頁介面：安全檢查、研究工作、逐步模式、核准、糾偏、記憶。FakeLLM、免費。

研究產物寫到暫存目錄（不動專案的 runs/、data/）。
"""

import threading
import time

import httpx
import pytest

import seo_intel.web.jobs as jobs
import seo_intel.web.server as server


@pytest.fixture
def web(tmp_path, monkeypatch):
    runs, reports = tmp_path / "runs", tmp_path / "reports"
    for mod in (jobs, server):
        monkeypatch.setattr(mod, "RUNS_DIR", runs)
        monkeypatch.setattr(mod, "REPORTS_DIR", reports)
    monkeypatch.setitem(server.SERVE_ROOTS, "runs", runs)
    monkeypatch.setitem(server.SERVE_ROOTS, "reports", reports)
    monkeypatch.setattr(jobs, "RUNS_DIR", runs)
    import seo_intel.core.episode as episode

    monkeypatch.setattr(episode, "RUNS_DIR", runs)
    app = server.App(jobs.JobManager(tmp_path / "intel.db"))
    httpd, app = server.serve(0, app)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    client = httpx.Client(base_url=f"http://127.0.0.1:{httpd.server_address[1]}", timeout=30)
    client.headers["X-Token"] = app.token
    yield client, app, tmp_path
    httpd.shutdown()
    httpd.server_close()


def _wait(client, job_id, since=0, until=("finished",)):
    for _ in range(1200):
        d = client.get(f"/api/jobs/{job_id}?since={since}").json()
        since += len(d["events"])
        hit = [e for e in d["events"] if e["type"] in until]
        if hit:
            return hit[0], since
        time.sleep(0.05)
    raise AssertionError("工作沒有在時限內完成")


def test_security_checks(web):
    client, app, _ = web
    page = client.get("/")
    assert page.status_code == 200 and app.token in page.text
    no_token = httpx.post(f"{client.base_url}/api/research", json={"scenario": "s1"})
    assert no_token.status_code == 403                                     # 其他網站無法替使用者發起研究
    assert client.get("/api/status", headers={"Host": "evil.example"}).status_code == 403   # DNS rebinding
    for bad in ("/files/runs/../../.env", "/files/runs/..%2F..%2F.env", "/files/other/x.html", "/static/../server.py"):
        assert client.get(bad).status_code == 404


def test_research_job_with_approval_round_trip(web):
    client, _, tmp = web
    job = client.post("/api/research", json={"scenario": "s5", "llm": "fake", "memory": "none",
                                             "max_fetches": 30}).json()
    assert client.post("/api/research", json={"scenario": "s1", "memory": "none"}).status_code == 409   # 一次一個
    fin, since = _wait(client, job["id"])
    s = fin["summary"]
    assert fin["status"] == "waiting_approval" and s["pending_approval"]["rule"] == "exceed_budget"
    assert [a["episode"] for a in client.get("/api/approvals").json()] == [s["id"]]
    r = client.post(f"/api/episodes/{s['id']}/decide", json={"approve": False, "note": "到此為止"}).json()
    assert r["approval"]["status"] == "rejected"
    fin, _ = _wait(client, r["job"]["id"], since)
    done = fin["summary"]
    assert done["status"] == "done" and done["claims"][0]["template"] == "summitgear.sim/category/{slug}"
    assert any("人工拒絕" in x for x in done["limitations"])
    assert client.get(f"/files/runs/{s['id']}/timeline.html").status_code == 200
    assert client.get("/api/episodes").json()[0]["status"] == "done"


def test_step_mode_pause_and_resume(web):
    client, _, _ = web
    job = client.post("/api/research", json={"scenario": "s2", "llm": "fake", "memory": "none", "step": True}).json()
    ev, since = _wait(client, job["id"], until=("await",))
    assert ev["seq"] == 1
    client.post(f"/api/jobs/{job['id']}/control", json={"action": "next"})
    ev, since = _wait(client, job["id"], since, until=("await",))
    assert ev["seq"] == 2                                                   # 一次只走一步
    client.post(f"/api/jobs/{job['id']}/control", json={"action": "pause"})
    fin, since = _wait(client, job["id"], since)
    assert fin["status"] == "paused" and fin["summary"]["status"] == "running"
    r = client.post(f"/api/episodes/{job['episode_id']}/resume", json={"step": False}).json()
    fin, _ = _wait(client, r["id"], since)
    assert fin["summary"]["status"] == "done" and fin["summary"]["end_reason"] == "hypothesis_supported"


def test_feedback_and_memory_views(web):
    client, _, tmp = web
    mem = tmp / "m.db"
    job = client.post("/api/research", json={"scenario": "s6", "llm": "fake", "memory": str(mem)}).json()
    fin, _ = _wait(client, job["id"])
    eid = fin["summary"]["id"]
    out = client.post("/api/feedback", json={"episode": eid, "claim": "C1", "verdict": "wrong", "llm": "fake",
                                             "memory": str(mem),
                                             "reason": "flowboard.sim 的 /compare/ 不是原因，應忽略"}).json()
    assert out["lesson"]["source"] == "human_correction"
    m = client.get("/api/memory", params={"path": str(mem)}).json()
    human = next(x for x in m["lessons"] if x["source"] == "human_correction")
    assert human["biases_text"] and m["corrections"][0]["episode_id"] == eid
    detail = client.get(f"/api/memory/lesson/{human['id']}", params={"path": str(mem)}).json()
    assert detail["events"][0]["event"] == "created"
    assert client.post("/api/memory/retire", json={"lesson": human["id"], "reason": "測試", "path": str(mem)}).json()["ok"]
    assert client.post("/api/eval", json={"llm": "live"}).status_code == 409   # 真實 LLM 評測必須給花費上限
