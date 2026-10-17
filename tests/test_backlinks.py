"""反向連結 CSV 的讀取與輪廓（Off-page 的資料來源；不連網、不呼叫 LLM）。"""

from datetime import date

import pytest

from seo_intel.real.backlinks import BacklinkData, link_profile, parse

WINDOW = (date(2026, 7, 3), date(2026, 10, 1))


AHREFS = """Referring page title,Referring page URL,Domain rating,Target URL,Anchor,Nofollow,First seen
x,https://blog.example.com/p1,45,https://a.tw/guide/%E9%AB%98%E5%B1%B1%E8%8C%B6-1/,高山茶推薦,false,2026-08-10T03:00:00Z
x,https://news.example.net/p2,60,https://a.tw/guide/%E9%AB%98%E5%B1%B1%E8%8C%B6-2/,高山茶怎麼挑,false,2026-09-01
x,https://forum.example.org/t/9,20,https://a.tw/,a.tw,true,2025-11-01
x,https://own-fan.example.com/x,10,https://own.tw/,我們的網站,false,2026-08-20
"""


def test_backlink_csv_variants_and_profile(tmp_path):
    links = parse(AHREFS)
    assert len(links) == 4 and links[0].first_seen == date(2026, 8, 10) and links[2].nofollow is True
    semrush = "Page ascore;Source url;Target url;Anchor;Nofollow;First seen\n30;https://s.example.com/a;https://a.tw/x/;高山茶;false;2026-09-02\n"
    assert parse(semrush)[0].authority == 30
    utf16 = ("Referring page URL\tTarget URL\nhttps://s.example.com/a\thttps://a.tw/\n").encode("utf-16")
    data_path = tmp_path / "ahrefs-excel.csv"
    data_path.write_bytes(utf16)
    assert BacklinkData.from_paths([data_path]).links[0].target_domain == "a.tw"
    with pytest.raises(ValueError, match="缺少欄位"):
        parse("Anchor,First seen\nx,2026-01-01\n")
    data = BacklinkData(links, ["a.csv"])
    comp = data.for_site("www.a.tw")
    assert len(comp) == 3 and len(data.for_site("own.tw")) == 1
    p = link_profile(comp, "a.tw", WINDOW, ["高山茶"], ranking_urls={"https://a.tw/guide/%E9%AB%98%E5%B1%B1%E8%8C%B6-1/"})
    assert p["new_ref_domains_in_window"] == 2 and p["n_ref_domains"] == 3
    assert p["anchor_share"]["keyword"] == pytest.approx(0.667, abs=1e-3) and p["share_to_ranking_pages"] == pytest.approx(0.333, abs=1e-3)
    assert p["top_targets"][0]["template"] == "a.tw/guide/{slug}" and p["nofollow_share"] == pytest.approx(0.333, abs=1e-3)
