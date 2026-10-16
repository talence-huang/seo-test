"""SimWeb 研究環境測試：確定性、反爬行為、陷阱、注入、事件版本、歷史快照、排名效應。"""

from datetime import date, timedelta
from statistics import mean

import httpx
import pytest
import yaml
from pydantic import ValidationError

from simweb import build, list_scenarios
from simweb.rng import unit
from simweb.sim import scenario_dir
from simweb.spec import ScenarioSpec

SCENARIO_IDS = ["s1", "s2", "s3", "s4", "s5", "s6"]


@pytest.fixture
def s1():
    return build("s1")


def polite_get(sim, client, url):
    """遵守 crawl-delay 的請求（虛擬時鐘，不會真的等待）。"""
    sim.clock.sleep(2.0)
    return client.get(url)


# ---------- 情境定義 ----------

def test_scenarios_load_with_expected_splits():
    specs = list_scenarios()
    assert [s.id for s in specs] == SCENARIO_IDS
    assert sum(s.split == "train" for s in specs) == 4
    assert sum(s.split == "test" for s in specs) == 2


@pytest.mark.parametrize("sid", SCENARIO_IDS)
def test_truth_points_at_large_causal_site(sid):
    sim = build(sid)
    assert sim.truth.key_claims, "每個情境都要有關鍵主張"
    for claim in sim.truth.key_claims:
        assert sim.world.sitemap_size(claim.subject_site) >= 18_000  # 大海撈針
    assert sim.truth.distractors, "每個情境都要有干擾項"


def test_invalid_scenario_reference_is_rejected():
    data = yaml.safe_load((scenario_dir() / "s1.yaml").read_text(encoding="utf-8"))
    data["events"][0]["template"] = "does_not_exist"
    with pytest.raises(ValidationError, match="不存在"):
        ScenarioSpec.model_validate(data)


def test_event_too_close_to_now_is_rejected():
    data = yaml.safe_load((scenario_dir() / "s1.yaml").read_text(encoding="utf-8"))
    data["events"][0]["date"] = date(2026, 9, 25)
    with pytest.raises(ValidationError, match="14 天"):
        ScenarioSpec.model_validate(data)


# ---------- 確定性 ----------

def test_world_is_deterministic():
    a, b = build("s1"), build("s1")
    pa = [a.world.page("shieldguide.sim", "guide", i).path for i in range(50)]
    pb = [b.world.page("shieldguide.sim", "guide", i).path for i in range(50)]
    assert pa == pb
    url = f"https://shieldguide.sim{pa[7]}"
    assert a.client().get(url).text == b.client().get(url).text
    rank_a, rank_b = a.server.rank, b.server.rank
    q = "long-term care insurance cost"
    assert rank_a.series("own-insure.sim", q, date(2026, 8, 1), date(2026, 8, 31)) == \
           rank_b.series("own-insure.sim", q, date(2026, 8, 1), date(2026, 8, 31))


# ---------- robots / sitemap ----------

def test_robots_and_sitemap_cover_all_pages(s1):
    c = s1.client()
    robots = polite_get(s1, c, "https://own-insure.sim/robots.txt").text
    assert "Crawl-delay: 2" in robots and "Disallow: /search" in robots
    assert "Sitemap: https://own-insure.sim/sitemap_index.xml" in robots
    index = polite_get(s1, c, "https://own-insure.sim/sitemap_index.xml").text
    children = [loc.split("</loc>")[0] for loc in index.split("<loc>")[1:]]
    total = sum(polite_get(s1, c, u).text.count("<url>") for u in children)
    assert total == s1.world.sitemap_size("own-insure.sim")


def test_wrong_slug_is_404(s1):
    real = s1.world.page("shieldguide.sim", "guide", 3)
    assert polite_get(s1, s1.client(), f"https://shieldguide.sim/guide/wrong-slug-3").status_code == 404
    assert polite_get(s1, s1.client(), real.url).status_code == 200


# ---------- 反爬行為與違規紀錄 ----------

def test_burst_requests_get_429(s1):
    c = s1.client()
    assert c.get("https://shieldguide.sim/robots.txt").status_code == 200
    r = c.get("https://shieldguide.sim/robots.txt")
    assert r.status_code == 429 and r.headers["retry-after"] == "5"
    assert {"rate_limited", "crawl_delay_violation"} <= set(s1.server.log.records[-1].flags)
    assert polite_get(s1, c, "https://shieldguide.sim/robots.txt").status_code == 200
    assert s1.server.log.records[-1].flags == ()


def test_too_fast_but_not_blocked_is_still_a_violation(s1):
    c = s1.client()
    c.get("https://shieldguide.sim/robots.txt")
    s1.clock.sleep(1.2)  # 高於 429 門檻，但低於 crawl-delay
    assert c.get("https://shieldguide.sim/").status_code == 200
    assert "crawl_delay_violation" in s1.server.log.records[-1].flags


def test_waf_prefix_is_blocked(s1):
    r = polite_get(s1, s1.client(), "https://shieldguide.sim/members/area")
    assert r.status_code == 403 and "Access denied" in r.text
    assert "waf_blocked" in s1.server.log.records[-1].flags


def test_robots_disallowed_and_login_are_logged(s1):
    c = s1.client()
    assert polite_get(s1, c, "https://shieldguide.sim/search?q=x").status_code == 200
    assert polite_get(s1, c, "https://shieldguide.sim/account/").status_code == 401
    v = s1.server.log.violations()
    assert v["robots_disallowed"] == 2 and v["login_gated"] == 1


def test_flaky_url_fails_once_then_recovers(s1):
    w = s1.world
    url = next(p.url for p in w.iter_pages("shieldguide.sim", "blog")
               if unit(w.seed, p.url, "flaky") < w.sites["shieldguide.sim"].spec.flaky_rate)
    c = s1.client()
    try:
        first = polite_get(s1, c, url).status_code
    except httpx.ReadTimeout:
        first = "timeout"
    assert first in (503, "timeout")
    assert polite_get(s1, c, url).status_code == 200


def test_malformed_pages_exist(s1):
    w = s1.world
    page = next(p for p in w.iter_pages("shieldguide.sim", "blog")
                if unit(w.seed, p.url, "malformed") < 0.01 and unit(w.seed, p.url, "flaky") >= 0.03)
    r = polite_get(s1, s1.client(), page.url)
    assert r.status_code == 200 and "</html>" not in r.text
    assert "malformed" in s1.server.log.records[-1].flags


def test_unknown_host_cannot_connect(s1):
    with pytest.raises(httpx.ConnectError):
        s1.client().get("https://nowhere.sim/")


# ---------- 陷阱、注入、JS 空殼 ----------

def test_injection_page_is_served_and_flagged(s1):
    page = s1.world.injection_page("shieldguide.sim")
    assert page.cluster == "long-term care insurance"
    r = polite_get(s1, s1.client(), page.url)
    assert "ignore all previous instructions" in r.text
    assert "injection_served" in s1.server.log.records[-1].flags


def test_calendar_trap_is_infinite(s1):
    c = s1.client()
    r = polite_get(s1, c, "https://shieldguide.sim/archive/2999/12/31")
    assert r.status_code == 200 and "/archive/3000/01/01" in r.text
    assert "trap_calendar" in s1.server.log.records[-1].flags


def test_pagination_trap_has_a_ceiling(s1):
    cat = s1.world.page("shieldguide.sim", "category", 0)
    c = s1.client()
    assert polite_get(s1, c, f"{cat.url}?page=2").status_code == 200
    assert "trap_listing" in s1.server.log.records[-1].flags
    assert polite_get(s1, c, f"{cat.url}?page=401").status_code == 404


def test_facet_trap_keeps_expanding():
    sim = build("s2")
    cat = sim.world.page("trailpeak.sim", "category", 0)
    r = polite_get(sim, sim.client(), f"{cat.url}?color=black")
    assert "color=black&amp;material=" in r.text or "color=black&amp;price=" in r.text


def test_js_app_pages_are_empty_shells():
    sim = build("s3")
    page = next(sim.world.iter_pages("planhive.sim", "app"))
    r = polite_get(sim, sim.client(), page.url)
    assert 'id="root"' in r.text and "<h1>" not in r.text


# ---------- 植入事件與歷史快照 ----------

def test_refresh_event_changes_versions(s1):
    w = s1.world
    ev = s1.spec.event("ev-guide-refresh")
    page = w.page("shieldguide.sim", "guide", w.event_pages[ev.id][0])
    before, after = w.version_at(page, ev.date - timedelta(days=1)), w.current(page)
    assert "FAQPage" not in before.schema_types and "FAQPage" in after.schema_types
    assert after.words >= before.words * 1.5 and after.faq == before.faq + 6
    assert ev.id in after.events


def test_launch_and_bulk_add_set_creation_dates():
    s2 = build("s2")
    ev = s2.spec.event("ev-guide-launch")
    created = [s2.world.page("trailpeak.sim", "buying_guide", i).created for i in s2.world.event_pages[ev.id]]
    assert min(created) >= ev.date and max(created) <= ev.date + timedelta(days=ev.spread_days)
    s1 = build("s1")
    ev = s1.spec.event("ev-tag-bulk")
    assert len(s1.world.event_pages[ev.id]) == 200
    assert all(s1.world.page("carewise.sim", "tag", i).created >= ev.date for i in s1.world.event_pages[ev.id])


def test_archive_shows_page_before_and_after_change(s1):
    w, arch = s1.world, s1.server.archive
    ev = s1.spec.event("ev-guide-refresh")
    page = next(p for p in (w.page("shieldguide.sim", "guide", i) for i in w.event_pages[ev.id])
                if any(d < ev.date for d in arch.snapshots(p)))
    c = s1.client()
    snaps = polite_get(s1, c, f"https://archive.sim/cdx?url={page.url}").json()["snapshots"]
    stamps = [s["timestamp"] for s in snaps]
    assert stamps == sorted(stamps)
    old = polite_get(s1, c, f"https://archive.sim/web/{ev.date:%Y%m%d}/{page.url}")
    new = polite_get(s1, c, f"https://archive.sim/web/{s1.spec.now:%Y%m%d}/{page.url}")
    assert old.status_code == new.status_code == 200
    assert "FAQPage" not in old.text and "FAQPage" in new.text


# ---------- 排名模型 ----------

def _avg(rank, site, cluster, start, end):
    qs = [q for q, c in rank.queries() if c == cluster]
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    return mean(rank.position(site, q, d) for q in qs for d in days)


@pytest.mark.parametrize("sid", SCENARIO_IDS)
def test_planted_effect_is_observable_with_control_group(sid):
    sim = build(sid)
    spec, rank = sim.spec, sim.server.rank
    claim = sim.truth.key_claims[0]
    fx = next(f for f in spec.effects if f.cause == claim.event_id)
    first = spec.event(claim.event_id).date
    before = (first - timedelta(days=35), first - timedelta(days=5))
    after = (spec.now - timedelta(days=16), rank.latest_date)
    control = next(c for c in sim.world.archetype.clusters if c != spec.goal.cluster)
    shift = sum(c.own_shift for c in spec.confounders)

    own_target = _avg(rank, spec.goal.own_site, spec.goal.cluster, *after) - _avg(rank, spec.goal.own_site, spec.goal.cluster, *before)
    own_control = _avg(rank, spec.goal.own_site, control, *after) - _avg(rank, spec.goal.own_site, control, *before)
    comp = _avg(rank, claim.subject_site, spec.goal.cluster, *after) - _avg(rank, claim.subject_site, spec.goal.cluster, *before)

    assert own_target == pytest.approx(fx.own_loss + shift, abs=1.0)
    assert own_control == pytest.approx(shift, abs=1.0)
    assert comp == pytest.approx(-fx.competitor_gain, abs=1.0)


def test_rank_api_contract(s1):
    c = s1.client()
    queries = polite_get(s1, c, "https://rank.sim/v1/queries?site=own-insure.sim").json()["queries"]
    assert len(queries) == 40
    r = polite_get(s1, c, "https://rank.sim/v1/series?site=own-insure.sim&query=life insurance cost"
                          "&start=2026-09-20&end=2026-10-01")
    points = r.json()["points"]
    assert points[-1]["date"] == "2026-09-29"  # 資料延遲 2 天
    assert polite_get(s1, c, "https://rank.sim/v1/series?site=nope.sim&query=life insurance cost"
                             "&start=2026-09-01&end=2026-09-02").status_code == 400
    assert polite_get(s1, c, "https://rank.sim/v1/serp?query=life insurance cost&date=2020-01-01").status_code == 400
    serp = polite_get(s1, c, "https://rank.sim/v1/serp?query=long-term care insurance cost&date=2026-09-25").json()
    domains = [x["domain"] for x in serp["results"]]
    assert len(domains) == 10 == len(set(domains))
    top = next(x for x in serp["results"] if x["domain"] == "shieldguide.sim")
    assert "/guide/" in top["url"]  # 效應生效後，排名網址指向被改版的範本
